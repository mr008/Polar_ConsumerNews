from xbot.models import Metrics, Post, utcnow
from xbot.score import signals as sig


def _post(text, likes=0, reposts=0, followers=1000):
    return Post(tweet_id="1", author_handle="x", author_name="X", text=text,
                created_at=utcnow(), author_follower_count=followers,
                metrics=Metrics(likes=likes, reposts=reposts))


def test_recency_decay_monotonic():
    assert sig.recency_decay(0, 8) == 1.0
    assert sig.recency_decay(8, 8) < sig.recency_decay(4, 8) < 1.0


def test_eng_per_follower_uses_floor():
    # tiny account: floor protects against divide-by-noise
    assert sig.eng_per_follower(100, 10, floor=500) == 100 / 500


def test_log_scale_dampens_whales():
    assert sig.log_scale(0) == 0.0
    assert sig.log_scale(1_000_000) < 1_000_000  # log compresses big counts


def test_velocity_single_snapshot_proxy():
    p = _post("a real post about growth", likes=60)
    # no history -> engagement / age proxy, non-negative
    assert sig.velocity([], p) >= 0.0
