"""SourceAdapter interface. Both the offline sample source and the live X API
source implement this, so the pipeline never knows which one produced the data.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..models import Post


@runtime_checkable
class SourceAdapter(Protocol):
    def fetch_timeline(self, limit: int = 120) -> list[Post]:
        """Return up to `limit` recent posts from the watched feed, normalized."""
        ...
