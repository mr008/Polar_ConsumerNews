"""Cheap text similarity (token Jaccard). Used for echo detection (ranking) and
near-duplicate detection (dedup). The real version swaps in embeddings; the
interface (similarity in [0,1]) stays identical.
"""
from __future__ import annotations

import re

_WORD = re.compile(r"[a-z0-9']+")
_STOP = {
    "the", "a", "an", "to", "of", "and", "or", "in", "on", "for", "your", "you",
    "it", "is", "this", "that", "with", "all", "from", "just", "have", "has",
    "i", "my", "me", "we", "they", "them", "their", "at", "by", "as", "be",
}


def tokens(text: str) -> set[str]:
    return {w for w in _WORD.findall((text or "").lower()) if w not in _STOP and len(w) > 1}


def similarity(a: str, b: str) -> float:
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    return inter / union if union else 0.0
