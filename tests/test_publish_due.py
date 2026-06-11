"""publish_due + make_drafts behaviors: one post per window, best quote_score
first, skip-on-failure, stale expiry, publish-time re-checks, judge-once /
no-clobber scoring, supersede, circuit breaker, vet/revision flow."""
from datetime import timedelta

from xbot.config import NS
from xbot.models import Draft, Metrics, Post, Score, utcnow
from xbot.orchestrator import Orchestrator
from xbot.storage.sqlite_repo import SqliteRepository


def _cfg(tmp_path, autonomous=True, per_run=1, extra=None):
    data = {
        "mode": {"autonomous": autonomous},
        "posting": {"per_day": 3, "per_run": per_run},
        "ops": {"kill_switch_file": str(tmp_path / "STOP")},
        "ranking": {"teaching_weight": 0.65, "qa_gate": False},
        "scoring_weights": {"likes": 0.05, "reposts": 0.15, "velocity": 0.10,
                            "eng_per_follower": 0.25, "echo": 0.25,
                            "recency": 0.10, "topic_fit": 0.10},
        "recency_tau_hours": 8,
    }
    if extra:
        data.update(extra)
    return NS(data)


def _repo():
    repo = SqliteRepository(":memory:")
    repo.init_schema()
    return repo


def _post(tid, handle=None, text=None):
    handle = handle or f"user{tid}"
    # distinct tokens per tid so the near-duplicate check doesn't collapse them
    text = text or f"post {tid} growth tactic alpha{tid} beta{tid} gamma{tid}"
    return Post(tweet_id=tid, author_handle=handle, author_name=handle,
                text=text, created_at=utcnow(),
                author_follower_count=1000, metrics=Metrics(likes=10))


def _queue(repo, tid, quote_score, safety_passed=True, age_hours=0,
           handle=None, text=None):
    repo.upsert_post(_post(tid, handle=handle, text=text))
    repo.save_score(Score(tweet_id=tid, quote_score=quote_score))
    # digit-free commentary: the publish-time re-vet runs the fabrication gate
    repo.add_draft(Draft(tweet_id=tid, commentary="a sharp growth take worth stealing",
                         model="test", safety_passed=safety_passed,
                         created_at=utcnow() - timedelta(hours=age_hours)))


class _FakePublisher:
    def __init__(self, fail_ids=()):
        self.fail_ids = set(fail_ids)
        self.published = []

    def publish(self, draft, post):
        if post.tweet_id in self.fail_ids:
            raise RuntimeError("403 simulated")
        self.published.append(post.tweet_id)
        return {"ok": True, "id": f"our_{post.tweet_id}"}


class _FakeJudge:
    """score_batch returns a fixed verdict dict; records what it was asked."""
    def __init__(self, verdicts=None):
        self.verdicts = verdicts or {}
        self.batches = []

    def score_batch(self, posts):
        self.batches.append([p.tweet_id for p in posts])
        return {tid: v for tid, v in self.verdicts.items()
                if tid in {p.tweet_id for p in posts}}


def _orch(tmp_path, repo, publisher=None, judge=None, **cfg_kw):
    orch = object.__new__(Orchestrator)  # skip __init__ (builds live adapters)
    orch.cfg = _cfg(tmp_path, **cfg_kw)
    orch.repo = repo
    orch.publisher = publisher or _FakePublisher()
    orch.judge = judge or _FakeJudge()
    orch.judge_reasons = {}
    return orch


# ---------------- publish flow ----------------

def test_posts_one_per_run_best_score_first(tmp_path):
    repo = _repo()
    _queue(repo, "1", quote_score=0.4)   # queued first (older)...
    _queue(repo, "2", quote_score=0.9)   # ...but this one ranks higher
    pub = _FakePublisher()
    result = _orch(tmp_path, repo, pub).publish_due()
    assert result["status"] == "posted"
    assert result["count"] == 1
    assert pub.published == ["2"]        # best first, FIFO order ignored
    assert repo.count_posted_today() == 1


def test_failure_skips_to_next_best(tmp_path):
    repo = _repo()
    _queue(repo, "1", quote_score=0.9)
    _queue(repo, "2", quote_score=0.5)
    pub = _FakePublisher(fail_ids={"1"})
    result = _orch(tmp_path, repo, pub).publish_due()
    assert result["status"] == "posted"
    assert pub.published == ["2"]        # best one failed -> next-best went out
    assert len(result["failed"]) == 1
    # the failed draft is marked, not retried forever
    assert [tid for tid, _, _ in repo.pending_drafts()] == []


def test_all_failed_reports_without_crashing(tmp_path):
    repo = _repo()
    _queue(repo, "1", quote_score=0.9)
    pub = _FakePublisher(fail_ids={"1"})
    result = _orch(tmp_path, repo, pub).publish_due()
    assert result["status"] == "all_failed"
    assert result["count"] == 0


def test_daily_cap_and_review_gate(tmp_path):
    repo = _repo()
    for tid in ("1", "2", "3", "4"):
        _queue(repo, tid, quote_score=0.5)
    pub = _FakePublisher()
    orch = _orch(tmp_path, repo, pub, per_run=5)  # per_run > remaining
    result = orch.publish_due()
    assert result["count"] == 3                   # per_day=3 still caps it
    assert orch.publish_due()["status"] == "cap_reached"

    gated = _orch(tmp_path, _repo(), pub, autonomous=False).publish_due()
    assert gated["status"] == "review_required"


def test_unsafe_draft_never_posts(tmp_path):
    repo = _repo()
    _queue(repo, "1", quote_score=0.9, safety_passed=False)
    pub = _FakePublisher()
    result = _orch(tmp_path, repo, pub).publish_due()
    assert result["status"] == "queue_empty"
    assert pub.published == []


def test_stale_draft_expires_instead_of_posting(tmp_path):
    repo = _repo()
    _queue(repo, "1", quote_score=0.9, age_hours=60)   # past 48h shelf life
    _queue(repo, "2", quote_score=0.3, age_hours=1)
    pub = _FakePublisher()
    result = _orch(tmp_path, repo, pub).publish_due()
    assert result["status"] == "posted"
    assert pub.published == ["2"]                      # stale winner never posts
    assert [tid for tid, _, _ in repo.pending_drafts()] == []


def test_expire_stale_drafts_repo_counts(tmp_path):
    repo = _repo()
    _queue(repo, "1", quote_score=0.5, age_hours=60)
    _queue(repo, "2", quote_score=0.5, age_hours=1)
    assert repo.expire_stale_drafts(48) == 1
    assert repo.expire_stale_drafts(48) == 0           # idempotent
    assert [p.tweet_id for _, _, p in repo.pending_drafts()] == ["2"]


# ---------------- publish-time re-checks ----------------

def test_same_author_not_posted_twice(tmp_path):
    repo = _repo()
    _queue(repo, "1", quote_score=0.9, handle="alice")
    _queue(repo, "2", quote_score=0.8, handle="alice")   # same author
    _queue(repo, "3", quote_score=0.5, handle="bob")
    pub = _FakePublisher()
    result = _orch(tmp_path, repo, pub, per_run=3).publish_due()
    # alice's second draft is skipped at publish time; bob's goes out instead
    assert pub.published == ["1", "3"]
    assert result["count"] == 2
    assert [tid for tid, _, _ in repo.pending_drafts()] == []


def test_same_idea_not_posted_twice(tmp_path):
    repo = _repo()
    same_idea = "reuse one winning AI image across accounts for millions of views"
    _queue(repo, "1", quote_score=0.9, handle="alice", text=same_idea)
    _queue(repo, "2", quote_score=0.8, handle="bob", text=same_idea + " period")
    pub = _FakePublisher()
    result = _orch(tmp_path, repo, pub, per_run=2).publish_due()
    assert pub.published == ["1"]          # the echoed twin is skipped
    assert result["count"] == 1


# ---------------- scoring: judge-once + no clobbering ----------------

def test_judge_outage_does_not_clobber_stored_verdicts(tmp_path):
    repo = _repo()
    repo.upsert_post(_post("1"))
    repo.save_score(Score(tweet_id="1", quote_worthy=0.8, topic_fit=1.0,
                          quote_score=0.7, judged=True))
    orch = _orch(tmp_path, repo, judge=_FakeJudge({}))   # judge returns nothing
    orch.score()
    s = repo.get_score("1")
    assert s.judged is True
    assert s.quote_worthy == 0.8          # stored verdict survived the re-score
    assert s.topic_fit == 1.0
    assert s.quote_score > 0.3            # not crushed by the topic gate


def test_judged_posts_are_not_rejudged(tmp_path):
    repo = _repo()
    repo.upsert_post(_post("1"))
    repo.upsert_post(_post("2"))
    judge = _FakeJudge({"1": (0.9, True, "solid tactic"),
                        "2": (0.2, True, "thin")})
    orch = _orch(tmp_path, repo, judge=judge)
    orch.score()
    assert sorted(judge.batches[0]) == ["1", "2"]   # first run judges both
    assert repo.get_score("1").judged is True
    orch.score()
    assert judge.batches[1] == []                    # second run re-judges nothing


# ---------------- collect circuit breaker ----------------

def test_monthly_read_budget_circuit_breaker(tmp_path):
    repo = _repo()
    repo.log_run("collect", read=50)
    orch = _orch(tmp_path, repo,
                 extra={"scoping": {"monthly_read_budget": 40}})
    assert orch.collect() == 0            # never touches the (absent) source
    runs = repo.recent_runs(1)
    assert any("circuit_breaker" in r["detail"] for r in runs)


# ---------------- supersede ----------------

def test_supersede_frees_slot_for_clearly_better_candidate(tmp_path):
    repo = _repo()
    for tid, qs in (("1", 0.50), ("2", 0.45), ("3", 0.40), ("4", 0.35)):
        _queue(repo, tid, quote_score=qs)            # queue full (target 4)
    orch = _orch(tmp_path, repo)
    new = _post("9")
    repo.upsert_post(new)
    eligible = [(Score(tweet_id="9", quote_score=0.9), new)]
    assert orch._maybe_supersede(eligible, drafted_ids=set()) == 1
    pending = [p.tweet_id for _, _, p in repo.pending_drafts()]
    assert "4" not in pending             # weakest retired
    # a merely-equal candidate does NOT supersede
    eligible = [(Score(tweet_id="9", quote_score=0.46), new)]
    assert orch._maybe_supersede(eligible, drafted_ids=set()) == 0


def test_blocked_posts_are_drafted_only_once(tmp_path):
    repo = _repo()
    repo.upsert_post(_post("1"))
    repo.add_draft(Draft(tweet_id="1", commentary="bad", model="test",
                         safety_passed=False), status="blocked")
    assert "1" in repo.drafted_tweet_ids()


# ---------------- vet / revision ----------------

class _FakeGenerator:
    def __init__(self, revised_text="short and sweet h/t @user1",
                 first_text="x" * 300):
        self.revised_text = revised_text
        self.first_text = first_text
        self.revisions = 0

    def generate(self, post, allow_thread=False):
        return Draft(tweet_id=post.tweet_id, commentary=self.first_text, model="fake")

    def revise(self, post, previous, feedback):
        self.revisions += 1
        return Draft(tweet_id=post.tweet_id, commentary=self.revised_text, model="fake")


def test_vet_revises_too_long_commentary_once(tmp_path):
    repo = _repo()
    orch = _orch(tmp_path, repo)
    orch.generator = _FakeGenerator()
    post = _post("1")
    draft, ok, notes = orch._vet_commentary(post, orch.generator.generate(post))
    assert ok is True
    assert orch.generator.revisions == 1
    assert draft.commentary == "short and sweet h/t @user1"


def test_vet_trims_when_revision_still_too_long(tmp_path):
    # too_long was 50% of all draft blocks — a still-too-long rewrite now gets a
    # deterministic trim instead of being thrown away.
    repo = _repo()
    orch = _orch(tmp_path, repo)
    orch.generator = _FakeGenerator(revised_text="y" * 300)
    post = _post("1")
    draft, ok, notes = orch._vet_commentary(post, orch.generator.generate(post))
    assert ok is True
    assert notes == "ok(trimmed)"
    assert len(draft.commentary) <= 280


def test_vet_blocks_when_revision_fails_for_non_length_reason(tmp_path):
    repo = _repo()
    orch = _orch(tmp_path, repo)
    # the rewrite invents a number that isn't in the source — not trimmable
    orch.generator = _FakeGenerator(revised_text="claims 99% growth h/t @user1")
    post = _post("1")
    draft, ok, notes = orch._vet_commentary(post, orch.generator.generate(post))
    assert ok is False
    assert notes.startswith("fabricated_number")


def test_make_drafts_skip_sentinel_blocks_without_commentary(tmp_path):
    repo = _repo()
    repo.upsert_post(_post("1"))
    judge = _FakeJudge({"1": (0.9, 1.0, "solid tactic")})
    orch = _orch(tmp_path, repo, judge=judge)
    orch.generator = _FakeGenerator(first_text="SKIP: cliffhanger, no lesson yet")
    created = orch.make_drafts()
    assert len(created) == 1
    assert created[0]["ok"] is False
    assert created[0]["notes"].startswith("no_material")
    assert "1" in repo.drafted_tweet_ids()          # one attempt per post, EVER
    assert repo.pending_drafts() == []              # never queued
    assert "1" in repo.candidates("skipped")


# ---------------- publish-time re-vet (the 2026-06-10 refusal incident) ----------------

def test_publish_revet_blocks_grandfathered_refusal(tmp_path):
    """A draft vetted before a gate existed must not post on its stale verdict:
    the refusal that published on Jun 10 was exactly this hole."""
    repo = _repo()
    refusal = ("This post doesn't contain enough tactical content to build a "
               "skill-share breakdown from. Drop the full thread and I'll turn "
               "it into a sharp breakdown.")
    repo.upsert_post(_post("1"))
    repo.save_score(Score(tweet_id="1", quote_score=0.9))
    repo.add_draft(Draft(tweet_id="1", commentary=refusal, model="test",
                         safety_passed=True))     # grandfathered: stamped ok
    pub = _FakePublisher()
    result = _orch(tmp_path, repo, pub).publish_due()
    assert pub.published == []
    assert result["status"] == "queue_empty"
    blocked = repo.activity_drafts(["blocked"], 1)
    assert blocked and blocked[0]["note"].startswith("revet:refusal_meta")


# ---------------- thread publishing chain ----------------

def test_dryrun_publisher_chains_parts_and_attribution(tmp_path):
    from xbot.publish.dryrun import DryRunPublisher
    cfg = NS({"posting": {"format": "mention", "attribution_reply": True,
                          "max_thread_parts": 3}})
    pub = DryRunPublisher(cfg)
    post = _post("1")
    post.url = "https://x.com/user1/status/1"
    draft = Draft(tweet_id="1", commentary="hook line h/t @user1", model="t",
                  parts=["step one and step two, then the takeaway"])
    res = pub.publish(draft, post)
    assert res["ok"] is True
    assert res["mode"] == "mention"
    assert len(res["thread_ids"]) == 2      # 1 content part + 1 attribution reply


def test_api_publisher_chain_stops_on_part_failure(tmp_path, monkeypatch):
    for k in ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET"):
        monkeypatch.setenv(k, "test-key")
    from xbot.publish.api_publisher import ApiPublisher
    cfg = NS({"posting": {"format": "mention", "attribution_reply": True}})
    pub = ApiPublisher(cfg)
    monkeypatch.setattr(pub, "_session", lambda: None)
    monkeypatch.setattr(pub, "_post",
                        lambda session, payload: {"id": "main-id"})

    def failing_reply(text, prev_id):
        raise RuntimeError("simulated part failure")
    monkeypatch.setattr(pub, "reply", failing_reply)

    post = _post("1")
    post.url = "https://x.com/user1/status/1"
    draft = Draft(tweet_id="1", commentary="hook line h/t @user1", model="t",
                  parts=["a step part"])
    res = pub.publish(draft, post)
    assert res["ok"] is True                # the hook stays up
    assert res["id"] == "main-id"
    assert res["thread_ids"] == []          # chain stopped, nothing deleted


# ---------------- report / activity / PT ----------------

def test_posted_times_stored_and_reported_in_pt(tmp_path):
    repo = _repo()
    repo.tz_name = "America/Los_Angeles"
    _queue(repo, "1", quote_score=0.9)
    orch = _orch(tmp_path, repo)
    orch.publish_due()

    row = repo.conn.execute(
        "SELECT posted_at, posted_at_pt FROM posted_log").fetchone()
    assert row["posted_at"].endswith("+00:00")
    assert row["posted_at_pt"].endswith(("-07:00", "-08:00"))  # PDT / PST

    entry = orch.report()["activity"]["posted"][0]
    assert entry["tz"] in ("PDT", "PST")
    assert entry["posted_at"].endswith(("-07:00", "-08:00"))


def test_run_log_aggregates_daily_counts(tmp_path):
    repo = _repo()
    _queue(repo, "1", quote_score=0.9)
    _queue(repo, "2", quote_score=0.5)
    pub = _FakePublisher(fail_ids={"1"})
    orch = _orch(tmp_path, repo, pub)
    orch.publish_due()                       # posts 1 (after one failure)
    repo.log_run("collect", read=50)
    repo.log_run("collect", read=31)         # second collect, same day
    repo.log_run("draft", judged=15, drafted=2)

    publish_runs = [r for r in repo.recent_runs(72) if r["kind"] == "publish"]
    assert "403 simulated" in publish_runs[0]["detail"]

    days = orch.report()["activity"]["days"]
    assert len(days) == 1
    assert days[0]["read"] == 81
    assert days[0]["judged"] == 15
    assert days[0]["drafted"] == 2
    assert days[0]["posted"] == 1


def test_report_activity_log(tmp_path):
    repo = _repo()
    _queue(repo, "1", quote_score=0.9)
    _queue(repo, "2", quote_score=0.5)
    pub = _FakePublisher(fail_ids={"1"})
    orch = _orch(tmp_path, repo, pub)
    orch.publish_due()  # "1" fails -> skip to "2", which posts

    activity = orch.report()["activity"]
    assert len(activity["posted"]) == 1
    assert activity["posted"][0]["url"] == "https://x.com/i/status/our_2"
    assert activity["posted"][0]["author"] == "user2"
    assert len(activity["problems"]) == 1
    assert activity["problems"][0]["status"] == "failed"
    assert "403 simulated" in activity["problems"][0]["note"]
