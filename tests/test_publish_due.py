"""publish_due: one post per window, best quote_score first, skip-on-failure,
stale-draft expiry."""
from datetime import timedelta

from xbot.config import NS
from xbot.models import Draft, Metrics, Post, Score, utcnow
from xbot.orchestrator import Orchestrator
from xbot.storage.sqlite_repo import SqliteRepository


def _cfg(tmp_path, autonomous=True, per_run=1):
    return NS({
        "mode": {"autonomous": autonomous},
        "posting": {"per_day": 3, "per_run": per_run},
        "ops": {"kill_switch_file": str(tmp_path / "STOP")},
    })


def _repo():
    repo = SqliteRepository(":memory:")
    repo.init_schema()
    return repo


def _post(tid, handle="alice"):
    return Post(tweet_id=tid, author_handle=handle, author_name=handle,
                text=f"post {tid} about growth tactics", created_at=utcnow(),
                author_follower_count=1000, metrics=Metrics(likes=10))


def _queue(repo, tid, quote_score, safety_passed=True, age_hours=0):
    repo.upsert_post(_post(tid))
    repo.save_score(Score(tweet_id=tid, quote_score=quote_score))
    repo.add_draft(Draft(tweet_id=tid, commentary=f"take on {tid}", model="test",
                         safety_passed=safety_passed,
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


def _orch(tmp_path, repo, publisher, **cfg_kw):
    orch = object.__new__(Orchestrator)  # skip __init__ (builds live adapters)
    orch.cfg = _cfg(tmp_path, **cfg_kw)
    orch.repo = repo
    orch.publisher = publisher
    return orch


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


def test_unsafe_draft_never_posts(tmp_path):
    repo = _repo()
    _queue(repo, "1", quote_score=0.9, safety_passed=False)
    pub = _FakePublisher()
    result = _orch(tmp_path, repo, pub).publish_due()
    assert result["status"] == "queue_empty"
    assert pub.published == []


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
    assert activity["posted"][0]["author"] == "alice"
    assert len(activity["problems"]) == 1
    assert activity["problems"][0]["status"] == "failed"
    assert "403 simulated" in activity["problems"][0]["note"]
