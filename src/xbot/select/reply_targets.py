"""Reply-target selection for the auto-reply engine.

Targets come from the owner's curated home feed (followed accounts only, by
construction — the source adapter reads nothing else). A good target is FRESH
(engagement velocity rewards replies in the first hours), from a BIGGER account
(profile-visit lever), ON-TOPIC per the stored judge verdict, and not someone
we've replied to recently. Zero extra LLM/API calls: every signal here is
already in the DB.
"""
from __future__ import annotations

import math

from ..commentary.safety import classify_source
from ..config import NS
from ..models import Post

RECENCY_TAU_HOURS = 1.5  # velocity window — a 3h-old post is worth ~1/8 of a fresh one


def _rank(post: Post) -> float:
    return math.log1p(post.author_follower_count) * math.exp(-post.age_hours / RECENCY_TAU_HOURS)


def select_reply_targets(posts: list[Post], cfg: NS, repo,
                         own_handle: str = "") -> tuple[list[Post], list[tuple[Post, str]]]:
    """Return (targets ranked best-first, skipped with reasons)."""
    max_age_min = float(cfg.get("replies.max_target_age_minutes", 180))
    min_followers = int(cfg.get("replies.min_author_followers", 5000))
    min_topic = float(cfg.get("replies.min_topic_fit", 0.45))
    min_teaching = float(cfg.get("replies.min_teaching", 0.2))
    cooldown_days = int(cfg.get("replies.author_cooldown_days", 3))
    exclude = {h.lstrip("@").lower() for h in cfg.get("replies.exclude_authors", []) or []}
    langs = cfg.get("scoping.languages", ["en"])

    recent_reply_authors = repo.reply_authors_since(cooldown_days)
    targets: list[Post] = []
    skipped: list[tuple[Post, str]] = []

    def skip(p: Post, why: str):
        skipped.append((p, why))

    for p in posts:
        if p.age_hours * 60 > max_age_min:
            skip(p, "too_old"); continue
        if p.is_reply or p.is_retweet:
            skip(p, "reply_or_rt"); continue
        if langs and p.lang not in langs:
            skip(p, "lang"); continue
        if len((p.text or "").split()) < 4:
            skip(p, "too_short"); continue
        if own_handle and p.author_handle.lower() == own_handle.lstrip("@").lower():
            skip(p, "own_post"); continue
        if p.author_handle.lower() in exclude:
            skip(p, "excluded_author"); continue
        if p.author_follower_count < min_followers:
            skip(p, f"small_author:{p.author_follower_count}"); continue
        ok, reason = classify_source(p, cfg)
        if not ok:
            skip(p, reason); continue
        s = repo.get_score(p.tweet_id)
        if s is None or not s.judged:
            skip(p, "not_judged"); continue  # judge verdicts come free from collect
        if s.topic_fit < min_topic:
            skip(p, f"low_topic:{s.topic_fit:.2f}"); continue
        if s.quote_worthy < min_teaching:
            skip(p, f"low_teaching:{s.quote_worthy:.2f}"); continue
        if repo.has_replied(p.tweet_id):
            skip(p, "already_replied"); continue
        if repo.has_posted(p.tweet_id) or repo.has_posted(p.canonical_id):
            skip(p, "already_quoted"); continue
        if p.author_handle in recent_reply_authors:
            skip(p, "author_cooldown"); continue
        targets.append(p)

    targets.sort(key=_rank, reverse=True)
    return targets, skipped
