"""Core data models. Plain dataclasses so the dry-run pipeline needs no extra deps.

Both the sample source and the (future) live X API source normalize into `Post`,
so everything downstream is source-agnostic.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_dt(value) -> datetime:
    """Accept ISO strings or datetimes; always return tz-aware UTC."""
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_local(value, tz_name: str) -> datetime:
    """UTC timestamp -> tz-aware local time (e.g. PT). Internal date math stays
    UTC; this converts at the storage/display edge. Falls back to UTC if the
    timezone database is unavailable rather than crashing."""
    dt = parse_dt(value)
    if not tz_name or tz_name == "UTC":
        return dt
    try:
        from zoneinfo import ZoneInfo
        return dt.astimezone(ZoneInfo(tz_name))
    except Exception:
        return dt


@dataclass
class Metrics:
    likes: int = 0
    reposts: int = 0
    replies: int = 0
    quotes: int = 0
    views: int = 0
    captured_at: datetime = field(default_factory=utcnow)

    @property
    def total_engagement(self) -> int:
        return self.likes + self.reposts + self.replies + self.quotes


@dataclass
class Post:
    tweet_id: str
    author_handle: str
    author_name: str
    text: str
    created_at: datetime
    url: str = ""
    author_follower_count: int = 0
    lang: str = "en"
    is_reply: bool = False
    is_retweet: bool = False
    is_quote: bool = False
    has_media: bool = False
    has_link: bool = False
    metrics: Metrics = field(default_factory=Metrics)
    # canonical_id collapses retweets/quotes back to the original being amplified
    canonical_id: Optional[str] = None

    def __post_init__(self):
        self.created_at = parse_dt(self.created_at)
        if self.canonical_id is None:
            self.canonical_id = self.tweet_id

    @property
    def age_hours(self) -> float:
        return max((utcnow() - self.created_at).total_seconds() / 3600.0, 0.0)

    @classmethod
    def from_dict(cls, d: dict) -> "Post":
        m = d.get("metrics", {}) or {}
        metrics = Metrics(
            likes=int(m.get("likes", 0) or 0),
            reposts=int(m.get("reposts", 0) or 0),
            replies=int(m.get("replies", 0) or 0),
            quotes=int(m.get("quotes", 0) or 0),
            views=int(m.get("views", 0) or 0),
            captured_at=parse_dt(m["captured_at"]) if m.get("captured_at") else utcnow(),
        )
        known = {f for f in cls.__dataclass_fields__ if f not in ("metrics",)}
        kwargs = {k: v for k, v in d.items() if k in known}
        return cls(metrics=metrics, **kwargs)


@dataclass
class Score:
    tweet_id: str
    likes_n: float = 0.0
    reposts_n: float = 0.0
    velocity_n: float = 0.0
    eng_per_follower_n: float = 0.0
    echo_n: float = 0.0
    recency_n: float = 0.0
    topic_fit: float = 0.0
    stage1_score: float = 0.0
    quote_worthy: float = 0.0
    quote_score: float = 0.0
    scored_at: datetime = field(default_factory=utcnow)


@dataclass
class Draft:
    tweet_id: str
    commentary: str
    model: str
    safety_passed: bool = False
    safety_notes: str = ""
    created_at: datetime = field(default_factory=utcnow)


def to_jsonable(obj) -> dict:
    """asdict() with datetimes turned into ISO strings, for storage/logging."""
    def conv(v):
        if isinstance(v, datetime):
            return v.isoformat()
        if isinstance(v, dict):
            return {k: conv(x) for k, x in v.items()}
        if isinstance(v, list):
            return [conv(x) for x in v]
        return v
    return {k: conv(v) for k, v in asdict(obj).items()}
