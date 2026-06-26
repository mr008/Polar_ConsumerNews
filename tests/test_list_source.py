from xbot.ingest.api_source import ApiSourceAdapter
from xbot.models import Post, Score, utcnow
from xbot.storage.sqlite_repo import SqliteRepository

# ---------------- List source: client-side early-stop dedup ----------------

class _Resp:
    def __init__(self, payload): self._p = payload
    def raise_for_status(self): pass
    def json(self): return self._p

class _FakeSession:
    def __init__(self, pages): self.pages = pages; self.calls = []
    def get(self, url, params=None, timeout=None):
        self.calls.append(dict(params or {}))
        token = (params or {}).get("pagination_token")
        return _Resp(self.pages[token] if token in self.pages else self.pages[None])

def _adapter(monkeypatch, list_id="9", page_size=25, pages=None):
    for k in ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN",
              "X_ACCESS_TOKEN_SECRET", "X_USER_ID"):
        monkeypatch.setenv(k, "x")
    a = ApiSourceAdapter(max_posts_per_day=100, list_id=list_id, list_page_size=page_size)
    fake = _FakeSession(pages or {})
    a._session = lambda: fake
    return a, fake

def _payload(ids, next_token=None):
    p = {"data": [{"id": str(i), "text": f"post {i}", "author_id": "u1",
                   "created_at": "2026-06-25T00:00:00Z", "public_metrics": {}} for i in ids],
         "includes": {"users": [{"id": "u1", "username": "a", "name": "A",
                                 "public_metrics": {"followers_count": 10}}]}}
    if next_token:
        p["meta"] = {"next_token": next_token}
    return p


def test_list_stops_at_first_seen_tweet(monkeypatch):
    a, _ = _adapter(monkeypatch, pages={None: _payload([105, 104, 103, 102, 101])})
    posts = a.fetch_timeline(limit=100, since_id="102")
    assert [p.tweet_id for p in posts] == ["105", "104", "103"]  # 102/101 are seen


def test_list_paginates_until_seen(monkeypatch):
    pages = {None: _payload([110, 109, 108], next_token="t2"),
             "t2": _payload([107, 106, 105])}
    a, fake = _adapter(monkeypatch, page_size=3, pages=pages)
    posts = a.fetch_timeline(limit=100, since_id="106")
    assert [p.tweet_id for p in posts] == ["110", "109", "108", "107"]
    assert len(fake.calls) == 2  # needed page 2 to reach the seen boundary


def test_list_first_run_no_since_id(monkeypatch):
    a, _ = _adapter(monkeypatch, pages={None: _payload([5, 4, 3, 2, 1])})
    posts = a.fetch_timeline(limit=3, since_id=None)
    assert [p.tweet_id for p in posts] == ["5", "4", "3"]  # capped by limit


def test_home_path_when_no_list_id(monkeypatch):
    a, fake = _adapter(monkeypatch, list_id="", pages={None: _payload([9, 8])})
    posts = a.fetch_timeline(limit=10, since_id=None)
    assert [p.tweet_id for p in posts] == ["9", "8"]
    assert "since_id" not in fake.calls[0]  # home, no since_id this run


# ---------------- author_yield (drives list-sync) ----------------

def test_author_yield(tmp_path):
    repo = SqliteRepository(str(tmp_path / "t.db"))
    repo.init_schema()
    repo.upsert_post(Post(tweet_id="1", author_handle="alice", author_name="A",
                          text="x", created_at=utcnow()))
    repo.upsert_post(Post(tweet_id="2", author_handle="bob", author_name="B",
                          text="y", created_at=utcnow()))
    repo.save_score(Score(tweet_id="1", quote_worthy=0.8, judged=True))
    repo.save_score(Score(tweet_id="2", quote_worthy=0.2, judged=True))
    repo.conn.execute("INSERT INTO posted_log (source_tweet_id, author_handle) "
                      "VALUES ('1', 'alice')")
    repo.conn.commit()
    y = {a["handle"]: a for a in repo.author_yield()}
    assert y["alice"]["posted"] == 1 and y["alice"]["max_qw"] == 0.8
    assert y["bob"]["posted"] == 0 and y["bob"]["max_qw"] == 0.2
    assert y["alice"]["reads"] == 1
