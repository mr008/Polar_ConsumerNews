"""Offline source: loads fixtures so the whole pipeline runs without X API access.

Timestamps in the fixture are relative ("hours_ago") so posts always look recent
regardless of when you run it.
"""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from ..models import Post, utcnow
from .normalize import normalize


class SampleSource:
    def __init__(self, fixture_path: str = "fixtures/sample_posts.json"):
        self.fixture_path = Path(fixture_path)

    def fetch_timeline(self, limit: int = 120) -> list[Post]:
        if not self.fixture_path.exists():
            raise SystemExit(f"fixture not found: {self.fixture_path.resolve()}")
        raw = json.loads(self.fixture_path.read_text(encoding="utf-8"))
        posts: list[Post] = []
        now = utcnow()
        for item in raw[:limit]:
            item = dict(item)
            hours_ago = item.pop("hours_ago", 1)
            item["created_at"] = (now - timedelta(hours=hours_ago)).isoformat()
            m = item.get("metrics", {})
            m.setdefault("captured_at", now.isoformat())
            item["metrics"] = m
            posts.append(normalize(Post.from_dict(item)))
        return posts
