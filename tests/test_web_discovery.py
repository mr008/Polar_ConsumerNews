from datetime import timedelta

import xbot.ingest.web_discovery as wd
from xbot.config import NS
from xbot.ingest.web_discovery import extract_handles, search_handles
from xbot.models import Post, Score, utcnow
from xbot.orchestrator import Orchestrator
from xbot.storage.sqlite_repo import SqliteRepository


# ------------------------- pure handle extraction -------------------------

def test_extract_handles_urls_mentions_dedup_and_junk():
    results = [
        {"url": "https://x.com/levelsio", "title": "Pieter Levels", "description": "indie hacker"},
        {"url": "https://twitter.com/gregisenberg/status/123", "title": "", "description": ""},
        {"url": "https://example.com/blog",
         "title": "Follow @dickiebush and @Nicolascole77",
         "description": "top <strong>writers</strong>"},
        {"url": "https://x.com/search?q=ai", "title": "see x.com/home", "description": ""},
        {"url": "https://x.com/thishandleiswaytoolongtobevalid", "title": "", "description": ""},
        {"url": "https://x.com/levelsio", "title": "dup profile", "description": ""},
    ]
    # URLs + @mentions, lowercased, order-preserving, deduped; reserved paths
    # (search/home) and the >15-char handle dropped.
    assert extract_handles(results) == ["levelsio", "gregisenberg", "dickiebush", "nicolascole77"]


def test_extract_handles_drops_generic_site_paths():
    # x.com/blog, x.com/news etc. are site sections, not accounts.
    results = [
        {"url": "https://x.com/blog", "title": "", "description": ""},
        {"url": "https://twitter.com/news", "title": "", "description": ""},
        {"url": "https://x.com/rileybrown", "title": "", "description": ""},
    ]
    assert extract_handles(results) == ["rileybrown"]


def test_extract_handles_empty():
    assert extract_handles([]) == []
    assert extract_handles([{"url": "https://example.com", "title": "no handles here"}]) == []


# ------------------------- search_handles orchestration -------------------------

def test_search_handles_dedups_across_queries_and_survives_failure(monkeypatch):
    calls = []

    def fake(query, api_key, count, timeout):
        calls.append(query)
        if "boom" in query:
            raise RuntimeError("rate limited")
        handle = "alice" if "alpha" in query else "bob"
        return [{"url": f"https://x.com/{handle}", "title": "", "description": ""}]

    monkeypatch.setitem(wd._PROVIDERS, "brave", fake)
    out = search_handles(["alpha q", "boom q", "beta q"], "key", per_query=5)
    assert set(out) == {"alice", "bob"}      # both non-failing queries contributed
    assert len(calls) == 3                    # the failing query didn't abort the loop


def test_search_handles_no_key_or_queries():
    assert search_handles(["q"], "") == []
    assert search_handles([], "key") == []


def test_search_handles_unknown_provider(monkeypatch):
    assert search_handles(["q"], "key", provider="nope") == []


# ------------------------- You.com provider adapter -------------------------

def test_you_provider_maps_response_and_normalizes_snippets(monkeypatch):
    import pytest
    httpx = pytest.importorskip("httpx")
    payload = {"results": {"web": [
        {"url": "https://x.com/levelsio", "title": "Levels",
         "description": "indie hacker", "snippets": ["ships fast", "12 startups"]},
        {"url": "https://ex.com/a", "title": "A", "description": "d"},  # no snippets
    ]}}

    class _Resp:
        def raise_for_status(self): pass
        def json(self): return payload

    sent = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        sent.update(url=url, params=params, headers=headers)
        return _Resp()

    monkeypatch.setattr(httpx, "get", fake_get)

    out = wd._you_search("who to follow", "KEY", 10, 20.0)
    # You's `snippets` is normalized to our `extra_snippets`; missing -> []
    assert out == [
        {"url": "https://x.com/levelsio", "title": "Levels",
         "description": "indie hacker", "extra_snippets": ["ships fast", "12 startups"]},
        {"url": "https://ex.com/a", "title": "A", "description": "d", "extra_snippets": []},
    ]
    # correct endpoint + auth header + query param name
    assert sent["url"] == "https://ydc-index.io/v1/search"
    assert sent["headers"]["X-API-Key"] == "KEY"
    assert sent["params"]["query"] == "who to follow"
    # registered so both discovery and blog-content can select it
    assert wd._PROVIDERS.get("you") is wd._you_search


def test_you_search_survives_empty_results(monkeypatch):
    import pytest
    httpx = pytest.importorskip("httpx")

    class _Resp:
        def raise_for_status(self): pass
        def json(self): return {}          # no `results` key at all

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp())
    assert wd._you_search("q", "KEY", 5, 20.0) == []


# ------------------------- web discovery sweep (orchestrator) -------------------------

class _FakeSource:
    def __init__(self, ids, members=None):
        self.ids = ids                        # handle -> user_id (resolvable only)
        self.members = members or []          # current List members
        self.recent_calls = []                # uids we paid to vet

    def list_members(self, list_id):
        return self.members

    def resolve_user_ids(self, handles):
        return {h: self.ids[h] for h in handles if h in self.ids}

    def fetch_user_recent(self, user_id, max_posts=5):
        self.recent_calls.append(user_id)
        posts = [Post(tweet_id=f"{user_id}_{i}", author_handle=f"h{user_id}",
                      author_name="x", text="teaching", created_at=utcnow())
                 for i in range(3)]
        return posts, 3                       # 3 posts, 3 billed reads


def _repo(tmp_path):
    r = SqliteRepository(str(tmp_path / "t.db"))
    r.init_schema()
    return r


def _seed_scored(repo, handle, n=4, qw=0.5):
    base = utcnow() - timedelta(days=1)
    for i in range(n):
        tid = f"{handle}{i}"
        repo.upsert_post(Post(tweet_id=tid, author_handle=handle, author_name=handle,
                              text="x", created_at=base))
        repo.save_score(Score(tweet_id=tid, quote_worthy=qw, judged=True))
    repo.conn.commit()


def _orch(repo, source, listsync_over=None):
    o = object.__new__(Orchestrator)
    o.repo = repo
    o.source = source
    listsync = {"discovery_mode": "web", "web_queries": ["q"], "web_max_new": 2,
                "web_vet_posts_per_author": 5, "web_vet_max_reads": 100}
    listsync.update(listsync_over or {})
    o.cfg = NS({"scoping": {"list_id": "LIST1", "monthly_read_budget": 0},
                "listsync": listsync})
    o.score = lambda: ([], [])                # don't invoke the real judge in a unit test
    return o


def test_web_sweep_vets_only_new_capped_handles(tmp_path, monkeypatch):
    monkeypatch.setenv("SEARCH_API_KEY", "key")
    repo = _repo(tmp_path)
    _seed_scored(repo, "known1")              # already scored -> deduped out
    src = _FakeSource(ids={"new1": "10", "new2": "20", "new3": "30"},
                      members=[{"id": "9", "handle": "member1"}])  # on List -> deduped
    monkeypatch.setattr(wd, "search_handles",
                        lambda *a, **k: ["known1", "member1", "new1", "new2", "new3"])
    n = _orch(repo, src).discovery_sweep()
    assert src.recent_calls == ["10", "20"]   # known1/member1 skipped; new3 over web_max_new=2
    assert n == 6                             # 2 candidates * 3 posts


def test_web_sweep_respects_vet_read_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("SEARCH_API_KEY", "key")
    repo = _repo(tmp_path)
    src = _FakeSource(ids={"a": "1", "b": "2", "c": "3"})
    monkeypatch.setattr(wd, "search_handles", lambda *a, **k: ["a", "b", "c"])
    n = _orch(repo, src, {"web_max_new": 5, "web_vet_max_reads": 4}).discovery_sweep()
    assert src.recent_calls == ["1", "2"]     # after 6 reads (>=4) the 3rd is blocked
    assert n == 6


def test_web_sweep_skips_without_api_key(tmp_path, monkeypatch):
    monkeypatch.delenv("SEARCH_API_KEY", raising=False)
    repo = _repo(tmp_path)
    src = _FakeSource(ids={"a": "1"})
    monkeypatch.setattr(wd, "search_handles", lambda *a, **k: ["a"])
    assert _orch(repo, src).discovery_sweep() == 0
    assert src.recent_calls == []             # no key -> no billed reads


def test_home_mode_still_dispatches_to_home_sweep(tmp_path, monkeypatch):
    # discovery_mode: home must NOT touch the web path.
    repo = _repo(tmp_path)
    src = _FakeSource(ids={})
    called = {"web": False}
    o = _orch(repo, src, {"discovery_mode": "home"})
    monkeypatch.setattr(o, "_web_discovery_sweep",
                        lambda: called.__setitem__("web", True) or 0)
    o._home_discovery_sweep = lambda: 42
    assert o.discovery_sweep() == 42
    assert called["web"] is False
