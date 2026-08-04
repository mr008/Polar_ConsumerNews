"""Outcome harvesting: metric snapshots of OUR OWN published posts.

The learning substrate for the autonomy layers (AUTONOMY.md): every published
post gets its engagement captured at fixed milestones after posting, joined to
the feature tags recorded at publish time (post_features). Owned reads cost
~$0.001 each, and a post is captured at most once per milestone, so the whole
harvester runs ~$0.50/mo at 3 posts/day.

A milestone is capturable only while its WINDOW is open — from its own offset
until the next milestone's offset. A 30h-old post with no snapshots yields one
"24h" row, not a misleading backfill of "1h"/"6h" rows captured a day late.
"""
from __future__ import annotations

# (label, hours after posting). Windows: 1h=[1,6) 6h=[6,24) 24h=[24,72)
# 72h=[72,168) 7d=[168,inf). Aligned with the 6h collect cadence so the early
# milestones are actually reachable.
MILESTONES: list[tuple[str, float]] = [
    ("1h", 1.0),
    ("6h", 6.0),
    ("24h", 24.0),
    ("72h", 72.0),
    ("7d", 168.0),
]

# Stop looking at posts older than this — the 7d milestone window has had a
# full day of collect runs to fire by then.
HARVEST_MAX_AGE_DAYS = 8

# Safety ceiling on owned reads per harvest run (belt-and-braces; the milestone
# windows already bound this to ~a handful).
HARVEST_MAX_READS_PER_RUN = 40


def due_milestone(age_hours: float, done: set[str]) -> str | None:
    """The single milestone whose window contains age_hours, unless captured."""
    for i, (label, start) in enumerate(MILESTONES):
        end = MILESTONES[i + 1][1] if i + 1 < len(MILESTONES) else float("inf")
        if start <= age_hours < end:
            return None if label in done else label
    return None
