"""Auto-reply engine: target selection, caps/cooldowns, kill switch, dry-run
path, SKIP sentinel, gate-block logging, and run_log accounting. Uses the same
object.__new__(Orchestrator) + in-memory repo conventions as test_publish_due."""
from datetime import timedelta

from xbot.config import NS
from xbot.models import Metrics, Post, Score, utcnow
from xbot.orchestrator import Orchestrator
from xbot.select.reply_targets import select_reply_targets
from xbot.storage.sqlite_repo import SqliteRepository

SOURCE_TEXT = ("faceless ugc react videos with ai are printing views — post 4x "
               "daily, kill losers at 24h, volume beats polish every time")
GOOD_REPLY = ("the kill-losers-at-24h rule is the underrated part — did you find "
              "24h was enough signal for react formats specifically?")


def _cfg(tmp_path, **replies_overrides):
    replies = {
        "enabled": True, "dry_run": True, "max_per_day": 6, "max_per_run": 1,
        "min_minutes_between": 45, "author_cooldown_days": 3,
        "min_author_followers": 5000, "max_target_age_minutes": 180,
        "min_topic_fit": 0.45, "min_teaching": 0.2, "max_reply_chars": 240,
    }
    replies.update(replies_overrides)
    return NS({
        "mode": {"autonomous": True},
        "ops": {"kill_switch_file": str(tmp_path / "STOP")},
        "ranking": {"qa_gate": False},
        "replies": replies,
        "scoping": {"languages": ["en"]},
    })


def _repo():
    repo = SqliteRepository(":memory:")
    repo.init_schema()
    return repo


def _post(tid, handle="bigshot", followers=20000, age_minutes=30, **kw):
    defaults = dict(
        tweet_id=tid, author_handle=handle, author_name=handle,
        text=SOURCE_TEXT, created_at=utcnow() - timedelta(minutes=age_minutes),
        author_follower_count=followers, url=f"https://x.com/{handle}/status/{tid}",
        metrics=Metrics(likes=50),
    )
    defaults.update(kw)
    return Post(**defaults)


def _seed(repo, post, topic=0.9, teaching=0.6, judged=True):
    repo.upsert_post(post)
    repo.save_score(Score(tweet_id=post.tweet_id, topic_fit=topic,
                          quote_worthy=teaching, quote_score=0.5, judged=judged))


class _FakeReplyGen:
    def __init__(self, texts=None):
        self.texts = list(texts or [GOOD_REPLY])
        self.calls = 0

    def generate(self, post):
        self.calls += 1
        return self.texts[min(self.calls - 1, len(self.texts) - 1)], "fake:model"

    def revise(self, post, previous, feedback):
        return previous, "fake:model"


class _RecordingPublisher:
    def __init__(self):
        self.replies = []

    def reply(self, text, in_reply_to_tweet_id):
        self.replies.append((text, in_reply_to_tweet_id))
        return {"ok": True, "id": f"our_{in_reply_to_tweet_id}"}

    def publish(self, draft, post):
        return {"ok": True, "id": "x"}


def _orch(tmp_path, repo, gen=None, publisher=None, **cfg_kw):
    orch = object.__new__(Orchestrator)
    orch.cfg = _cfg(tmp_path, **cfg_kw)
    orch.repo = repo
    orch.publisher = publisher or _RecordingPublisher()
    orch.judge_reasons = {}
    if gen is not None:
        orch.reply_generator = gen  # instance override hook used by _reply_scan
    return orch


# ---------------- target selection ----------------

def test_target_selection_filters(tmp_path):
    repo = _repo()
    cfg = _cfg(tmp_path)
    _seed(repo, _post("ok1"))                                       # qualifies
    _seed(repo, _post("old", age_minutes=300))                      # too old
    _seed(repo, _post("small", handle="tiny", followers=200))       # small author
    _seed(repo, _post("rt", handle="rter", is_retweet=True))        # retweet
    _seed(repo, _post("rep", handle="replier", is_reply=True))      # reply
    _seed(repo, _post("offtopic", handle="off"), topic=0.1)         # low topic
    _seed(repo, _post("unjudged", handle="uj"), judged=False)       # no verdict
    _seed(repo, _post("mine", handle="me_bot"))                     # own post

    targets, skipped = select_reply_targets(
        repo.recent_posts(72), cfg, repo, own_handle="me_bot")
    assert [t.tweet_id for t in targets] == ["ok1"]
    reasons = {p.tweet_id: why for p, why in skipped}
    assert reasons["old"] == "too_old"
    assert reasons["small"].startswith("small_author")
    assert reasons["rt"] == "reply_or_rt"
    assert reasons["offtopic"].startswith("low_topic")
    assert reasons["unjudged"] == "not_judged"
    assert reasons["mine"] == "own_post"


def test_targets_ranked_big_and_fresh_first(tmp_path):
    repo = _repo()
    _seed(repo, _post("a", handle="h1", followers=8000, age_minutes=20))
    _seed(repo, _post("b", handle="h2", followers=500000, age_minutes=20))
    targets, _ = select_reply_targets(repo.recent_posts(72), _cfg(tmp_path), repo)
    assert [t.tweet_id for t in targets] == ["b", "a"]


def test_already_replied_and_author_cooldown(tmp_path):
    repo = _repo()
    _seed(repo, _post("t1", handle="alice"))
    _seed(repo, _post("t2", handle="alice"))
    repo.log_reply("t1", "alice", "txt", "reply", "m", "posted", "", "our1")
    targets, skipped = select_reply_targets(repo.recent_posts(72), _cfg(tmp_path), repo)
    assert targets == []
    reasons = {p.tweet_id: why for p, why in skipped}
    assert reasons["t1"] == "already_replied"
    assert reasons["t2"] == "author_cooldown"


# ---------------- reply_scan flow ----------------

def test_dry_run_logs_but_uses_dryrun_publisher(tmp_path):
    repo = _repo()
    _seed(repo, _post("t1"))
    pub = _RecordingPublisher()
    orch = _orch(tmp_path, repo, gen=_FakeReplyGen(), publisher=pub)
    result = orch.reply_scan()
    assert result["status"] == "replied"
    assert result["count"] == 1
    assert pub.replies == []          # live publisher untouched in dry-run
    rows = repo.activity_replies(1)
    assert rows[0]["status"] == "dry_run"
    # dry-run replies do NOT consume the daily cap
    assert repo.count_replies_today() == 0


def test_live_mode_posts_via_publisher(tmp_path):
    repo = _repo()
    _seed(repo, _post("t1"))
    pub = _RecordingPublisher()
    orch = _orch(tmp_path, repo, gen=_FakeReplyGen(), publisher=pub, dry_run=False)
    result = orch.reply_scan()
    assert result["count"] == 1
    assert pub.replies == [(GOOD_REPLY, "t1")]
    assert repo.count_replies_today() == 1


def test_daily_cap_blocks(tmp_path):
    repo = _repo()
    _seed(repo, _post("t1"))
    for i in range(6):
        repo.log_reply(f"x{i}", f"a{i}", "t", "r", "m", "posted", "", f"o{i}")
    orch = _orch(tmp_path, repo, gen=_FakeReplyGen(), dry_run=False)
    assert orch.reply_scan()["status"] == "cap_reached"


def test_min_gap_blocks(tmp_path):
    repo = _repo()
    _seed(repo, _post("t1"))
    repo.log_reply("x", "a", "t", "r", "m", "posted", "", "o")  # just now
    orch = _orch(tmp_path, repo, gen=_FakeReplyGen(), dry_run=False)
    assert orch.reply_scan()["status"] == "too_soon"


def test_kill_switch_blocks(tmp_path):
    repo = _repo()
    _seed(repo, _post("t1"))
    (tmp_path / "STOP").write_text("stop")
    orch = _orch(tmp_path, repo, gen=_FakeReplyGen())
    assert orch.reply_scan()["status"] == "killed"


def test_disabled_flag_blocks(tmp_path):
    repo = _repo()
    _seed(repo, _post("t1"))
    orch = _orch(tmp_path, repo, gen=_FakeReplyGen(), enabled=False)
    assert orch.reply_scan()["status"] == "disabled"


def test_skip_sentinel_blocks_target_forever(tmp_path):
    repo = _repo()
    _seed(repo, _post("t1"))
    orch = _orch(tmp_path, repo, gen=_FakeReplyGen(["SKIP: nothing to add"]))
    result = orch.reply_scan()
    assert result["count"] == 0
    assert repo.has_replied("t1")        # blocked row → never retried
    rows = repo.activity_replies(1)
    assert rows[0]["status"] == "blocked"
    assert rows[0]["note"].startswith("no_material")


def test_gate_blocked_reply_logged_and_next_target_tried(tmp_path):
    repo = _repo()
    _seed(repo, _post("t1", handle="alice", followers=900000))   # ranked first
    _seed(repo, _post("t2", handle="bob", followers=10000))
    # first target draws a URL-bearing reply (gate-blocked twice: generate+revise
    # return the same), second target draws a clean one
    gen = _FakeReplyGen(["check https://t.co/x for the full breakdown of this",
                         GOOD_REPLY])
    orch = _orch(tmp_path, repo, gen=gen, dry_run=False)
    result = orch.reply_scan()
    assert result["count"] == 1
    assert result["results"][0]["target"] == "t2"
    assert repo.has_replied("t1")        # blocked, never retried
    assert repo.count_replies_today() == 1


def test_run_log_records_replied(tmp_path):
    repo = _repo()
    _seed(repo, _post("t1"))
    orch = _orch(tmp_path, repo, gen=_FakeReplyGen(), dry_run=False)
    orch.reply_scan()
    days = repo.daily_run_totals(1)
    assert days[0]["replied"] == 1
