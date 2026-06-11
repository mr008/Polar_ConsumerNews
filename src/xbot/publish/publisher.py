"""Publisher interface + post composition.

posting.format decides what a published item looks like:
  - "mention": commentary + "h/t @handle" tail, NO URL anywhere in the main post.
    X buries posts containing URLs (and bills them at ~$0.20 vs $0.015), so the
    source link — if kept at all — goes in a self-reply (posting.attribution_reply).
  - "link":    legacy mode — source URL appended to the main post ($0.20 tier,
    algorithmically suppressed). Kept for back-compat/rollback only.
  - "quote":   real quote tweet (cross-account quoting broke on X ~Feb 2026).
"""
from __future__ import annotations

import re
from typing import Protocol, runtime_checkable

from ..models import Draft, Post

_HT_TAIL = re.compile(r"\s*h/?t\s+@\w+\s*$", re.IGNORECASE)
_BULLET = re.compile(r"^\s*(?:•|\d+[\.\)])\s")
URL_RE = re.compile(r"https?://|t\.co/|\bwww\.", re.IGNORECASE)


@runtime_checkable
class Publisher(Protocol):
    def publish(self, draft: Draft, post: Post) -> dict:
        """Post the item (single post or thread chain). Returns {'ok': bool, 'id': str}."""
        ...

    def reply(self, text: str, in_reply_to_tweet_id: str) -> dict:
        """Post a reply ($0.015 — must contain no URL unless it's the attribution
        reply). Returns {'ok': bool, 'id': str}."""
        ...


def posting_format(cfg) -> str:
    """Resolve posting.format with back-compat for the old boolean key."""
    if cfg is None:
        return "quote"
    fmt = cfg.get("posting.format", "")
    if fmt:
        return str(fmt)
    return "link" if cfg.get("posting.link_instead_of_quote", False) else "quote"


def strip_ht_tail(commentary: str) -> str:
    """Remove the trailing "h/t @x" (composition re-attaches it per format)."""
    return _HT_TAIL.sub("", commentary.strip()).rstrip()


def body_budget(post: Post, cfg) -> int:
    """Max commentary-body chars (h/t tail excluded) for this post in the active
    format. Mention mode keeps the tail inside the 280; link mode also loses the
    URL (23 chars on X)."""
    fmt = posting_format(cfg)
    if fmt == "link":
        return 280 - (len(f"h/t @{post.author_handle}: ") + 23) - 2
    if fmt == "mention":
        return 280 - (len(f"h/t @{post.author_handle}") + 4)  # tail + separator slack
    return 280


def part_budget(cfg) -> int:
    """Max chars for a thread continuation part (no tail, no URL)."""
    return int(cfg.get("posting.max_part_chars", 270)) if cfg else 270


def smart_trim(commentary: str, budget: int) -> str:
    """Deterministic last-resort shortener: drop trailing bullets, then trim at a
    sentence/word boundary. Preserves the h/t tail. Used only after the LLM's own
    revision attempt still came back too long — trimming beats blocking."""
    m = _HT_TAIL.search(commentary.strip())
    tail = m.group(0).strip() if m else ""
    body = strip_ht_tail(commentary)

    def too_long(b: str) -> bool:
        return len(b) > budget

    lines = body.splitlines()
    while too_long("\n".join(lines).rstrip()):
        bullet_idx = [i for i, ln in enumerate(lines) if _BULLET.match(ln)]
        if len(bullet_idx) > 2:
            lines.pop(bullet_idx[-1])
        else:
            break
    body = "\n".join(lines).rstrip()

    if too_long(body):  # still over — cut at the last sentence end inside budget
        cut = body[:budget]
        for sep in (". ", "! ", "? ", "\n"):
            idx = cut.rfind(sep)
            if idx >= budget // 2:
                cut = cut[: idx + 1]
                break
        else:
            cut = cut.rsplit(" ", 1)[0]
        body = cut.rstrip()

    return f"{body}\n\n{tail}" if tail else body


def compose_text(draft: Draft, post: Post, cfg) -> tuple[str, str]:
    """Build the MAIN post text. Returns (text, format). Thread parts and the
    attribution reply are posted by the publisher as chained self-replies."""
    fmt = posting_format(cfg)
    if fmt == "link":
        body = strip_ht_tail(draft.commentary)
        attribution = f"h/t @{post.author_handle}: {post.url}"
        max_body = body_budget(post, cfg)
        if len(body) > max_body:  # last resort — safety should have caught this
            body = smart_trim(body, max_body)
        return f"{body}\n\n{attribution}", "link"
    if fmt == "mention":
        text = draft.commentary.strip()
        if not _HT_TAIL.search(text):  # credit tail is part of the voice — ensure it
            text = f"{text}\n\nh/t @{post.author_handle}"
        if len(text) > 280:  # last resort — safety should have caught this
            text = smart_trim(text, 280 - (len(f"h/t @{post.author_handle}") + 4))
        return text, "mention"
    return draft.commentary, "quote"


def attribution_text(post: Post) -> str:
    """The hidden source link — lives in a self-reply at the bottom of the chain,
    never in the main post (URL posts are buried + cost $0.20)."""
    return f"source: {post.url}"


def wants_attribution_reply(cfg) -> bool:
    """Attribution reply applies in mention mode only (quote mode embeds the
    source; link mode already carries the URL in the main post)."""
    return bool(cfg and cfg.get("posting.attribution_reply", False)
                and posting_format(cfg) == "mention")
