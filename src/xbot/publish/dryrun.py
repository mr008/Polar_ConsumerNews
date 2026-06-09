"""Dry-run publisher: prints exactly what would be posted. No network."""
from __future__ import annotations

from ..models import Draft, Post
from .publisher import compose_text

_COUNTER = {"n": 0}


class DryRunPublisher:
    def __init__(self, cfg=None):
        self.cfg = cfg

    def publish(self, draft: Draft, post: Post) -> dict:
        _COUNTER["n"] += 1
        fake_id = f"dryrun-{_COUNTER['n']:04d}"
        text, link_mode = compose_text(draft, post, self.cfg)
        print("\n" + "=" * 64)
        print("DRY RUN — would post this" +
              (" (standalone + source link):" if link_mode else " quote tweet:"))
        print("-" * 64)
        print(text)
        print("-" * 64)
        if not link_mode:
            print(f"  ↱ quoting @{post.author_handle}: {post.text.splitlines()[0][:70]}...")
        print(f"  source: {post.url}")
        print(f"  chars: {len(text)} | model: {draft.model} | id: {fake_id}")
        print("=" * 64)
        return {"ok": True, "id": fake_id, "mode": "link" if link_mode else "quote"}
