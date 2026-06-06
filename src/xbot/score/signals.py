"""Engagement signals only. Teaching value and topic relevance are decided
SEMANTICALLY by the LLM judge (score/teaching_judge.py) — there is no lexical
keyword scoring anymore.
"""
from __future__ import annotations

import math

from ..models import Metrics, Post


def log_scale(x: float) -> float:
    return math.log1p(max(x, 0.0))


def recency_decay(age_hours: float, tau_hours: float) -> float:
    return math.exp(-age_hours / max(tau_hours, 0.1))


def eng_per_follower(total_engagement: int, followers: int, floor: int = 500) -> float:
    return total_engagement / max(followers, floor)


def velocity(history: list[Metrics], post: Post) -> float:
    """Engagement growth per hour. Uses the snapshot slope when we have >=2
    samples; otherwise total engagement / age (a reasonable single-shot proxy)."""
    if len(history) >= 2:
        dt = (history[-1].captured_at - history[0].captured_at).total_seconds() / 3600.0
        d = history[-1].total_engagement - history[0].total_engagement
        if dt > 0.1:
            return max(d / dt, 0.0)
    return post.metrics.total_engagement / max(post.age_hours, 1.0)
