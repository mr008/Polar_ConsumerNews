"""Eligibility rules. A candidate must clear ALL of them; otherwise it's skipped
with a reason (logged for the daily report / tuning)."""
from __future__ import annotations

from ..commentary.safety import classify_source
from ..config import NS
from ..dedup import author_in_cooldown, is_near_duplicate
from ..models import Post, Score


def evaluate(post: Post, score: Score, cfg: NS, repo) -> tuple[bool, str]:
    ok, reason = classify_source(post, cfg)
    if not ok:
        return False, reason

    if score.topic_fit < cfg.get("thresholds.topic_fit_min", 0.55):
        return False, f"low_topic_fit:{score.topic_fit:.2f}"
    if score.quote_worthy < cfg.get("thresholds.quote_worthy_min", 0.55):
        return False, f"low_quote_worthy:{score.quote_worthy:.2f}"

    if post.is_reply:
        return False, "reply"
    if post.has_link and len(post.text) < 60:
        return False, "link_only"
    if post.has_media and len(post.text) < 40:
        return False, "media_only"

    if repo.has_posted(post.tweet_id) or repo.has_posted(post.canonical_id):
        return False, "already_posted"
    if is_near_duplicate(post.text, repo.posted_source_texts(),
                         cfg.get("thresholds.near_dup_similarity", 0.82)):
        return False, "near_duplicate"
    if author_in_cooldown(post.author_handle, repo, cfg.get("posting.author_cooldown_days", 5)):
        return False, "author_cooldown"

    return True, "eligible"


def select_all(posts: list[Post], score_map: dict[str, Score], cfg: NS, repo):
    eligible: list[tuple[Score, Post]] = []
    skipped: list[tuple[Post, str]] = []
    for p in posts:
        s = score_map.get(p.tweet_id)
        if s is None:
            continue
        ok, reason = evaluate(p, s, cfg, repo)
        if ok:
            eligible.append((s, p))
        else:
            skipped.append((p, reason))
    eligible.sort(key=lambda sp: sp[0].quote_score, reverse=True)
    return eligible, skipped
