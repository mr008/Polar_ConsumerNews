import xbot.ingest.web_articles as wa
from xbot.config import NS
from xbot.models import Draft, Post, is_web_source, utcnow
from xbot.orchestrator import Orchestrator
from xbot.publish.publisher import body_budget, compose_text
from xbot.storage.sqlite_repo import SqliteRepository


# ------------------------- pure helpers -------------------------

def test_skip_filters_non_article_domains():
    assert wa._skip("https://youtube.com/watch?v=x")
    assert wa._skip("https://x.com/foo")
    assert wa._skip("https://collabstr.com/top")
    assert not wa._skip("https://magichour.ai/blog/ugc")
    assert not wa._skip("https://usegavel.com/playbooks/scale-a-consumer-app")


def test_web_id_deterministic_and_prefixed():
    a = wa.web_id("https://example.com/x")
    assert a == wa.web_id("https://example.com/x")
    assert a != wa.web_id("https://example.com/y")
    assert a.startswith("web:")


def test_to_web_post_shape():
    p = wa.to_web_post("https://magichour.ai/blog/ugc", "brief text")
    assert is_web_source(p)
    assert p.author_handle == "magichour.ai" and p.author_name == "magichour.ai"
    assert p.url == "https://magichour.ai/blog/ugc" and p.text == "brief text"


def test_find_article_candidates_dedups_and_drops_junk(monkeypatch):
    def fake(q, key, count, timeout):
        return [
            {"url": "https://magichour.ai/blog/a", "title": "A", "description": "d"},
            {"url": "https://youtube.com/x", "title": "vid", "description": "d"},  # junk
            {"url": "https://magichour.ai/blog/a", "title": "dup", "description": "d"},
        ]
    monkeypatch.setitem(wa.wd._PROVIDERS, "brave", fake)
    out = wa.find_article_candidates(["q1", "q2"], "key", per_query=10)
    urls = [u for u, _t, _b in out]
    assert urls == ["https://magichour.ai/blog/a"]   # junk dropped, deduped across queries


# ------------------------- publisher: web post = original, no @h/t -------------------------

def test_web_post_composes_without_ht_tail():
    post = wa.to_web_post("https://magichour.ai/blog/ugc", "src")
    draft = Draft(tweet_id=post.tweet_id, commentary="Fix the angle first.\n\n• do x",
                  model="t")
    text, fmt = compose_text(draft, post, NS({"posting": {"format": "mention"}}))
    assert fmt == "mention"
    assert "h/t" not in text and "@" not in text
    assert body_budget(post, NS({"posting": {"format": "mention"}})) == 278


def test_web_post_strips_model_added_ht():
    post = wa.to_web_post("https://blog.dev/x", "src")
    draft = Draft(tweet_id=post.tweet_id, commentary="Teach it.\n\nh/t @blog", model="t")
    text, _ = compose_text(draft, post, NS({"posting": {"format": "mention"}}))
    assert "h/t" not in text


# ------------------------- orchestrator collect_web flow -------------------------

def _repo(tmp_path):
    r = SqliteRepository(str(tmp_path / "t.db"))
    r.init_schema()
    return r


def _orch(repo, over=None):
    o = object.__new__(Orchestrator)
    o.repo = repo
    wc = {"enabled": True, "queries": ["q"], "provider": "brave",
          "results_per_query": 10, "max_new_per_run": 2, "min_article_chars": 400}
    wc.update(over or {})
    o.cfg = NS({"webcontent": wc})
    o.score = lambda: ([], [])
    return o


def test_collect_web_stores_capped_new_summaries(tmp_path, monkeypatch):
    monkeypatch.setenv("SEARCH_API_KEY", "key")
    repo = _repo(tmp_path)
    monkeypatch.setattr(wa, "find_article_candidates", lambda *a, **k: [
        ("https://b.dev/1", "t1", "b1"),
        ("https://b.dev/2", "t2", "b2"),
        ("https://b.dev/3", "t3", "b3"),
    ])
    # article 2 summarizes to SKIP (None) -> excluded; cap is 2 new
    briefs = {"https://b.dev/1": "brief one", "https://b.dev/2": None,
              "https://b.dev/3": "brief three"}
    monkeypatch.setattr(wa, "summarize_article",
                        lambda title, blurb, url, cfg, min_chars=400: briefs[url])
    n = _orch(repo).collect_web()
    assert n == 2                                   # 1 and 3 kept, 2 skipped
    assert repo.get_post(wa.web_id("https://b.dev/1")) is not None
    assert repo.get_post(wa.web_id("https://b.dev/2")) is None


def test_collect_web_skips_already_seen(tmp_path, monkeypatch):
    monkeypatch.setenv("SEARCH_API_KEY", "key")
    repo = _repo(tmp_path)
    repo.upsert_post(wa.to_web_post("https://b.dev/1", "already here"))  # pre-existing
    monkeypatch.setattr(wa, "find_article_candidates", lambda *a, **k:
                        [("https://b.dev/1", "t", "b")])
    called = []
    monkeypatch.setattr(wa, "summarize_article",
                        lambda *a, **k: called.append(1) or "new")
    n = _orch(repo).collect_web()
    assert n == 0 and called == []                  # deduped BEFORE the paid summarize


def test_collect_web_disabled_is_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("SEARCH_API_KEY", "key")
    assert _orch(_repo(tmp_path), {"enabled": False}).collect_web() == 0


def test_collect_web_no_key_skips(tmp_path, monkeypatch):
    monkeypatch.delenv("SEARCH_API_KEY", raising=False)
    assert _orch(_repo(tmp_path)).collect_web() == 0
