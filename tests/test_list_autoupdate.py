from datetime import timedelta

from xbot.config import NS
from xbot.models import Post, Score, utcnow
from xbot.orchestrator import Orchestrator
from xbot.storage.sqlite_repo import SqliteRepository


class _FakeListSource:
    """Stands in for ApiSourceAdapter's List-admin surface."""
    def __init__(self, members=None, ids=None):
        self.members = members or []          # [{id, handle}]
        self.ids = ids or {}                  # handle -> user_id
        self.added, self.removed = [], []
        self.created = None

    def list_members(self, list_id):
        return self.members

    def resolve_user_ids(self, handles):
        return {h: self.ids[h] for h in handles if h in self.ids}

    def create_list(self, name, private=True):
        self.created = name
        return "LIST1"

    def add_list_member(self, list_id, uid):
        self.added.append(uid); return True

    def remove_list_member(self, list_id, uid):
        self.removed.append(uid); return True


def _repo(tmp_path):
    r = SqliteRepository(str(tmp_path / "t.db"))
    r.init_schema()
    return r


def _seed(repo, handle, n, qw, days_ago=1, posted=0):
    base = utcnow() - timedelta(days=days_ago)
    for i in range(n):
        tid = f"{handle}{i}"
        repo.upsert_post(Post(tweet_id=tid, author_handle=handle, author_name=handle,
                              text="x", created_at=base))
        repo.save_score(Score(tweet_id=tid, quote_worthy=qw, judged=True))
    for i in range(posted):
        repo.conn.execute("INSERT INTO posted_log (source_tweet_id, author_handle) "
                          "VALUES (?,?)", (f"{handle}{i}", handle))
    repo.conn.commit()


def _orch(repo, source, list_id="LIST1"):
    o = object.__new__(Orchestrator)
    o.repo = repo
    o.source = source
    o.cfg = NS({"scoping": {"list_id": list_id, "source_timeline": "list",
                            "list_name": "feed"}, "listsync": {}})
    return o


def test_promote_demote_diff(tmp_path):
    repo = _repo(tmp_path)
    _seed(repo, "teacher", 6, 0.60)            # consistent teacher -> promote
    _seed(repo, "flexer", 8, 0.10)             # low avg -> ignore
    _seed(repo, "flood", 100, 0.40)            # high-volume, clears promote bar but flood-excluded
    _seed(repo, "drifter", 6, 0.10)            # member, drifted low -> demote
    _seed(repo, "quiet", 5, 0.70, days_ago=40) # member, went quiet -> demote
    _seed(repo, "keeper", 6, 0.55)             # member, still good -> kept
    src = _FakeListSource(members=[{"id": "1", "handle": "drifter"},
                                   {"id": "2", "handle": "quiet"},
                                   {"id": "3", "handle": "keeper"}])
    res = _orch(repo, src).sync_keep_list(apply=False)
    assert "teacher" in res["promote"]
    assert "flexer" not in res["promote"] and "flood" not in res["promote"]
    assert set(res["demote"]) == {"drifter", "quiet"}
    assert "keeper" not in res["demote"]


def test_apply_calls_api_and_logs(tmp_path):
    repo = _repo(tmp_path)
    _seed(repo, "teacher", 6, 0.60)
    _seed(repo, "drifter", 6, 0.10)
    src = _FakeListSource(members=[{"id": "9", "handle": "drifter"}],
                          ids={"teacher": "100"})
    res = _orch(repo, src).sync_keep_list(apply=True)
    assert res["status"] == "ok"
    assert src.added == ["100"]          # teacher promoted
    assert src.removed == ["9"]          # drifter demoted
    assert repo.get_state("last_list_sync", "").startswith("+1 -1")


def test_creates_list_when_none(tmp_path):
    repo = _repo(tmp_path)
    _seed(repo, "teacher", 6, 0.60)
    src = _FakeListSource(ids={"teacher": "100"})
    res = _orch(repo, src, list_id="").sync_keep_list(apply=True)
    assert res["created"] and res["list_id"] == "LIST1"
    assert src.created == "feed" and src.added == ["100"]


def test_thin_data_member_gets_grace(tmp_path):
    # A member with too few judged posts is NOT demoted (insufficient evidence).
    repo = _repo(tmp_path)
    _seed(repo, "newish", 2, 0.10)       # only 2 judged < min_judged
    src = _FakeListSource(members=[{"id": "1", "handle": "newish"}])
    res = _orch(repo, src).sync_keep_list(apply=False)
    assert "newish" not in res["demote"]


def test_own_handle_never_promoted(tmp_path):
    repo = _repo(tmp_path)
    repo.set_state("own_handle", "me")
    _seed(repo, "me", 6, 0.90)           # the bot's own account scores high
    src = _FakeListSource()
    res = _orch(repo, src).sync_keep_list(apply=False)
    assert "me" not in res["promote"]
