"""Golden tests for the reply gates (check_reply), the deterministic refusal
heuristic, smart_trim, and thread-part validation in check_commentary.

The three REAL refusal texts below reached (or nearly reached) the live account
on 2026-06-09/10 — prod drafts #6 and #10 and local draft #14. They must never
pass a gate again.
"""
from xbot.commentary.safety import check_commentary, check_reply, looks_like_refusal
from xbot.config import NS
from xbot.models import Metrics, Post, utcnow
from xbot.publish.publisher import body_budget, compose_text, smart_trim

REFUSAL_PROD_10 = (
    "This post doesn't contain enough tactical content to build a skill-share "
    "breakdown from.\n\nThe source is a cliffhanger with no completed insight — "
    "no tactic, metric outcome, or lesson shared yet.\n\nDrop the full thread "
    "and I'll turn it into a sharp breakdown. h/t @gauravsbuilding"
)
REFUSAL_PROD_6 = (
    'This post doesn\'t contain enough tactical content to build a "steal this" '
    "skill breakdown. The author teases a story but shares no method, numbers, "
    "or repeatable steps. Share a source post with actual specifics and I'll "
    "write it. h/t @gauravsbuilding"
)
REFUSAL_LOCAL_14 = (
    "The source post is just a retweet of someone else referencing a 1990 "
    "article — there are no concrete tactics, metrics, tools, or outcomes I can "
    "report without fabricating.\n\nI can't write a compliant breakdown here."
)

GOOD_COMMENTARY = (
    "Faceless UGC accounts print views with zero talent costs 🎬\n\n"
    "• Rip trending sounds daily\n"
    "• AI voiceover over stock b-roll\n"
    "• Post often, kill losers fast\n\n"
    "Volume beats polish. h/t @creator"
)

SOURCE_TEXT = ("how to make faceless ugc react videos with ai — cheaper than "
               "hiring talent, post 4x daily, kill losers at 24h. 50 views "
               "minimum or delete.")


def _cfg(extra=None):
    data = {
        "safety": {"exclude": ["politics", "ragebait", "nsfw", "harassment",
                               "personal_drama", "medical_advice", "legal_advice",
                               "investment_advice"]},
        "posting": {"format": "mention", "max_thread_parts": 3, "max_part_chars": 270},
        "replies": {"max_reply_chars": 240},
        "ranking": {"qa_gate": False},
    }
    if extra:
        for k, v in extra.items():
            data.setdefault(k, {}).update(v) if isinstance(v, dict) else data.update({k: v})
    return NS(data)


def _post(text=SOURCE_TEXT, handle="creator"):
    return Post(tweet_id="t1", author_handle=handle, author_name=handle,
                text=text, created_at=utcnow(), author_follower_count=20000,
                url=f"https://x.com/{handle}/status/1", metrics=Metrics(likes=50))


# ---------------- refusal heuristic (the 2026-06-10 incident) ----------------

def test_all_three_real_refusals_are_detected():
    for refusal in (REFUSAL_PROD_10, REFUSAL_PROD_6, REFUSAL_LOCAL_14):
        assert looks_like_refusal(refusal), refusal[:50]


def test_real_refusals_blocked_by_check_commentary():
    cfg = _cfg()
    for refusal in (REFUSAL_PROD_10, REFUSAL_PROD_6, REFUSAL_LOCAL_14):
        ok, note = check_commentary(_post(), refusal, cfg)
        assert ok is False
        assert note.startswith("refusal_meta")


def test_skip_sentinel_detected_as_refusal_backstop():
    assert looks_like_refusal("SKIP: cliffhanger, no completed lesson")


def test_good_commentary_not_flagged_as_refusal():
    assert looks_like_refusal(GOOD_COMMENTARY) == ""
    ok, note = check_commentary(_post(), GOOD_COMMENTARY, _cfg())
    assert ok is True, note


# ---------------- check_reply gates ----------------

GOOD_REPLY = ("the kill-losers-at-24h rule is the underrated part — most people "
              "let dead posts sit for a week. did you find 24h was enough signal "
              "for react formats specifically?")


def test_good_reply_passes():
    ok, note = check_reply(_post(), GOOD_REPLY, _cfg())
    assert ok is True, note


def test_reply_with_url_rejected():
    ok, note = check_reply(_post(), "wrote about this here https://t.co/abc123 worth a read for the workflow", _cfg())
    assert ok is False and note == "reply_url"


def test_reply_with_hashtag_rejected():
    ok, note = check_reply(_post(), "volume beats polish every time #buildinpublic and the data backs it up", _cfg())
    assert ok is False and note == "reply_hashtag"


def test_reply_with_mention_rejected():
    ok, note = check_reply(_post(), "this matches what @someguy found with faceless accounts last cycle too", _cfg())
    assert ok is False and note == "reply_mention"


def test_generic_praise_rejected():
    for praise in ("great post", "So true!", "this is gold", "love this 🔥"):
        ok, note = check_reply(_post(), praise, _cfg())
        assert ok is False, praise
        assert note.startswith(("reply_generic_praise", "reply_too_short")), note


def test_fabricated_number_in_reply_rejected():
    ok, note = check_reply(_post(), "we saw 87% retention doing exactly this with react formats", _cfg())
    assert ok is False and note.startswith("reply_fabricated_number")


def test_number_from_source_allowed_in_reply():
    ok, note = check_reply(_post(), "posting 4x daily is the part people skip — does the cadence hold on smaller niches too?", _cfg())
    assert ok is True, note


def test_too_long_and_too_short_replies_rejected():
    ok, note = check_reply(_post(), "x" * 241, _cfg())
    assert ok is False and note.startswith("reply_too_long")
    ok, note = check_reply(_post(), "nice tactic", _cfg())
    assert ok is False and note.startswith("reply_too_short")


def test_refusal_style_reply_rejected():
    ok, note = check_reply(_post(), "I can't write a meaningful reply because the source post lacks specifics to engage with", _cfg())
    assert ok is False and note.startswith("reply_refusal_meta")


# ---------------- thread parts validation ----------------

def test_thread_parts_validated():
    cfg = _cfg()
    post = _post()
    good_parts = ["1. Rip trending sounds daily\n2. AI voiceover over stock b-roll\n"
                  "Takeaway: volume beats polish."]
    ok, note = check_commentary(post, GOOD_COMMENTARY, cfg, parts=good_parts)
    assert ok is True, note

    ok, note = check_commentary(post, GOOD_COMMENTARY, cfg,
                                parts=["see https://example.com for the steps"])
    assert ok is False and "url" in note

    ok, note = check_commentary(post, GOOD_COMMENTARY, cfg, parts=["y" * 300])
    assert ok is False and "too_long" in note

    ok, note = check_commentary(post, GOOD_COMMENTARY, cfg,
                                parts=["a" * 50, "b" * 50, "c" * 50])
    assert ok is False and note.startswith("too_many_parts")


def test_url_in_main_post_rejected_outside_link_mode():
    ok, note = check_commentary(_post(), "steal this https://x.com/a/status/1 h/t @creator", _cfg())
    assert ok is False and note == "url_in_commentary"


# ---------------- smart_trim ----------------

def test_smart_trim_drops_bullets_first_and_keeps_tail():
    long = ("Hook line here\n\n"
            "• first bullet with detail\n"
            "• second bullet with detail\n"
            "• third bullet with much much longer detail text\n"
            "• fourth bullet that pushes it over\n\n"
            "Takeaway line. h/t @creator")
    budget = len(long) - 30  # force at least one bullet drop
    trimmed = smart_trim(long, budget)
    from xbot.publish.publisher import strip_ht_tail
    assert len(strip_ht_tail(trimmed)) <= budget
    assert trimmed.rstrip().endswith("h/t @creator")
    assert "fourth bullet" not in trimmed


def test_smart_trim_handles_unbroken_text():
    trimmed = smart_trim("y" * 400, 280)
    assert len(trimmed) <= 280


# ---------------- compose_text formats ----------------

def test_mention_mode_has_no_url_and_keeps_tail():
    cfg = _cfg()
    text, fmt = compose_text(_draft(GOOD_COMMENTARY), _post(), cfg)
    assert fmt == "mention"
    assert "http" not in text
    assert text.rstrip().endswith("h/t @creator")


def test_mention_mode_appends_tail_when_missing():
    cfg = _cfg()
    text, _ = compose_text(_draft("Steal this growth loop. Volume beats polish."),
                           _post(), cfg)
    assert text.rstrip().endswith("h/t @creator")


def test_legacy_link_mode_still_composes():
    cfg = NS({"posting": {"format": "link"}})
    text, fmt = compose_text(_draft(GOOD_COMMENTARY), _post(), cfg)
    assert fmt == "link"
    assert "https://x.com/creator/status/1" in text


def test_legacy_boolean_key_back_compat():
    cfg = NS({"posting": {"link_instead_of_quote": True}})
    _, fmt = compose_text(_draft(GOOD_COMMENTARY), _post(), cfg)
    assert fmt == "link"
    cfg = NS({"posting": {}})
    _, fmt = compose_text(_draft(GOOD_COMMENTARY), _post(), cfg)
    assert fmt == "quote"


def test_mention_budget_tighter_than_quote():
    post = _post()
    assert body_budget(post, _cfg()) < 280
    assert body_budget(post, NS({"posting": {}})) == 280


def _draft(commentary, parts=None):
    from xbot.models import Draft
    return Draft(tweet_id="t1", commentary=commentary, model="test",
                 parts=parts or [])
