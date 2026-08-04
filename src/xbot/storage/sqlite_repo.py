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
    first_seen_at TEXT,
    reply_settings TEXT
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
    judged INTEGER DEFAULT 0,
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
    parts TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS reply_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_tweet_id TEXT,
    target_author TEXT,
    target_text TEXT,
    reply_text TEXT,
    model TEXT,
    status TEXT,
    note TEXT,
    our_tweet_id TEXT,
    created_at TEXT,
    posted_at TEXT,
    posted_at_pt TEXT
);

CREATE TABLE IF NOT EXISTS account_metrics (
    day TEXT PRIMARY KEY,
    followers INTEGER,
    following INTEGER,
    tweet_count INTEGER,
    captured_at TEXT
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
    n_replied INTEGER DEFAULT 0,
    detail TEXT
);

CREATE TABLE IF NOT EXISTS post_outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    our_tweet_id TEXT NOT NULL,
    milestone TEXT NOT NULL,
    likes INTEGER, reposts INTEGER, replies INTEGER, quotes INTEGER, views INTEGER,
    captured_at TEXT,
    UNIQUE(our_tweet_id, milestone)
);

CREATE TABLE IF NOT EXISTS post_features (
    our_tweet_id TEXT PRIMARY KEY,
    source_tweet_id TEXT,
    author_handle TEXT,
    route TEXT,
    kind TEXT,
    format TEXT,
    parts_n INTEGER DEFAULT 0,
    chars INTEGER DEFAULT 0,
    has_question INTEGER DEFAULT 0,
    hook TEXT,
    window_hour INTEGER,
    teaching REAL,
    topic_fit REAL,
    quote_score REAL,
    posted_at TEXT
);

CREATE TABLE IF NOT EXISTS agent_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT,
    ts_pt TEXT,
    session TEXT,
    model TEXT,
    turns INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0,
    detail TEXT
);

CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);

CREATE INDEX IF NOT EXISTS idx_posts_author ON posts(author_handle);
CREATE INDEX IF NOT EXISTS idx_metrics_tweet ON post_metrics(tweet_id);
CREATE INDEX IF NOT EXISTS idx_posted_source ON posted_log(source_tweet_id);
CREATE INDEX IF NOT EXISTS idx_candidates_status ON candidates(status);
CREATE INDEX IF NOT EXISTS idx_reply_target ON reply_log(target_tweet_id);
CREATE INDEX IF NOT EXISTS idx_reply_author ON reply_log(target_author);
CREATE INDEX IF NOT EXISTS idx_outcomes_tweet ON post_outcomes(our_tweet_id);
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
        # Migrations (2026-06) for pre-existing DBs. Errors = column exists.
        for ddl in ("ALTER TABLE posted_log ADD COLUMN posted_at_pt TEXT",
                    "ALTER TABLE scores ADD COLUMN judged INTEGER DEFAULT 0",
                    "ALTER TABLE drafts ADD COLUMN parts TEXT",
                    "ALTER TABLE run_log ADD COLUMN n_replied INTEGER DEFAULT 0",
                    "ALTER TABLE posts ADD COLUMN reply_settings TEXT"):
            try:
                self.conn.execute(ddl)
            except Exception:
                pass
        self.conn.commit()

    # ---------- posts + metrics ----------
    def upsert_post(self, post: Post) -> None:
        self.conn.execute(
            """INSERT INTO posts (tweet_id, author_handle, author_name, author_follower_count,
                   text, created_at, url, lang, is_reply, is_retweet, is_quote,
                   has_media, has_link, canonical_id, first_seen_at, reply_settings)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(tweet_id) DO UPDATE SET
                   author_follower_count=excluded.author_follower_count,
                   text=excluded.text,
                   reply_settings=COALESCE(excluded.reply_settings, posts.reply_settings)""",
            (post.tweet_id, post.author_handle, post.author_name, post.author_follower_count,
             post.text, post.created_at.isoformat(), post.url, post.lang,
             int(post.is_reply), int(post.is_retweet), int(post.is_quote),
             int(post.has_media), int(post.has_link), post.canonical_id,
             utcnow().isoformat(), post.reply_settings),
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
            reply_settings=row["reply_settings"],
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
                   echo_n, recency_n, topic_fit, quote_worthy, quote_score, judged, scored_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(tweet_id) DO UPDATE SET
                   stage1_score=excluded.stage1_score, velocity_n=excluded.velocity_n,
                   eng_per_follower_n=excluded.eng_per_follower_n, echo_n=excluded.echo_n,
                   recency_n=excluded.recency_n, topic_fit=excluded.topic_fit,
                   quote_worthy=excluded.quote_worthy, quote_score=excluded.quote_score,
                   judged=excluded.judged, scored_at=excluded.scored_at""",
            (s.tweet_id, s.stage1_score, s.velocity_n, s.eng_per_follower_n, s.echo_n,
             s.recency_n, s.topic_fit, s.quote_worthy, s.quote_score, int(s.judged),
             s.scored_at.isoformat()),
        )
        self.conn.commit()

    def get_score(self, tweet_id: str) -> Optional[Score]:
        r = self.conn.execute("SELECT * FROM scores WHERE tweet_id=?", (tweet_id,)).fetchone()
        if not r:
            return None
        try:
            judged = bool(r["judged"])
        except (KeyError, IndexError):
            judged = False
        return Score(
            tweet_id=r["tweet_id"], stage1_score=r["stage1_score"], velocity_n=r["velocity_n"],
            eng_per_follower_n=r["eng_per_follower_n"], echo_n=r["echo_n"], recency_n=r["recency_n"],
            topic_fit=r["topic_fit"], quote_worthy=r["quote_worthy"], quote_score=r["quote_score"],
            judged=judged, scored_at=parse_dt(r["scored_at"]),
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
                   safety_notes, status, parts, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (draft.tweet_id, draft.commentary, draft.model, int(draft.safety_passed),
             draft.safety_notes, status, json.dumps(draft.parts or []),
             draft.created_at.isoformat()),
        )
        self.conn.commit()
        return cur.lastrowid

    def _row_to_draft(self, r: sqlite3.Row) -> Draft:
        try:
            parts = json.loads(r["parts"]) if r["parts"] else []
        except (KeyError, IndexError, ValueError, TypeError):
            parts = []
        return Draft(
            tweet_id=r["tweet_id"], commentary=r["commentary"], model=r["model"],
            safety_passed=bool(r["safety_passed"]), safety_notes=r["safety_notes"] or "",
            parts=parts if isinstance(parts, list) else [],
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

    def drafted_tweet_ids(self) -> set[str]:
        """Posts that already have a draft in ANY status — one Sonnet attempt per
        post, ever (blocked posts must not be re-rolled every collect run)."""
        rows = self.conn.execute("SELECT DISTINCT tweet_id FROM drafts").fetchall()
        return {r["tweet_id"] for r in rows}

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

    # ---------- run log (per-run counters: read/judged/drafted/posted/replied) ----------
    def log_run(self, kind: str, read: int = 0, judged: int = 0, drafted: int = 0,
                posted: int = 0, replied: int = 0, detail: str = "") -> None:
        now = utcnow()
        self.conn.execute(
            "INSERT INTO run_log (ts, ts_pt, kind, n_read, n_judged, n_drafted, "
            "n_posted, n_replied, detail) VALUES (?,?,?,?,?,?,?,?,?)",
            (now.isoformat(), to_local(now, self.tz_name).isoformat(), kind,
             read, judged, drafted, posted, replied, (detail or "")[:300]),
        )
        self.conn.commit()

    def recent_runs(self, within_hours: float = 72) -> list[dict]:
        cutoff = (utcnow() - timedelta(hours=within_hours)).isoformat()
        rows = self.conn.execute(
            "SELECT ts, kind, n_read, n_judged, n_drafted, n_posted, n_replied, detail "
            "FROM run_log WHERE ts >= ? ORDER BY ts ASC",
            (cutoff,),
        ).fetchall()
        out = []
        for r in rows:
            local = to_local(r["ts"], self.tz_name)
            out.append({"ts": local.isoformat(), "tz": local.tzname() or "UTC",
                        "kind": r["kind"], "read": r["n_read"], "judged": r["n_judged"],
                        "drafted": r["n_drafted"], "posted": r["n_posted"],
                        "replied": r["n_replied"] or 0,
                        "detail": r["detail"] or ""})
        return out

    def reads_this_month(self) -> int:
        """Paid X reads since the 1st of the current local month (circuit breaker)."""
        start_local = to_local(utcnow(), self.tz_name).replace(
            day=1, hour=0, minute=0, second=0, microsecond=0)
        start = start_local.astimezone(timezone.utc).isoformat()
        r = self.conn.execute(
            "SELECT COALESCE(SUM(n_read), 0) AS c FROM run_log WHERE ts >= ?", (start,)
        ).fetchone()
        return r["c"]

    def author_yield(self, within_days: int | None = None) -> list[dict]:
        """Per-author funnel. AVERAGE teaching score (consistency) is the signal
        that matters for the curated read-List — `max_qw` rewards a single fluke,
        `avg_qw` rewards reliable teachers. Also returns judged count (enough
        signal?), last_post (went quiet?), and posts published. `within_days`
        windows it to recent activity so the auto-updater reacts to drift."""
        where, params = "", ()
        if within_days:
            cutoff = (utcnow() - timedelta(days=within_days)).isoformat()
            where, params = "WHERE p.created_at >= ?", (cutoff,)
        rows = self.conn.execute(
            "SELECT p.author_handle AS handle, COUNT(*) AS reads, "
            "SUM(CASE WHEN s.judged = 1 THEN 1 ELSE 0 END) AS judged, "
            "AVG(CASE WHEN s.judged = 1 THEN s.quote_worthy END) AS avg_qw, "
            "MAX(s.quote_worthy) AS max_qw, "
            "MAX(p.created_at) AS last_post, "
            "SUM(CASE WHEN pl.source_tweet_id IS NOT NULL THEN 1 ELSE 0 END) AS posted "
            "FROM posts p "
            "LEFT JOIN scores s ON p.tweet_id = s.tweet_id "
            "LEFT JOIN posted_log pl ON p.tweet_id = pl.source_tweet_id "
            f"{where} "
            "GROUP BY p.author_handle", params
        ).fetchall()
        return [{"handle": r["handle"], "reads": r["reads"] or 0,
                 "judged": r["judged"] or 0, "avg_qw": r["avg_qw"] or 0.0,
                 "max_qw": r["max_qw"] or 0.0, "last_post": r["last_post"] or "",
                 "posted": r["posted"] or 0} for r in rows]

    def daily_run_totals(self, days: int = 7) -> list[dict]:
        """Per-PT-day totals of the run counters (read/judged/drafted/posted/replied)."""
        cutoff = (utcnow() - timedelta(days=days)).isoformat()
        rows = self.conn.execute(
            "SELECT substr(ts_pt, 1, 10) AS day, SUM(n_read) AS n_read, "
            "SUM(n_judged) AS n_judged, SUM(n_drafted) AS n_drafted, "
            "SUM(n_posted) AS n_posted, SUM(n_replied) AS n_replied "
            "FROM run_log WHERE ts >= ? "
            "GROUP BY day ORDER BY day ASC",
            (cutoff,),
        ).fetchall()
        return [{"day": r["day"], "read": r["n_read"] or 0, "judged": r["n_judged"] or 0,
                 "drafted": r["n_drafted"] or 0, "posted": r["n_posted"] or 0,
                 "replied": r["n_replied"] or 0}
                for r in rows]

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

    # ---------- reply log (auto-reply engine) ----------
    def log_reply(self, target_tweet_id: str, target_author: str, target_text: str,
                  reply_text: str, model: str, status: str, note: str = "",
                  our_tweet_id: str = "") -> int:
        now = utcnow()
        posted = now.isoformat() if status == "posted" else None
        posted_pt = to_local(now, self.tz_name).isoformat() if status == "posted" else None
        cur = self.conn.execute(
            """INSERT INTO reply_log (target_tweet_id, target_author, target_text,
                   reply_text, model, status, note, our_tweet_id,
                   created_at, posted_at, posted_at_pt)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (target_tweet_id, target_author, (target_text or "")[:500], reply_text,
             model, status, (note or "")[:300], our_tweet_id,
             now.isoformat(), posted, posted_pt),
        )
        self.conn.commit()
        return cur.lastrowid

    def has_replied(self, target_tweet_id: str) -> bool:
        """Any reply_log row for the target — including blocked ones, so a
        failed target is never retried."""
        r = self.conn.execute(
            "SELECT 1 FROM reply_log WHERE target_tweet_id=? LIMIT 1",
            (target_tweet_id,),
        ).fetchone()
        return r is not None

    def reply_authors_since(self, days: int) -> set[str]:
        cutoff = (utcnow() - timedelta(days=days)).isoformat()
        rows = self.conn.execute(
            "SELECT DISTINCT target_author FROM reply_log "
            "WHERE status='posted' AND posted_at >= ?", (cutoff,),
        ).fetchall()
        return {r["target_author"] for r in rows}

    def count_replies_today(self) -> int:
        """'Today' = the local posting day, matching count_posted_today."""
        start_local = to_local(utcnow(), self.tz_name).replace(
            hour=0, minute=0, second=0, microsecond=0)
        start = start_local.astimezone(timezone.utc).isoformat()
        r = self.conn.execute(
            "SELECT COUNT(*) AS c FROM reply_log WHERE status='posted' AND posted_at >= ?",
            (start,),
        ).fetchone()
        return r["c"]

    def last_reply_at(self):
        r = self.conn.execute(
            "SELECT posted_at FROM reply_log WHERE status='posted' "
            "ORDER BY posted_at DESC LIMIT 1"
        ).fetchone()
        return parse_dt(r["posted_at"]) if r and r["posted_at"] else None

    def activity_replies(self, within_hours: float = 72) -> list[dict]:
        cutoff = (utcnow() - timedelta(hours=within_hours)).isoformat()
        rows = self.conn.execute(
            "SELECT target_author, reply_text, status, note, our_tweet_id, "
            "created_at, posted_at FROM reply_log WHERE created_at >= ? "
            "ORDER BY created_at DESC",
            (cutoff,),
        ).fetchall()
        out = []
        for r in rows:
            local = to_local(r["posted_at"] or r["created_at"], self.tz_name)
            out.append({
                "at": local.isoformat(), "tz": local.tzname() or "UTC",
                "author": r["target_author"], "status": r["status"],
                "note": (r["note"] or "")[:120],
                "url": f"https://x.com/i/status/{r['our_tweet_id']}" if r["our_tweet_id"] else "",
                "reply": ((r["reply_text"] or "").splitlines() or [""])[0][:100],
            })
        return out

    # ---------- account metrics (follower trend) ----------
    def snapshot_account(self, day: str, followers: int, following: int,
                         tweet_count: int) -> None:
        self.conn.execute(
            """INSERT INTO account_metrics (day, followers, following, tweet_count, captured_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(day) DO UPDATE SET
                   followers=excluded.followers, following=excluded.following,
                   tweet_count=excluded.tweet_count, captured_at=excluded.captured_at""",
            (day, followers, following, tweet_count, utcnow().isoformat()),
        )
        self.conn.commit()

    def account_history(self, days: int = 14) -> list[dict]:
        rows = self.conn.execute(
            "SELECT day, followers, following, tweet_count FROM account_metrics "
            "ORDER BY day DESC LIMIT ?", (days,),
        ).fetchall()
        return [{"day": r["day"], "followers": r["followers"],
                 "following": r["following"], "tweet_count": r["tweet_count"]}
                for r in reversed(rows)]

    # ---------- outcome harvester (metrics of OUR OWN posts over time) ----------
    def posted_recent(self, within_days: int = 8) -> list[dict]:
        """Our published posts young enough to still have open milestone windows.
        Rows without our_tweet_id (dry-run history) are unharvestable — skipped."""
        cutoff = (utcnow() - timedelta(days=within_days)).isoformat()
        rows = self.conn.execute(
            "SELECT our_tweet_id, posted_at FROM posted_log "
            "WHERE posted_at >= ? AND our_tweet_id != '' AND our_tweet_id IS NOT NULL "
            "ORDER BY posted_at DESC",
            (cutoff,),
        ).fetchall()
        return [{"our_tweet_id": r["our_tweet_id"], "posted_at": r["posted_at"]}
                for r in rows]

    def outcome_milestones(self, our_tweet_id: str) -> set[str]:
        rows = self.conn.execute(
            "SELECT milestone FROM post_outcomes WHERE our_tweet_id=?", (our_tweet_id,)
        ).fetchall()
        return {r["milestone"] for r in rows}

    def log_outcome(self, our_tweet_id: str, milestone: str, metrics: Metrics) -> None:
        """Idempotent: one row per (post, milestone), ever — a re-run can't
        overwrite a snapshot with later numbers under an early label."""
        self.conn.execute(
            """INSERT OR IGNORE INTO post_outcomes
                   (our_tweet_id, milestone, likes, reposts, replies, quotes, views,
                    captured_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (our_tweet_id, milestone, metrics.likes, metrics.reposts, metrics.replies,
             metrics.quotes, metrics.views, metrics.captured_at.isoformat()),
        )
        self.conn.commit()

    # ---------- post features (tagged at publish; joined to outcomes later) ----------
    def log_features(self, f: dict) -> None:
        self.conn.execute(
            """INSERT INTO post_features (our_tweet_id, source_tweet_id, author_handle,
                   route, kind, format, parts_n, chars, has_question, hook,
                   window_hour, teaching, topic_fit, quote_score, posted_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(our_tweet_id) DO NOTHING""",
            (f.get("our_tweet_id", ""), f.get("source_tweet_id", ""),
             f.get("author_handle", ""), f.get("route", "pipeline"),
             f.get("kind", "qt"), f.get("format", "single"),
             int(f.get("parts_n", 0)), int(f.get("chars", 0)),
             int(bool(f.get("has_question", False))), (f.get("hook", "") or "")[:160],
             f.get("window_hour"), f.get("teaching"), f.get("topic_fit"),
             f.get("quote_score"), f.get("posted_at", utcnow().isoformat())),
        )
        self.conn.commit()

    # ---------- agent usage (subscription window governor) ----------
    def log_agent_usage(self, session: str, model: str, turns: int,
                        input_tokens: int = 0, output_tokens: int = 0,
                        cost_usd: float = 0.0, detail: str = "") -> None:
        now = utcnow()
        self.conn.execute(
            """INSERT INTO agent_usage (ts, ts_pt, session, model, turns,
                   input_tokens, output_tokens, cost_usd, detail)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (now.isoformat(), to_local(now, self.tz_name).isoformat(), session,
             model, turns, input_tokens, output_tokens, cost_usd,
             (detail or "")[:300]),
        )
        self.conn.commit()

    def agent_turns_today(self) -> int:
        """Agent turns spent since local midnight — the USAGE GOVERNOR reads
        this before every session so the bot can't crowd the owner's plan."""
        start_local = to_local(utcnow(), self.tz_name).replace(
            hour=0, minute=0, second=0, microsecond=0)
        start = start_local.astimezone(timezone.utc).isoformat()
        r = self.conn.execute(
            "SELECT COALESCE(SUM(turns), 0) AS c FROM agent_usage WHERE ts >= ?",
            (start,),
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
