"""Deduplication: exact (posted_log), near-duplicate (text similarity), and
per-author cooldown."""
from .dedup import author_in_cooldown, is_near_duplicate

__all__ = ["author_in_cooldown", "is_near_duplicate"]
