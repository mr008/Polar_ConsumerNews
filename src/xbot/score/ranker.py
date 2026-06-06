"""Combine signals into a stage-1 weighted score and a final quote_score.

Cross-post signals (likes, reposts, velocity, eng/follower, echo) are min-max
normalized across the batch so the configured weights are comparable. topic_fit
and quote_worthiness are already 0..1 and used directly.
"""
from __future__ import annotations

from ..config import NS
from ..models import Post, Score
from ..textsim import similarity
from . import signals as sig

ECHO_SIM = 0.45  # two posts count as the "same idea" above this similarity


def _echo_count(post: Post, posts: list[Post]) -> int:
    n = 0
    for other in posts:
        if other.tweet_id == post.tweet_id:
            continue
        if other.canonical_id == post.canonical_id or similarity(post.text, other.text) >= ECHO_SIM:
            n += 1
    return n


def _minmax(values: dict[str, float]) -> dict[str, float]:
    if not values:
        return {}
    lo, hi = min(values.values()), max(values.values())
    if hi <= lo:
        return {k: 0.5 for k in values}
    return {k: (v - lo) / (hi - lo) for k, v in values.items()}


def score_posts(posts: list[Post], cfg: NS, repo=None, teaching_scores=None,
                topic_scores=None) -> list[Score]:
    """`teaching_scores` / `topic_scores` (dict tweet_id->value) let the semantic
    LLM judge supply teaching value and topic relevance; both fall back to the
    lexical heuristic per-post only when the judge didn't cover that post."""
    if not posts:
        return []
    w = cfg.scoring_weights
    tau = cfg.get("recency_tau_hours", 8)
    teaching_w = cfg.get("ranking.teaching_weight", 0.65)

    raw_likes, raw_reposts, raw_vel, raw_epf, raw_echo = {}, {}, {}, {}, {}
    recency, topic, qw = {}, {}, {}

    for p in posts:
        hist = repo.metrics_history(p.tweet_id) if repo else []
        raw_likes[p.tweet_id] = sig.log_scale(p.metrics.likes)
        raw_reposts[p.tweet_id] = sig.log_scale(p.metrics.reposts)
        raw_vel[p.tweet_id] = sig.velocity(hist, p)
        raw_epf[p.tweet_id] = sig.eng_per_follower(p.metrics.total_engagement, p.author_follower_count)
        raw_echo[p.tweet_id] = float(_echo_count(p, posts))
        recency[p.tweet_id] = sig.recency_decay(p.age_hours, tau)
        # Semantic judge is the sole authority; an unjudged post is ineligible (0).
        topic[p.tweet_id] = (topic_scores or {}).get(p.tweet_id, 0.0)
        qw[p.tweet_id] = (teaching_scores or {}).get(p.tweet_id, 0.0)

    n_likes = _minmax(raw_likes)
    n_reposts = _minmax(raw_reposts)
    n_vel = _minmax(raw_vel)
    n_epf = _minmax(raw_epf)
    n_echo = _minmax(raw_echo)

    scores: list[Score] = []
    for p in posts:
        tid = p.tweet_id
        stage1 = (
            w.likes * n_likes[tid]
            + w.reposts * n_reposts[tid]
            + w.velocity * n_vel[tid]
            + w.eng_per_follower * n_epf[tid]
            + w.echo * n_echo[tid]
            + w.recency * recency[tid]
            + w.topic_fit * topic[tid]
        )
        # TEACHING-FIRST: teaching value (qw) is primary; the engagement composite
        # (stage1) is a secondary sanity-check. Topic acts as a gate. So a concrete,
        # useful post with modest engagement beats a viral hype post.
        quote_score = (teaching_w * qw[tid] + (1 - teaching_w) * stage1) * (0.3 + 0.7 * topic[tid])
        scores.append(Score(
            tweet_id=tid,
            velocity_n=n_vel[tid], eng_per_follower_n=n_epf[tid], echo_n=n_echo[tid],
            recency_n=recency[tid], topic_fit=topic[tid], quote_worthy=qw[tid],
            stage1_score=round(stage1, 4), quote_score=round(quote_score, 4),
        ))
    scores.sort(key=lambda s: s.quote_score, reverse=True)
    return scores
