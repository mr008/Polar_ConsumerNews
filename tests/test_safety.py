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
