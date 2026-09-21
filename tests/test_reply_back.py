"""reply-back: answer people who reply to OUR posts. X's Feb-2026 policy blocks
programmatic replies unless the target's author engaged us first — a reply to
our post IS that engagement, so this is the one auto-reply path still open, and
the author-reply-back signal is the heaviest in the ranking."""
from datetime import timedelta

from xbot.config import NS
from xbot.models import Post, utcnow
from xbot.orchestrator import Orchestrator
from xbot.storage.sqlite_repo import SqliteRepository

OUR_UID = "999"
OUR_POST = "Cost per trial dropped almost 4x on TikTok ads with a fresh ad group."


def _cfg(tmp_path, **rb):
    reply_back = {"enabled": True, "dry_run": False, "max_per_day": 5,
                  "max_per_run": 2, "max_age_hours": 24, "max_reads_per_run": 20}
    reply_back.update(rb)
    return NS({"ops": {"kill_switch_file": str(tmp_path / "STOP")},
               "ranking": {"qa_gate": False},
               "replies": {"max_reply_chars": 240},
               "reply_back": reply_back})


def _mention(tid, text="how long did you let the old ad group run before moving?",
             handle="alice", author_id="1", to_uid=OUR_UID, age_hours=1,
             has_link=False):
    post = Post(tweet_id=tid, author_handle=handle, author_name=handle, text=text,
                created_at=utcnow() - timedelta(hours=age_hours), has_link=has_link)
    return {"post": post, "author_id": author_id,
            "in_reply_to_user_id": to_uid, "parent_text": OUR_POST}


class _FakeSource:
    uid = OUR_UID

    def __init__(self, items, newest="500"):
        self.items, self.newest, self.calls = items, newest, []

    def fetch_mentions(self, since_id=None, max_results=20):
        self.calls.append(since_id)
        return self.items, self.newest


class _FakeGen:
    def __init__(self, text="about a week, then we only moved the videos that held CPT"):
        self.text, self.seen = text, []

    def generate(self, post):
        self.seen.append(post.text)
        return self.text, "fake"

    def revise(self, post, previous, feedback):
        return self.text, "fake"


class _FakePublisher:
    def __init__(self):
        self.replies = []

    def reply(self, text, in_reply_to_tweet_id):
        self.replies.append(in_reply_to_tweet_id)
        return {"ok": True, "id": f"our_{in_reply_to_tweet_id}"}


def _orch(tmp_path, items, gen=None, **rb):
    repo = SqliteRepository(":memory:")
    repo.init_schema()
    orch = object.__new__(Orchestrator)
    orch.cfg = _cfg(tmp_path, **rb)
    orch.repo = repo
    orch.source = _FakeSource(items)
    orch.publisher = _FakePublisher()
    orch.reply_back_generator = gen or _FakeGen()
    return orch


def test_replies_only_to_fresh_human_replies_to_us(tmp_path):
    items = [
        _mention("10"),                                     # eligible
        _mention("11", to_uid="42"),                        # mention in someone else's thread
        _mention("12", author_id=OUR_UID, handle="us"),     # our own self-reply
        _mention("13", age_hours=30),                       # too old
        _mention("14", has_link=True, handle="spam"),       # link = spam bait
    ]
    orch = _orch(tmp_path, items)
    result = orch.reply_back()
    assert orch.publisher.replies == ["10"]
    assert result["status"] == "replied" and result["count"] == 1


def test_generator_sees_our_post_and_their_reply(tmp_path):
    gen = _FakeGen()
    orch = _orch(tmp_path, [_mention("10")], gen=gen)
    orch.reply_back()
    assert OUR_POST in gen.seen[0] and "old ad group" in gen.seen[0]


def test_never_replies_twice_and_advances_since_id(tmp_path):
    orch = _orch(tmp_path, [_mention("10")])
    orch.reply_back()
    assert orch.repo.get_state("mentions_since_id") == "500"
    orch.reply_back()                                   # same mention returned again
    assert orch.publisher.replies == ["10"]
    assert orch.source.calls == [None, "500"]


def test_one_reply_per_person_per_run(tmp_path):
    items = [_mention("10"), _mention("11", text="and what budget per ad group?")]
    orch = _orch(tmp_path, items)
    orch.reply_back()
    assert orch.publisher.replies == ["10"]


def test_skip_and_unsafe_replies_are_not_posted(tmp_path):
    orch = _orch(tmp_path, [_mention("10")], gen=_FakeGen("SKIP: just an emoji"))
    assert orch.reply_back()["count"] == 0
    orch = _orch(tmp_path, [_mention("10")],
                 gen=_FakeGen("we saw 73% lower costs doing exactly this"))  # invented number
    assert orch.reply_back()["count"] == 0
    assert orch.publisher.replies == []


def test_dry_run_disabled_and_kill_switch(tmp_path):
    orch = _orch(tmp_path, [_mention("10")], dry_run=True)
    result = orch.reply_back()
    assert result["count"] == 1 and orch.publisher.replies == []   # dry-run publisher used
    assert _orch(tmp_path, [_mention("10")], enabled=False).reply_back()["status"] == "disabled"
    (tmp_path / "STOP").write_text("")
    assert _orch(tmp_path, [_mention("10")]).reply_back()["status"] == "killed"


def test_no_mentions_capability_is_a_noop(tmp_path):
    orch = _orch(tmp_path, [])
    orch.source = object()                      # e.g. SampleSource
    assert orch.reply_back()["status"] == "no_source"
