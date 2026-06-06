"""Scoring: cheap weighted signals (stage 1) + quote-worthiness (stage 2)."""
from .ranker import score_posts

__all__ = ["score_posts"]
