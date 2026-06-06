from __future__ import annotations

from ..textsim import similarity


def is_near_duplicate(text: str, posted_texts: list[str], threshold: float) -> bool:
    """True if `text` is too similar to anything already posted (an idea repeat)."""
    return any(similarity(text, prev) >= threshold for prev in posted_texts)


def author_in_cooldown(handle: str, repo, days: int) -> bool:
    return handle in repo.posted_authors_since(days)
