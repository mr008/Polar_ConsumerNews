from xbot.config import NS
from xbot.commentary.prescreen import LLMPrescreen, get_prescreen
from xbot.models import Post, utcnow


def _post(text, has_media=False):
    return Post(tweet_id="1", author_handle="x", author_name="X", text=text,
                created_at=utcnow(), has_media=has_media)


def _screen(monkeypatch_answer):
    s = LLMPrescreen(NS({"ranking": {"prescreen_model": "m"}}), "groq")
    s._call = lambda text, has_media: monkeypatch_answer  # bypass the network
    return s


def test_no_rejects():
    assert _screen("NO").has_material(_post("RT @x: huge milestone …")) is False


def test_no_with_trailing_text_still_rejects():
    assert _screen("NO.").has_material(_post("just hit $1M, insane")) is False
    assert _screen("no").has_material(_post("teaser, link in bio")) is False


def test_yes_drafts():
    assert _screen("YES").has_material(_post("reuse one AI image across 50 accounts")) is True


def test_ambiguous_or_blank_fails_open():
    # Anything that isn't a decisive NO must draft (never lose a post to noise).
    assert _screen("").has_material(_post("some post")) is True
    assert _screen("maybe?").has_material(_post("some post")) is True


def test_exception_fails_open():
    s = LLMPrescreen(NS({"ranking": {}}), "groq")
    def boom(text, has_media):
        raise RuntimeError("api down")
    s._call = boom
    assert s.has_material(_post("anything")) is True


def test_get_prescreen_disabled_returns_none():
    assert get_prescreen(NS({"ranking": {"draft_prescreen": False}})) is None


def test_get_prescreen_enabled_but_no_key_returns_none(monkeypatch):
    for k in ("ANTHROPIC_API_KEY", "GROQ_API_KEY", "XAI_API_KEY",
              "GEMINI_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    cfg = NS({"ranking": {"draft_prescreen": True}, "llm": {"provider": "auto"}})
    assert get_prescreen(cfg) is None


def test_get_prescreen_enabled_with_key(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "x")
    cfg = NS({"ranking": {"draft_prescreen": True}, "llm": {"provider": "groq"}})
    assert get_prescreen(cfg) is not None
