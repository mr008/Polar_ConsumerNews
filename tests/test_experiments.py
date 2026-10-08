"""Experiment layer (spec docs/superpowers/specs/2026-10-08-growth-experiments-design.md)."""
import json

from xbot.config import NS
from xbot.models import Draft, Metrics, Post, utcnow
from xbot.storage.sqlite_repo import SqliteRepository


def _repo():
    repo = SqliteRepository(":memory:")
    repo.init_schema()
    return repo


def _post(tid="1", handle="alice"):
    return Post(tweet_id=tid, author_handle=handle, author_name=handle,
                text=f"tactic {tid} alpha beta", created_at=utcnow(),
                author_follower_count=1000, metrics=Metrics(likes=1))


def _cfg(**exp):
    data = {"experiments": {
        "image_card": {"enabled": False, "arms": ["text", "card"], "started": "2026-10-09"},
        "author_bait": {"enabled": False, "arms": ["tail", "question"], "started": "2026-10-09"},
        "volume": {"enabled": False, "period": True, "started": "2026-10-09"},
    }}
    for name, on in exp.items():
        data["experiments"][name]["enabled"] = on
    return NS(data)


def test_disabled_experiments_assign_nothing():
    from xbot.experiments import assign_arms
    assert assign_arms(_cfg(), _repo(), _post()) == {}


def test_arm_default_is_control():
    from xbot.experiments import arm
    assert arm({}, "image_card") == "text"
    assert arm(None, "author_bait") == "tail"
    assert arm({"image_card": "card"}, "image_card") == "card"


def test_alternation_is_balanced_over_drafts():
    from xbot.experiments import assign_arms
    cfg = _cfg(image_card=True, author_bait=True)
    repo = _repo()
    seen = []
    for i in range(6):
        post = _post(str(i))
        repo.upsert_post(post)
        arms = assign_arms(cfg, repo, post)
        seen.append(arms)
        repo.add_draft(Draft(tweet_id=post.tweet_id, commentary="x", model="t",
                             safety_passed=True, arms=arms))
    assert [a["image_card"] for a in seen] == ["text", "card"] * 3
    assert [a["author_bait"] for a in seen] == ["tail", "question"] * 3


def test_blocked_drafts_do_not_advance_alternation():
    from xbot.experiments import assign_arms
    cfg = _cfg(image_card=True)
    repo = _repo()
    p = _post("1"); repo.upsert_post(p)
    arms = assign_arms(cfg, repo, p)
    repo.add_draft(Draft(tweet_id="1", commentary="x", model="t", arms=arms), status="blocked")
    assert assign_arms(cfg, repo, _post("2")) == arms   # still "text"


def test_web_posts_never_get_question():
    from xbot.experiments import assign_arms
    cfg = _cfg(author_bait=True)
    repo = _repo()
    web = Post(tweet_id="web:abc", author_handle="blog.com", author_name="Blog",
               text="brief", created_at=utcnow())
    assert assign_arms(cfg, repo, web)["author_bait"] == "tail"
    # ...and it does not consume an alternation slot: the first X post is
    # still the first slot (control), the second X post the second.
    repo.add_draft(Draft(tweet_id="web:abc", commentary="x", model="t",
                         arms={"author_bait": "tail"}))
    first = assign_arms(cfg, repo, _post("2"))
    assert first["author_bait"] == "tail"
    repo.upsert_post(_post("2"))
    repo.add_draft(Draft(tweet_id="2", commentary="x", model="t", arms=first))
    assert assign_arms(cfg, repo, _post("3"))["author_bait"] == "question"


def test_draft_arms_round_trip_and_legacy_rows():
    repo = _repo()
    repo.upsert_post(_post("1"))
    did = repo.add_draft(Draft(tweet_id="1", commentary="x", model="t",
                               safety_passed=True, arms={"image_card": "card"}))
    draft, _ = repo.get_draft(did)
    assert draft.arms == {"image_card": "card"}
    # pre-migration row: arms NULL must read back as {}
    repo.conn.execute("UPDATE drafts SET arms=NULL WHERE id=?", (did,))
    repo.conn.commit()
    draft, _ = repo.get_draft(did)
    assert draft.arms == {}
    assert repo.arm_counts("image_card") == {}


def test_log_features_stores_arms():
    repo = _repo()
    repo.log_features({"our_tweet_id": "our_1", "arms": {"image_card": "text",
                                                          "author_bait": "question"}})
    row = repo.conn.execute("SELECT arms FROM post_features").fetchone()
    assert json.loads(row["arms"]) == {"image_card": "text", "author_bait": "question"}
