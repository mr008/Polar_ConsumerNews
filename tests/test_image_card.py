# tests/test_image_card.py
"""image_card experiment: self-rendered PNG attached to the main post, with a
plain-text fallback that is RECORDED as the text arm (spec §2)."""
import importlib.util
import io
import sys

import pytest

from xbot.config import NS
from xbot.models import Draft, Metrics, Post, Score, utcnow
from xbot.orchestrator import Orchestrator
from xbot.publish.card import H, W, card_lines, render_card
from xbot.storage.sqlite_repo import SqliteRepository

needs_pillow = pytest.mark.skipif(importlib.util.find_spec("PIL") is None,
                                  reason="Pillow (media extra) not installed")

BODY = ("A founder ran one ad on two platforms.\n\n"
        "He divided what he spent by the trials it brought in.\n\n"
        "Measure this before you spend more.\n\nh/t @adriamatz")


def test_card_lines_drop_tail_and_handle_lines():
    hook, rest = card_lines(BODY)
    assert hook == "A founder ran one ad on two platforms."
    assert rest == ["He divided what he spent by the trials it brought in.",
                    "Measure this before you spend more."]
    hook, rest = card_lines("Hook.\n\nDid it hold, @adriamatz?")
    assert rest == []


def _png_size(png: bytes):
    from PIL import Image
    return Image.open(io.BytesIO(png)).size


@needs_pillow
def test_render_card_is_1200x675_png():
    png = render_card(BODY, "polar")
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert _png_size(png) == (W, H)


@needs_pillow
def test_render_card_survives_empty_and_huge_text():
    assert _png_size(render_card("", "polar")) == (W, H)
    assert _png_size(render_card("word " * 400, "polar")) == (W, H)


CFG = NS({"posting": {"format": "mention", "per_day": 3, "per_run": 1,
                      "card_handle": "polar"},
          "llm": {"max_commentary_chars": 262}, "safety": {"exclude": []},
          "mode": {"autonomous": True}, "ranking": {"qa_gate": False},
          "experiments": {"image_card": {"enabled": True, "arms": ["text", "card"]}}})


class _Resp:
    def __init__(self, code, data):
        self.status_code, self._data, self.text = code, data, str(data)

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:  # like requests.HTTPError: carries .response
            err = RuntimeError(f"http {self.status_code}")
            err.response = self
            raise err


class _Session:
    """Fake OAuth1Session: records payloads; upload can be told to fail."""
    def __init__(self, upload_ok=True, media_post_ok=True):
        self.upload_ok, self.media_post_ok, self.calls = upload_ok, media_post_ok, []

    def post(self, url, json=None, files=None, data=None, timeout=None):
        self.calls.append({"url": url, "json": json, "files": files, "data": data})
        if url.endswith("/media/upload"):
            return _Resp(201, {"data": {"id": "m1"}}) if self.upload_ok \
                else _Resp(500, {"error": "upload down"})
        if json and "media" in json and not self.media_post_ok:
            return _Resp(400, {"error": "bad media"})
        return _Resp(201, {"data": {"id": "t1"}})


def _api_publisher(session):
    from xbot.publish.api_publisher import ApiPublisher
    pub = object.__new__(ApiPublisher)
    pub.cfg = CFG
    pub._session = lambda: session
    return pub


def _post():
    return Post(tweet_id="1", author_handle="adriamatz", author_name="A",
                text="one ad two platforms", created_at=utcnow(),
                author_follower_count=10, metrics=Metrics())


@needs_pillow
def test_api_publisher_attaches_media_in_card_arm():
    s = _Session()
    res = _api_publisher(s).publish(Draft(tweet_id="1", commentary=BODY, model="t",
                                          arms={"image_card": "card"}), _post())
    assert res["media"] is True and res["id"] == "t1"
    tweet_call = [c for c in s.calls if c["url"].endswith("/tweets")][0]
    assert tweet_call["json"]["media"] == {"media_ids": ["m1"]}
    upload_call = [c for c in s.calls if c["url"].endswith("/media/upload")][0]
    assert upload_call["data"] == {"media_category": "tweet_image"}


def test_api_publisher_falls_back_to_text_when_upload_fails():
    s = _Session(upload_ok=False)
    res = _api_publisher(s).publish(Draft(tweet_id="1", commentary=BODY, model="t",
                                          arms={"image_card": "card"}), _post())
    assert res["media"] is False and res["id"] == "t1"
    tweet_call = [c for c in s.calls if c["url"].endswith("/tweets")][0]
    assert "media" not in tweet_call["json"]


@needs_pillow
def test_api_publisher_retries_without_media_when_post_rejects_it():
    s = _Session(media_post_ok=False)
    res = _api_publisher(s).publish(Draft(tweet_id="1", commentary=BODY, model="t",
                                          arms={"image_card": "card"}), _post())
    assert res["media"] is False and res["id"] == "t1"
    tweet_calls = [c for c in s.calls if c["url"].endswith("/tweets")]
    assert len(tweet_calls) == 2 and "media" not in tweet_calls[1]["json"]


@needs_pillow
@pytest.mark.parametrize("exc", ["TimeoutError", "ConnectionError",
                                 "requests.Timeout", "requests.ConnectionError"])
def test_media_post_timeout_is_not_retried_as_text(exc):
    # X may have accepted the card post before the response was lost: a text
    # retry would be a duplicate (or a duplicate-403 that fails the draft).
    if exc.startswith("requests."):   # the CI test job installs no `x` extra
        err = getattr(pytest.importorskip("requests"), exc.split(".", 1)[1])
    else:
        err = {"TimeoutError": TimeoutError, "ConnectionError": ConnectionError}[exc]

    class _Lost(_Session):
        def post(self, url, json=None, files=None, data=None, timeout=None):
            if json and "media" in json:
                self.calls.append({"url": url, "json": json, "files": files, "data": data})
                raise err("read timed out")
            return super().post(url, json=json, files=files, data=data, timeout=timeout)

    s = _Lost()
    with pytest.raises(err):
        _api_publisher(s).publish(Draft(tweet_id="1", commentary=BODY, model="t",
                                        arms={"image_card": "card"}), _post())
    assert len([c for c in s.calls if c["url"].endswith("/tweets")]) == 1


@needs_pillow
def test_media_post_403_rejection_is_retried_as_text():
    class _Forbidden(_Session):
        def post(self, url, json=None, files=None, data=None, timeout=None):
            if json and "media" in json:
                self.calls.append({"url": url, "json": json, "files": files, "data": data})
                return _Resp(403, {"detail": "media not allowed here"})
            return super().post(url, json=json, files=files, data=data, timeout=timeout)

    s = _Forbidden()
    res = _api_publisher(s).publish(Draft(tweet_id="1", commentary=BODY, model="t",
                                          arms={"image_card": "card"}), _post())
    assert res["media"] is False and res["id"] == "t1"
    tweet_calls = [c for c in s.calls if c["url"].endswith("/tweets")]
    assert len(tweet_calls) == 2 and "media" not in tweet_calls[1]["json"]


def test_text_arm_never_uploads():
    s = _Session()
    _api_publisher(s).publish(Draft(tweet_id="1", commentary=BODY, model="t",
                                    arms={"image_card": "text"}), _post())
    assert not [c for c in s.calls if c["url"].endswith("/media/upload")]


@needs_pillow
@pytest.mark.parametrize("code", [401, 402, 403])
def test_upload_account_error_never_stops_publishing(code, capsys):
    # An upload-only auth failure (e.g. a rejected multipart signature) must
    # not stop the run: the post goes out as text, and a REAL account problem
    # surfaces from that text POST /2/tweets with the same credentials.
    class _Denied(_Session):
        def post(self, url, json=None, files=None, data=None, timeout=None):
            if url.endswith("/media/upload"):
                self.calls.append({"url": url, "json": json, "files": files, "data": data})
                return _Resp(code, {"error": "app permission denied"})
            return super().post(url, json=json, files=files, data=data, timeout=timeout)

    s = _Denied()
    res = _api_publisher(s).publish(Draft(tweet_id="1", commentary=BODY, model="t",
                                          arms={"image_card": "card"}), _post())
    assert res["media"] is False and res["id"] == "t1"
    tweet_calls = [c for c in s.calls if c["url"].endswith("/tweets")]
    assert len(tweet_calls) == 1 and "media" not in tweet_calls[0]["json"]
    out = capsys.readouterr().out
    assert "[publish] card skipped (" in out and "posting text" in out
    assert "from POST /2/media/upload" in out         # names the failing endpoint


CFG_OFF = NS({**CFG.as_dict(),
              "experiments": {"image_card": {"enabled": False, "arms": ["text", "card"]}}})


def test_card_arm_ignored_when_test_disabled():
    # Kill switch: turning the test off reaches card drafts already queued.
    s = _Session()
    pub = _api_publisher(s)
    pub.cfg = CFG_OFF
    res = pub.publish(Draft(tweet_id="1", commentary=BODY, model="t",
                            arms={"image_card": "card"}), _post())
    assert res["media"] is False
    assert not [c for c in s.calls if c["url"].endswith("/media/upload")]


def test_dry_run_card_arm_ignored_when_test_disabled(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from xbot.publish.dryrun import DryRunPublisher
    res = DryRunPublisher(CFG_OFF).publish(Draft(tweet_id="1", commentary=BODY, model="t",
                                                 arms={"image_card": "card"}), _post())
    assert res["media"] is False
    assert not list(tmp_path.glob("data/cards/*.png"))


def test_disabled_card_draft_records_text_in_features():
    repo = SqliteRepository(":memory:"); repo.init_schema()
    p = _post(); repo.upsert_post(p)
    repo.save_score(Score(tweet_id="1", quote_score=0.8, judged=True))
    repo.add_draft(Draft(tweet_id="1", commentary=BODY, model="t", safety_passed=True,
                         arms={"image_card": "card"}))
    s = _Session()
    pub = _api_publisher(s)
    pub.cfg = CFG_OFF
    o = object.__new__(Orchestrator)
    o.cfg, o.repo, o.publisher, o.judge_reasons = CFG_OFF, repo, pub, {}
    assert o.publish_due()["status"] == "posted"
    assert not [c for c in s.calls if c["url"].endswith("/media/upload")]
    row = repo.conn.execute("SELECT arms FROM post_features").fetchone()
    assert row["arms"] == '{"image_card": "text"}'


class _Pub:
    def __init__(self, media):
        self.media = media

    def publish(self, draft, post):
        return {"ok": True, "id": "our_1", "media": self.media}


def test_features_record_the_arm_actually_posted():
    repo = SqliteRepository(":memory:"); repo.init_schema()
    p = _post(); repo.upsert_post(p)
    repo.save_score(Score(tweet_id="1", quote_score=0.8, judged=True))
    repo.add_draft(Draft(tweet_id="1", commentary=BODY, model="t", safety_passed=True,
                         arms={"image_card": "card"}))
    o = object.__new__(Orchestrator)
    o.cfg, o.repo, o.publisher, o.judge_reasons = CFG, repo, _Pub(media=False), {}
    assert o.publish_due()["status"] == "posted"
    row = repo.conn.execute("SELECT arms FROM post_features").fetchone()
    assert row["arms"] == '{"image_card": "text"}'


@needs_pillow
def test_dry_run_writes_png_for_card_arm(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from xbot.publish.dryrun import DryRunPublisher
    res = DryRunPublisher(CFG).publish(Draft(tweet_id="1", commentary=BODY, model="t",
                                             arms={"image_card": "card"}), _post())
    assert res["media"] is True
    assert list((tmp_path / "data" / "cards").glob("*.png"))


def test_web_posts_alternate_for_image_card():
    """Web sources are exempt from author_bait alternation only; image_card
    counts them like any other post."""
    from xbot.experiments import assign_arms
    repo = SqliteRepository(":memory:"); repo.init_schema()
    seen = []
    for tid in ("web:aaa", "web:bbb"):
        web = Post(tweet_id=tid, author_handle="blog.com", author_name="Blog",
                   text="brief", created_at=utcnow())
        repo.upsert_post(web)
        arms = assign_arms(CFG, repo, web)
        seen.append(arms["image_card"])
        repo.add_draft(Draft(tweet_id=tid, commentary="x", model="t",
                             safety_passed=True, arms=arms))
    assert seen == ["text", "card"]


@pytest.fixture
def no_pillow(monkeypatch):
    """Make `from PIL import ...` raise ImportError even when Pillow is installed."""
    for mod in ("PIL", "PIL.Image", "PIL.ImageDraw", "PIL.ImageFont"):
        monkeypatch.setitem(sys.modules, mod, None)


def test_pillow_missing_api_publisher_posts_text(no_pillow, capsys):
    s = _Session()
    res = _api_publisher(s).publish(Draft(tweet_id="1", commentary=BODY, model="t",
                                          arms={"image_card": "card"}), _post())
    assert res["media"] is False and res["id"] == "t1"
    assert not [c for c in s.calls if c["url"].endswith("/media/upload")]
    tweet_calls = [c for c in s.calls if c["url"].endswith("/tweets")]
    assert len(tweet_calls) == 1 and "media" not in tweet_calls[0]["json"]
    assert "card skipped" in capsys.readouterr().out


def test_pillow_missing_dry_run_writes_no_png(no_pillow, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    from xbot.publish.dryrun import DryRunPublisher
    res = DryRunPublisher(CFG).publish(Draft(tweet_id="1", commentary=BODY, model="t",
                                             arms={"image_card": "card"}), _post())
    assert res["media"] is False
    assert not list(tmp_path.glob("data/cards/*.png"))
    assert "card skipped" in capsys.readouterr().out
