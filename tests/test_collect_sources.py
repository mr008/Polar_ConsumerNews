"""source_timeline: both — the List then the home feed, each with its own
since_id watermark, sharing one per-run read ceiling."""
from xbot.config import NS
from xbot.models import Metrics, Post, utcnow
from xbot.orchestrator import Orchestrator
from xbot.storage.sqlite_repo import SqliteRepository


def _post(tid):
    return Post(tweet_id=tid, author_handle=f"u{tid}", author_name=f"u{tid}",
                text=f"tactic {tid}", created_at=utcnow(),
                author_follower_count=5000, metrics=Metrics(likes=5))


class FakeSource:
    def __init__(self, feeds):
        self.feeds = feeds          # source key -> tweet ids it returns
        self.calls = []             # (source, limit, since_id)

    def fetch_timeline(self, limit=120, since_id=None, source=None):
        self.calls.append((source, limit, since_id))
        return [_post(t) for t in self.feeds[source][:limit]]


def _orch(feeds, timeline, cap=50):
    repo = SqliteRepository(":memory:")
    repo.init_schema()
    o = object.__new__(Orchestrator)
    o.cfg = NS({"scoping": {"source_timeline": timeline, "max_posts_per_day": cap,
                            "monthly_read_budget": 0}})
    o.repo = repo
    o.source = FakeSource(feeds)
    return o


def test_both_reads_list_then_home_with_own_watermarks():
    o = _orch({"list": ["300", "200"], "home": ["500", "400"]}, "both")
    o.repo.set_state("since_id:list", "100")
    o.repo.upsert_post(_post("150"))     # newest stored before the run
    assert o.collect() == 4
    # List first; home has no watermark yet, so it falls back to the newest post
    # stored BEFORE this run — not the List posts just stored.
    assert o.source.calls == [("list", 50, "100"), ("home", 48, "150")]
    assert o.repo.get_state("since_id:list") == "300"
    assert o.repo.get_state("since_id:home") == "500"


def test_both_shares_one_per_run_ceiling():
    o = _orch({"list": ["3", "2", "1"], "home": ["9"]}, "both", cap=3)
    assert o.collect() == 3
    assert [c[0] for c in o.source.calls] == ["list"]   # ceiling hit; home skipped


def test_single_source_modes_unchanged():
    o = _orch({"list": ["2"], "home": ["9"]}, "list")
    o.collect()
    assert [c[0] for c in o.source.calls] == ["list"]
    o = _orch({"list": ["2"], "home": ["9"]}, "home")
    o.collect()
    assert [c[0] for c in o.source.calls] == ["home"]
