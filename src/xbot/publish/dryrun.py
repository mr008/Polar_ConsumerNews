"""Dry-run publisher: prints exactly what would be posted. No network."""
from __future__ import annotations

from ..models import Draft, Post

_COUNTER = {"n": 0}


class DryRunPublisher:
    def publish(self, draft: Draft, post: Post) -> dict:
        _COUNTER["n"] += 1
        fake_id = f"dryrun-{_COUNTER['n']:04d}"
        print("\n" + "=" * 64)
        print("DRY RUN — would post this quote tweet:")
        print("-" * 64)
        print(draft.commentary)
        print("-" * 64)
        print(f"  ↱ quoting @{post.author_handle}: {post.text.splitlines()[0][:70]}...")
        print(f"  (source: {post.url})")
        print(f"  chars: {len(draft.commentary)} | model: {draft.model} | id: {fake_id}")
        print("=" * 64)
        return {"ok": True, "id": fake_id}
