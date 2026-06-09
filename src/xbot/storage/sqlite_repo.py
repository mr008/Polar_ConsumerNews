"""SQLite implementation of Repository. Pure stdlib (sqlite3)."""
from __future__ import annotations

import json
import sqlite3
from datetime import timedelta, timezone
from pathlib import Path
from typing import Optional

from ..models import Draft, Metrics, Post, Score, parse_dt, to_local, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    tweet_id TEXT PRIMARY KEY,
    author_handle TEXT NOT NULL,
    author_name TEXT,
    author_follower_count INTEGER DEFAULT 0,
    text TEXT,
    created_at TEXT,
    url TEXT,
    lang TEXT,
    is_reply INTEGER DEFAULT 0,
    is_retweet INTEGER DEFAULT 0,
    is_quote INTEGER DEFAULT 0,
    has_media INTEGER DEFAULT 0,
    has_link INTEGER DEFAULT 0,
    canonical_id TEXT,
    first_seen_at TEXT
);

CREATE TABLE IF NOT EXISTS post_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tweet_id TEXT NOT NULL,
    likes INTEGER, reposts INTEGER, replies INTEGER, quotes INTEGER, views INTEGER,
    captured_at TEXT,
    UNIQUE(tweet_id, captured_at)
);

CREATE TABLE IF NOT EXISTS scores (
    tweet_id TEXT PRIMARY KEY,
    stage1_score REAL, velocity_n REAL, eng_per_follower_n REAL, echo_n REAL,
    recency_n REAL, topic_fit REAL, quote_worthy REAL, quote_score REAL,
    scored_at TEXT
);

CREATE TABLE IF NOT EXISTS candidates (
    tweet_id TEXT PRIMARY KEY,
    status TEXT,
    skip_reason TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tweet_id TEXT NOT NULL,
    commentary TEXT,
    model TEXT,
    safety_passed INTEGER,
    safety_notes TEXT,
    status TEXT DEFAULT 'pending',
    note TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS posted_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_tweet_id TEXT,
    our_tweet_id TEXT,
    author_handle TEXT,
    source_text TEXT,
    commentary TEXT,
    posted_at TEXT,
    posted_at_pt TEXT
);

CREATE TABLE IF NOT EXISTS run_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT,
    ts_pt TEXT,
    kind TEXT,
    n_read INTEGER DEFAULT 0,
    n_judged INTEGER DEFAULT 0,
    n_drafted INTEGER DEFAULT 0,
    n_posted INTEGER DEFAULT 0,
    detail TEXT
);

CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);

CREATE INDEX IF NOT EXISTS idx_posts_author ON posts(author_handle);
CREATE INDEX IF NOT EXISTS idx_metrics_tweet ON post_metrics(tweet_id);
CREATE INDEX IF NOT EXISTS idx_posted_source ON posted_log(source_tweet_id);
CREATE INDEX IF NOT EXISTS idx_candidates_status ON candidates(status);
"""


class SqliteRepository:
    # Local posting timezone (config posting.timezone); set by get_repository().
    # Drives the stored posted_at_pt column, the daily-cap day boundary, and
    # report display. Internal date math stays UTC.
    tz_name = "UTC"

    def __init__(self, path: str = "data/state.db"):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL;")

    def init_schema(self) -> None:
        self.conn.executescript(SCHEMA)
        # Migration (2026-06): posted_at_pt on pre-existing DBs. Errors = exists.
        try:
            self.conn.execute("ALTER TABLE posted_log ADD COLUMN posted_at_pt TEXT")
        except Exception:
            pass
        self.conn.commit()

    # ---------- posts + metrics ----------
    def upsert_post(self, post: Post) -> None:
        self.conn.execute(
            """INSERT INTO posts (tweet_id, author_handle, author_name, author_follower_count,
                   text, created_at, url, lang, is_reply, is_retweet, is_quote,
                   has_media, has_link, canonical_id, first_seen_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(tweet_id) DO UPDATE SET
                   author_follower_count=excluded.author_follower_count,
                   text=excluded.text""",
            (post.tweet_id, post.author_handle, post.author_name, post.author_follower_count,
             post.text, post.created_at.isoformat(), post.url, post.lang,
             int(post.is_reply), int(post.is_retweet), int(post.is_quote),
             int(post.has_media), int(post.has_link), post.canonical_id,
             utcnow().isoformat()),
        )
        self.add_metrics(post.tweet_id, post.metrics)
        self.conn.commit()

    def add_metrics(self, tweet_id: str, metrics: Metrics) -> None:
        self.conn.execute(
            """INSERT OR IGNORE INTO post_metrics
                   (tweet_id, likes, reposts, replies, quotes, views, captured_at)
               VALUES (?,?,?,?,?,?,?)""",
            (tweet_id, metrics.likes, metrics.reposts, metrics.replies,
             metrics.quotes, metrics.views, metrics.captured_at.isoformat()),
        )
        self.conn.commit()

    def _row_to_post(self, row: sqlite3.Row) -> Post:
        m = self.conn.execute(
            "SELECT * FROM post_metrics WHERE tweet_id=? ORDER BY captured_at DESC LIMIT 1",
            (row["tweet_id"],),
        ).fetchone()
        metrics = Metrics(
            likes=m["likes"], reposts=m["reposts"], replies=m["replies"],
            quotes=m["quotes"], views=m["views"], captured_at=parse_dt(m["captured_at"]),
        ) if m else Metrics()
        return Post(
            tweet_id=row["tweet_id"], author_handle=row["author_handle"],
            author_name=row["author_name"], text=row["text"],
            created_at=row["created_at"], url=row["url"] or "",
            author_follower_count=row["author_follower_count"] or 0,
            lang=row["lang"] or "en",
            is_reply=bool(row["is_reply"]), is_retweet=bool(row["is_retweet"]),
            is_quote=bool(row["is_quote"]), has_media=bool(row["has_media"]),
            has_link=bool(row["has_link"]), metrics=metrics,
            canonical_id=row["canonical_id"],
        )

    def get_post(self, tweet_id: str) -> Optional[Post]:
        row = self.conn.execute("SELECT * FROM posts WHERE tweet_id=?", (tweet_id,)).fetchone()
        return self._row_to_post(row) if row else None

    def recent_posts(self, within_hours: float = 72) -> list[Post]:
        cutoff = (utcnow() - timedelta(hours=within_hours)).isoformat()
        rows = self.conn.execute(
            "SELECT * FROM posts WHERE created_at >= ? ORDER BY created_at DESC", (cutoff,)
        ).fetchall()
        return [self._row_to_post(r) for r in rows]

    def max_seen_tweet_id(self) -> Optional[str]:
        """Newest tweet id we've already stored (snowflake ids sort numerically).
        Used as `since_id` so collect never re-buys posts it already read."""
        r = self.conn.execute(
            "SELECT tweet_id FROM posts ORDER BY CAST(tweet_id AS INTEGER) DESC LIMIT 1"
        ).fetchone()
        return r["tweet_id"] if r else None

    def metrics_history(self, tweet_id: str) -> list[Metrics]:
        rows = self.conn.execute(
            "SELECT * FROM post_metrics WHERE tweet_id=? ORDER BY captured_at ASC", (tweet_id,)
        ).fetchall()
        return [Metrics(r["likes"], r["reposts"], r["replies"], r["quotes"], r["views"],
                        parse_dt(r["captured_at"])) for r in rows]

    # ---------- scoring + candidates ----------
    def save_score(self, s: Score) -> None:
        self.conn.execute(
            """INSERT INTO scores (tweet_id, stage1_score, velocity_n, eng_per_follower_n,
                   echo_n, recency_n, topic_fit, quote_worthy, quote_score, scored_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(tweet_id) DO UPDATE SET
                   stage1_score=excluded.stage1_score, velocity_n=excluded.velocity_n,
                   eng_per_follower_n=excluded.eng_per_follower_n, echo_n=excluded.echo_n,
                   recency_n=excluded.recency_n, topic_fit=excluded.topic_fit,
                   quote_worthy=excluded.quote_worthy, quote_score=excluded.quote_score,
                   scored_at=excluded.scored_at""",
            (s.tweet_id, s.stage1_score, s.velocity_n, s.eng_per_follower_n, s.echo_n,
             s.recency_n, s.topic_fit, s.quote_worthy, s.quote_score, s.scored_at.isoformat()),
        )
        self.conn.commit()

    def get_score(self, tweet_id: str) -> Optional[Score]:
        r = self.conn.execute("SELECT * FROM scores WHERE tweet_id=?", (tweet_id,)).fetchone()
        if not r:
            return None
        return Score(
            tweet_id=r["tweet_id"], stage1_score=r["stage1_score"], velocity_n=r["velocity_n"],
            eng_per_follower_n=r["eng_per_follower_n"], echo_n=r["echo_n"], recency_n=r["recency_n"],
            topic_fit=r["topic_fit"], quote_worthy=r["quote_worthy"], quote_score=r["quote_score"],
            scored_at=parse_dt(r["scored_at"]),
        )

    def set_candidate(self, tweet_id: str, status: str, skip_reason: str = "") -> None:
        self.conn.execute(
            """INSERT INTO candidates (tweet_id, status, skip_reason, updated_at)
               VALUES (?,?,?,?)
               ON CONFLICT(tweet_id) DO UPDATE SET
                   status=excluded.status, skip_reason=excluded.skip_reason,
                   updated_at=excluded.updated_at""",
            (tweet_id, status, skip_reason, utcnow().isoformat()),
        )
        self.conn.commit()

    def candidates(self, status: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT tweet_id FROM candidates WHERE status=?", (status,)
        ).fetchall()
        return [r["tweet_id"] for r in rows]

    # ---------- drafts ----------
    def add_draft(self, draft: Draft, status: str = "pending") -> int:
        cur = self.conn.execute(
            """INSERT INTO drafts (tweet_id, commentary, model, safety_passed,
                   safety_notes, status, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (draft.tweet_id, draft.commentary, draft.model, int(draft.safety_passed),
             draft.safety_notes, status, draft.created_at.isoformat()),
        )
        self.conn.commit()
        return cur.lastrowid

    def _row_to_draft(self, r: sqlite3.Row) -> Draft:
        return Draft(
            tweet_id=r["tweet_id"], commentary=r["commentary"], model=r["model"],
            safety_passed=bool(r["safety_passed"]), safety_notes=r["safety_notes"] or "",
            created_at=parse_dt(r["created_at"]),
        )

    def expire_stale_drafts(self, max_age_hours: float) -> int:
        """Mark pending drafts older than max_age_hours as 'stale' — their moment
        has passed and the queue should refill with fresh material. (COUNT-then-
        UPDATE because the Turso cursor shim has no rowcount.)"""
        cutoff = (utcnow() - timedelta(hours=max_age_hours)).isoformat()
        n = self.conn.execute(
            "SELECT COUNT(*) AS c FROM drafts WHERE status='pending' AND created_at < ?",
            (cutoff,),
        ).fetchone()["c"]
        if n:
            self.conn.execute(
                "UPDATE drafts SET status='stale', note='expired' "
                "WHERE status='pending' AND created_at < ?",
                (cutoff,),
            )
            self.conn.commit()
        return n

    def pending_drafts(self) -> list[tuple[int, Draft, Post]]:
        rows = self.conn.execute(
            "SELECT * FROM drafts WHERE status='pending' ORDER BY id ASC"
        ).fetchall()
        out = []
        for r in rows:
            post = self.get_post(r["tweet_id"])
            if post:
                out.append((r["id"], self._row_to_draft(r), post))
        return out

    def get_draft(self, draft_id: int) -> Optional[tuple[Draft, Post]]:
        r = self.conn.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
        if not r:
            return None
        post = self.get_post(r["tweet_id"])
        return (self._row_to_draft(r), post) if post else None

    def set_draft_status(self, draft_id: int, status: str, note: str = "") -> None:
        self.conn.execute(
            "UPDATE drafts SET status=?, note=? WHERE id=?", (status, note, draft_id)
        )
        self.conn.commit()

    # ---------- posted log ----------
    def has_posted(self, source_tweet_id: str) -> bool:
        r = self.conn.execute(
            "SELECT 1 FROM posted_log WHERE source_tweet_id=? LIMIT 1", (source_tweet_id,)
        ).fetchone()
        return r is not None

    def posted_authors_since(self, days: int) -> set[str]:
        cutoff = (utcnow() - timedelta(days=days)).isoformat()
        rows = self.conn.execute(
            "SELECT DISTINCT author_handle FROM posted_log WHERE posted_at >= ?", (cutoff,)
        ).fetchall()
        return {r["author_handle"] for r in rows}

    def posted_source_texts(self) -> list[str]:
        rows = self.conn.execute("SELECT source_text FROM posted_log").fetchall()
        return [r["source_text"] or "" for r in rows]

    def log_posted(self, source_tweet_id, our_tweet_id, author_handle,
                   source_text, commentary) -> None:
        now = utcnow()
        self.conn.execute(
            """INSERT INTO posted_log (source_tweet_id, our_tweet_id, author_handle,
                   source_text, commentary, posted_at, posted_at_pt)
               VALUES (?,?,?,?,?,?,?)""",
            (source_tweet_id, our_tweet_id, author_handle, source_text, commentary,
             now.isoformat(), to_local(now, self.tz_name).isoformat()),
        )
        self.conn.commit()

    # ---------- run log (per-run counters: read/judged/drafted/posted) ----------
    def log_run(self, kind: str, read: int = 0, judged: int = 0, drafted: int = 0,
                posted: int = 0, detail: str = "") -> None:
        now = utcnow()
        self.conn.execute(
            "INSERT INTO run_log (ts, ts_pt, kind, n_read, n_judged, n_drafted, "
            "n_posted, detail) VALUES (?,?,?,?,?,?,?,?)",
            (now.isoformat(), to_local(now, self.tz_name).isoformat(), kind,
             read, judged, drafted, posted, (detail or "")[:300]),
        )
        self.conn.commit()

    def recent_runs(self, within_hours: float = 72) -> list[dict]:
        cutoff = (utcnow() - timedelta(hours=within_hours)).isoformat()
        rows = self.conn.execute(
            "SELECT ts, kind, n_read, n_judged, n_drafted, n_posted, detail "
            "FROM run_log WHERE ts >= ? ORDER BY ts ASC",
            (cutoff,),
        ).fetchall()
        out = []
        for r in rows:
            local = to_local(r["ts"], self.tz_name)
            out.append({"ts": local.isoformat(), "tz": local.tzname() or "UTC",
                        "kind": r["kind"], "read": r["n_read"], "judged": r["n_judged"],
                        "drafted": r["n_drafted"], "posted": r["n_posted"],
                        "detail": r["detail"] or ""})
        return out

    # ---------- activity log (surfaced by `xbot report`) ----------
    def activity_posted(self, within_hours: float = 72) -> list[dict]:
        cutoff = (utcnow() - timedelta(hours=within_hours)).isoformat()
        rows = self.conn.execute(
            "SELECT posted_at, author_handle, our_tweet_id, commentary FROM posted_log "
            "WHERE posted_at >= ? ORDER BY posted_at DESC",
            (cutoff,),
        ).fetchall()
        out = []
        for r in rows:
            # Convert on read (covers rows written before posted_at_pt existed).
            local = to_local(r["posted_at"], self.tz_name)
            out.append({
                "posted_at": local.isoformat(),
                "tz": local.tzname() or "UTC",
                "author": r["author_handle"],
                "url": f"https://x.com/i/status/{r['our_tweet_id']}" if r["our_tweet_id"] else "",
                "commentary": ((r["commentary"] or "").splitlines() or [""])[0][:100],
            })
        return out

    def activity_drafts(self, statuses: list[str], within_hours: float = 72) -> list[dict]:
        """Drafts that went wrong (failed/blocked/stale), newest first. The time
        filter uses the draft's created_at (drafts expire at 48h anyway)."""
        cutoff = (utcnow() - timedelta(hours=within_hours)).isoformat()
        qmarks = ",".join("?" * len(statuses))
        rows = self.conn.execute(
            f"SELECT d.id, d.status, d.note, d.safety_notes, d.tweet_id, p.author_handle "
            f"FROM drafts d LEFT JOIN posts p ON p.tweet_id = d.tweet_id "
            f"WHERE d.status IN ({qmarks}) AND d.created_at >= ? ORDER BY d.id DESC",
            (*statuses, cutoff),
        ).fetchall()
        return [{
            "draft_id": r["id"],
            "status": r["status"],
            "author": r["author_handle"] or "?",
            "note": (r["note"] or r["safety_notes"] or "")[:200],
        } for r in rows]

    def count_posted_today(self) -> int:
        """'Today' = the local posting day (posting.timezone), so the 3/day cap
        aligns with the PT posting windows instead of UTC midnight (~4-5pm PT)."""
        start_local = to_local(utcnow(), self.tz_name).replace(
            hour=0, minute=0, second=0, microsecond=0)
        start = start_local.astimezone(timezone.utc).isoformat()
        r = self.conn.execute(
            "SELECT COUNT(*) AS c FROM posted_log WHERE posted_at >= ?", (start,)
        ).fetchone()
        return r["c"]

    # ---------- state ----------
    def get_state(self, key: str, default: str = "") -> str:
        r = self.conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default

    def set_state(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO state (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value) if not isinstance(value, str) else value),
        )
        self.conn.commit()
