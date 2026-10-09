from xbot.config import NS
from xbot.commentary.safety import check_commentary, classify_source
from xbot.models import Post, utcnow

CFG = NS({
    "safety": {"exclude": ["politics", "medical_advice", "investment_advice", "nsfw"]},
    "llm": {"max_commentary_chars": 240},
})


def _post(text):
    return Post(tweet_id="1", author_handle="x", author_name="X", text=text,
                created_at=utcnow())


def test_politics_rejected():
    ok, reason = classify_source(_post("vote them out, the election proves it"), CFG)
    assert not ok and reason.startswith("excluded:politics")


def test_medical_rejected():
    ok, reason = classify_source(_post("take 2000mg of this supplement to cure migraine"), CFG)
    assert not ok and "medical" in reason


def test_growth_revenue_is_in_scope():
    # "$20k/mo" growth content must NOT be filtered as financial advice
    ok, _ = classify_source(_post("how I scaled my app to $20k/mo with AI UGC"), CFG)
    assert ok


def test_commentary_blocks_fabricated_numbers():
    post = _post("reusing content gets you reach")          # no numbers in source
    ok, reason = check_commentary(post, "This gets you 10000 views easy", CFG)
    assert not ok and reason.startswith("fabricated_number")


def test_commentary_allows_source_numbers():
    post = _post("scaled to $20k/mo with this 5 step play")
    ok, _ = check_commentary(post, "the 5 step play that hit 20k. h/t @x", CFG)
    assert ok


def test_readability_rejects_bullets_jargon_and_long_lines():
    from xbot.commentary.safety import check_readability
    assert check_readability("• one\n• two") == "format:bullets"
    assert check_readability("- one\n- two") == "format:bullets"
    assert check_readability("Your CPT is the only number.") == "format:jargon:cpt"
    long = " ".join(["word"] * 21)
    assert check_readability(long).startswith("format:line_too_long:21")
    assert check_readability("A founder ran one ad on two platforms.\n\nMeasure it.") == ""


def test_readability_line_length_boundary():
    from xbot.commentary.safety import check_readability
    assert check_readability(" ".join(["word"] * 20)) == ""
    assert check_readability(" ".join(["word"] * 21)) == "format:line_too_long:21"


def test_readability_rejects_numbered_lists_but_not_leading_numbers():
    from xbot.commentary.safety import check_readability
    assert check_readability("1. post daily\n2. reuse the winner") == "format:bullets"
    assert check_readability("Steps:\n  2) reuse the winner") == "format:bullets"
    assert check_readability("4 times more per trial.") == ""
    assert check_readability("He paid 4 times more.\n\n30 a day bought it.") == ""


def test_commentary_gate_applies_readability_rules():
    post = _post("he ran one ad on 2 platforms")
    ok, reason = check_commentary(post, "• 2 platforms, same ad\n• measure it", CFG)
    assert not ok and reason == "format:bullets"
    ok, reason = check_commentary(post, "One ad, 2 platforms.\n\nMeasure it.\n\nh/t @x", CFG)
    assert ok
    ok, reason = check_commentary(post, "One ad, 2 platforms.", CFG,
                                  parts=["• step one"])
    assert not ok and reason == "part1_format:bullets"


def test_readability_rules_can_be_switched_off():
    cfg = NS({"safety": {"exclude": []}, "llm": {"max_commentary_chars": 240},
              "voice": {"readable_rules": False}})
    post = _post("he ran one ad on 2 platforms")
    ok, _ = check_commentary(post, "• 2 platforms\n• measure", cfg)
    assert ok
