"""Dry-run publisher: prints exactly what would be posted. No network."""
from __future__ import annotations

from ..models import Draft, Post
from .publisher import attribution_text, compose_text, wants_attribution_reply

_COUNTER = {"n": 0}


def _fake_id() -> str:
    _COUNTER["n"] += 1
    return f"dryrun-{_COUNTER['n']:04d}"


class DryRunPublisher:
    def __init__(self, cfg=None):
        self.cfg = cfg

    def reply(self, text: str, in_reply_to_tweet_id: str) -> dict:
        fake_id = _fake_id()
        print("\n" + "-" * 64)
        print(f"DRY RUN — would REPLY to {in_reply_to_tweet_id}:")
        print(text)
        print(f"  chars: {len(text)} | id: {fake_id}")
        print("-" * 64)
        return {"ok": True, "id": fake_id}

    def publish(self, draft: Draft, post: Post) -> dict:
        fake_id = _fake_id()
        text, fmt = compose_text(draft, post, self.cfg)
        label = {"quote": "quote tweet", "link": "standalone + source link",
                 "mention": "standalone (h/t mention, link in reply)"}[fmt]
        print("\n" + "=" * 64)
        print(f"DRY RUN — would post this {label}:")
        print("-" * 64)
        print(text)
        thread_ids = []
        chain = [p for p in draft.parts if p.strip()]
        if wants_attribution_reply(self.cfg) and post.url:
            chain.append(attribution_text(post))
        for i, part in enumerate(chain, 1):
            print(f"--- thread reply {i}/{len(chain)} ({len(part)} chars) ---")
            print(part)
            thread_ids.append(_fake_id())
        print("-" * 64)
        if fmt == "quote":
            print(f"  ↱ quoting @{post.author_handle}: {post.text.splitlines()[0][:70]}...")
        print(f"  source: {post.url}")
        print(f"  chars: {len(text)} | model: {draft.model} | id: {fake_id}")
        print("=" * 64)
        return {"ok": True, "id": fake_id, "mode": fmt, "thread_ids": thread_ids}
