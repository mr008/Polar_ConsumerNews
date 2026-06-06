"""Publisher interface."""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..models import Draft, Post


@runtime_checkable
class Publisher(Protocol):
    def publish(self, draft: Draft, post: Post) -> dict:
        """Post the quote tweet. Returns {'ok': bool, 'id': str}."""
        ...
