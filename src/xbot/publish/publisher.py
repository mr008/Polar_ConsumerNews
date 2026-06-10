"""Publisher interface."""
from __future__ import annotations

import re
from typing import Protocol, runtime_checkable

from ..models import Draft, Post

_HT_TAIL = re.compile(r"\s*h/?t\s+@\w+\s*$", re.IGNORECASE)


@runtime_checkable
class Publisher(Protocol):
    def publish(self, draft: Draft, post: Post) -> dict:
        """Post the quote tweet. Returns {'ok': bool, 'id': str}."""
        ...


def strip_ht_tail(commentary: str) -> str:
    """Remove the trailing "h/t @x" (link mode folds it into the attribution line)."""
    return _HT_TAIL.sub("", commentary.strip()).rstrip()


def body_budget(post: Post, cfg) -> int:
    """Max commentary-body chars for this post. In link mode the attribution line
    ("h/t @handle: <url>", URL counts as 23 chars on X) eats into the 280."""
    if cfg and cfg.get("posting.link_instead_of_quote", False):
        return 280 - (len(f"h/t @{post.author_handle}: ") + 23) - 2
    return 280


def compose_text(draft: Draft, post: Post, cfg) -> tuple[str, bool]:
    """Build the final post text. In link mode (cross-account quoting is broken),
    append the source URL after the commentary — X often auto-embeds it as a quote
    card, and it's a clickable link regardless. Returns (text, link_mode)."""
    link_mode = bool(cfg.get("posting.link_instead_of_quote", False)) if cfg else False
    if link_mode:
        body = strip_ht_tail(draft.commentary)
        attribution = f"h/t @{post.author_handle}: {post.url}"
        max_body = body_budget(post, cfg)
        if len(body) > max_body:  # last resort — safety should have caught this
            body = body[:max_body].rsplit(" ", 1)[0].rstrip()
        return f"{body}\n\n{attribution}", True
    return draft.commentary, False
