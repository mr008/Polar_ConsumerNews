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


def compose_text(draft: Draft, post: Post, cfg) -> tuple[str, bool]:
    """Build the final post text. In link mode (cross-account quoting is broken),
    append the source URL after the commentary — X often auto-embeds it as a quote
    card, and it's a clickable link regardless. Returns (text, link_mode)."""
    link_mode = bool(cfg.get("posting.link_instead_of_quote", False)) if cfg else False
    if link_mode:
        # Drop the trailing "h/t @x" from the body and merge it with the link into one
        # clean attribution line. X counts any URL as 23 chars; keep total < 280.
        body = _HT_TAIL.sub("", draft.commentary.strip()).rstrip()
        attribution = f"h/t @{post.author_handle}: {post.url}"
        max_body = 280 - (len(f"h/t @{post.author_handle}: ") + 23) - 2
        if len(body) > max_body:
            body = body[:max_body].rsplit(" ", 1)[0].rstrip()
        return f"{body}\n\n{attribution}", True
    return draft.commentary, False
