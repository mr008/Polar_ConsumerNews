"""Phase 0 autonomy substrate: milestone windows, the outcome harvester,
publish-time feature tagging, and the agent usage governor (AUTONOMY.md)."""
from datetime import timedelta
from types import SimpleNamespace

from xbot.config import NS
from xbot.models import Draft, Metrics, Post, Score, utcnow
from xbot.orchestrator import Orchestrator
from xbot.outcomes import due_milestone
from xbot.storage.sqlite_repo import SqliteRepository


def _repo():
    repo = SqliteRepository(":memory:")
    repo.init_schema()
    return repo


def _orch(repo, source=None, cfg=None):
    orch = object.__new__(Orchestrator)  # skip __init__ (builds live adapters)
    orch.cfg = cfg or NS({"posting": {"per_day": 3, "per_run": 1},
                          "ranking": {"qa_gate": False},
                          "mode": {"autonomous": True},
                          "ops": {"kill_switch_file": "data/NOPE"}})
    orch.repo = repo
    orch.source = source if source is not None else SimpleNamespace()
    return orch


def _log_posted_at(repo, our_id, age_hours):
    posted = (utcnow() - timedelta(hours=age_hours)).isoformat()
    repo.conn.execute(
        "INSERT INTO posted_log (source_tweet_id, our_tweet_id, author_handle, "
        "source_text, commentary, posted_at, posted_at_pt) VALUES (?,?,?,?,?,?,?)",
        (f"src_{our_id}", our_id, "someone", "text", "commentary", posted, posted))
    repo.conn.commit()


class _FakeMetricsSource:
    """fetch_metrics look-alike; records what it was asked for."""
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def fetch_metrics(self, ids):
        self.calls.append(list(ids))
        if self.fail:
            raise RuntimeError("simulated API outage")
        return {tid: Metrics(likes=7, views=123) for tid in ids}


# ---------------- milestone windows ----------------

def test_due_milestone_windows():
    assert due_milestone(0.5, set()) is None            # too young for any window
    assert due_milestone(2.0, set()) == "1h"            # [1, 6)
    assert due_milestone(2.0, {"1h"}) is None           # already captured
    assert due_milestone(7.0, set()) == "6h"            # [6, 24)
    assert due_milestone(30.0, set()) == "24h"          # late deploy: NO 1h/6h backfill
    assert due_milestone(200.0, {"24h", "72h"}) == "7d"  # [168, inf)
    assert due_milestone(200.0, {"7d"}) is None


# ---------------- repo: outcomes ----------------

def test_log_outcome_idempotent_and_milestones():
    repo = _repo()
    repo.log_outcome("our_1", "1h", Metrics(likes=3, views=50))
    repo.log_outcome("our_1", "1h", Metrics(likes=99, views=9999))  # ignored
    repo.log_outcome("our_1", "24h", Metrics(likes=10))
    assert repo.outcome_milestones("our_1") == {"1h", "24h"}
    row = repo.conn.execute(
        "SELECT likes FROM post_outcomes WHERE our_tweet_id='our_1' "
        "AND milestone='1h'").fetchone()
    assert row["likes"] == 3                            # first capture survives


def test_posted_recent_skips_unharvestable_rows():
    repo = _repo()
    _log_posted_at(repo, "our_1", age_hours=2)
    _log_posted_at(repo, "", age_hours=2)               # dry-run row: no our id
    _log_posted_at(repo, "our_old", age_hours=24 * 20)  # past the harvest window
    rows = repo.posted_recent(within_days=8)
    assert [r["our_tweet_id"] for r in rows] == ["our_1"]


# ---------------- orchestrator.harvest ----------------

def test_harvest_captures_due_milestones():
    repo = _repo()
    _log_posted_at(repo, "our_2h", age_hours=2)         # due: 1h
    _log_posted_at(repo, "our_30h", age_hours=30)       # due: 24h (no backfill)
    _log_posted_at(repo, "our_fresh", age_hours=0.2)    # nothing due yet
    src = _FakeMetricsSource()
    res = _orch(repo, source=src).harvest()
    assert res["status"] == "ok"
    assert res["count"] == 2
    assert sorted(src.calls[0]) == ["our_2h", "our_30h"]
    assert repo.outcome_milestones("our_2h") == {"1h"}
    assert repo.outcome_milestones("our_30h") == {"24h"}
    # second run in the same window: nothing due anymore
    assert _orch(repo, source=src).harvest()["status"] == "nothing_due"


def test_harvest_fails_quiet_on_api_outage():
    repo = _repo()
    _log_posted_at(repo, "our_2h", age_hours=2)
    res = _orch(repo, source=_FakeMetricsSource(fail=True)).harvest()
    assert res["status"] == "fetch_failed"
    assert repo.outcome_milestones("our_2h") == set()   # retried next window
    assert any("fetch_failed" in r["detail"] for r in repo.recent_runs(1))


def test_harvest_unsupported_source_is_a_noop():
    res = _orch(_repo(), source=SimpleNamespace()).harvest()
    assert res["status"] == "unsupported_source"


def test_harvest_never_counts_against_read_breaker():
    repo = _repo()
    _log_posted_at(repo, "our_2h", age_hours=2)
    _orch(repo, source=_FakeMetricsSource()).harvest()
    assert repo.reads_this_month() == 0                 # owned reads aren't breaker reads


# ---------------- feature tagging at publish ----------------

class _FakePublisher:
    def publish(self, draft, post):
        return {"ok": True, "id": f"our_{post.tweet_id}"}


def _queue(repo, tid, qw=0.8):
    repo.upsert_post(Post(tweet_id=tid, author_handle=f"user{tid}",
                          author_name=f"user{tid}",
                          text=f"post {tid} growth tactic alpha{tid}",
                          created_at=utcnow(), author_follower_count=1000,
                          metrics=Metrics(likes=10)))
    repo.save_score(Score(tweet_id=tid, quote_worthy=qw, topic_fit=0.9,
                          quote_score=0.7, judged=True))
    repo.add_draft(Draft(tweet_id=tid, commentary="a sharp growth take. steal it?",
                         model="test", safety_passed=True))


def test_publish_tags_features():
    repo = _repo()
    repo.tz_name = "America/Los_Angeles"
    _queue(repo, "1")
    orch = _orch(repo)
    orch.publisher = _FakePublisher()
    assert orch.publish_due()["status"] == "posted"
    row = repo.conn.execute("SELECT * FROM post_features").fetchone()
    assert row["our_tweet_id"] == "our_1"
    assert row["route"] == "pipeline"
    assert row["kind"] == "qt"
    assert row["format"] == "single"
    assert row["has_question"] == 1
    assert row["teaching"] == 0.8
    assert row["hook"].startswith("a sharp growth take")
    assert 0 <= row["window_hour"] <= 23


def test_feature_tagging_failure_never_breaks_publishing(monkeypatch):
    repo = _repo()
    _queue(repo, "1")
    orch = _orch(repo)
    orch.publisher = _FakePublisher()
    monkeypatch.setattr(repo, "log_features",
                        lambda f: (_ for _ in ()).throw(RuntimeError("boom")))
    result = orch.publish_due()
    assert result["status"] == "posted"                 # the post still went out
    assert repo.count_posted_today() == 1


# ---------------- agent usage governor + runner ----------------

def test_agent_usage_ledger_and_governor():
    from xbot.agents import governor_allows
    repo = _repo()
    assert governor_allows(repo, ceiling=10) == (True, 0, 10)
    repo.log_agent_usage("smoke", "default", turns=6, cost_usd=0.0)
    repo.log_agent_usage("curator", "sonnet", turns=4, input_tokens=100,
                         output_tokens=50)
    assert repo.agent_turns_today() == 10
    allowed, used, cap = governor_allows(repo, ceiling=10)
    assert not allowed and used == 10                   # at ceiling => skip


def test_parse_claude_json():
    from xbot.agents import parse_claude_json
    good = ('{"is_error": false, "result": "per_day=3", "num_turns": 2, '
            '"total_cost_usd": 0.01, "usage": {"input_tokens": 900, '
            '"output_tokens": 40}, "session_id": "s1"}')
    p = parse_claude_json(good)
    assert p["ok"] and p["turns"] == 2 and p["input_tokens"] == 900
    bad = parse_claude_json("not json at all")
    assert bad["ok"] is False and bad["error"] == "unparseable_output"


def test_run_session_skips_over_governor_and_runs_under_it(monkeypatch):
    from xbot.agents import runner
    repo = _repo()
    repo.log_agent_usage("earlier", "default", turns=60)
    res = runner.run_session("smoke", "hi", repo, ceiling=60)
    assert res["status"] == "skipped_governor"          # no subprocess launched

    calls = {}
    def fake_run(cmd, **kw):
        calls["cmd"] = cmd
        return SimpleNamespace(returncode=0, stderr="", stdout=(
            '{"is_error": false, "result": "ok", "num_turns": 3, '
            '"total_cost_usd": 0.0, "usage": {"input_tokens": 5, '
            '"output_tokens": 5}}'))
    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    res = runner.run_session("smoke", "hi", repo, ceiling=100, max_turns=6)
    assert res["status"] == "ok"
    assert "--max-turns" in calls["cmd"] and "6" in calls["cmd"]
    assert repo.agent_turns_today() == 63               # 60 earlier + 3 now


def test_run_session_flags_auth_errors(monkeypatch):
    from xbot.agents import runner
    repo = _repo()
    monkeypatch.setattr(runner.subprocess, "run", lambda cmd, **kw: SimpleNamespace(
        returncode=1, stderr="401 authentication_error: OAuth token revoked",
        stdout=""))
    res = runner.run_session("smoke", "hi", repo, ceiling=100)
    assert res["status"] == "auth_error"                # the Mechanic's incident signal
