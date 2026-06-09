"""Turso (libSQL) repository.

Reuses ALL of SqliteRepository's SQL/logic by wrapping the libsql HTTP client in a
thin object that quacks like a sqlite3 connection (execute/executescript/commit +
a cursor with fetchone/fetchall). libsql rows already support row["col"] access,
so the SqliteRepository method bodies work unchanged.

IMPORTANT: the libsql sync client runs its event loop in a NON-daemon thread, so
the process won't exit until the client is closed. atexit is too late (the
interpreter blocks joining that thread before atexit runs), so callers MUST call
close_all() before exiting — the CLI does this in a finally.
"""
from __future__ import annotations

import os

from .sqlite_repo import SqliteRepository

_OPEN_CONNECTIONS: list = []


def close_all() -> None:
    """Close every open Turso connection so the process can exit. No-op if none."""
    for conn in list(_OPEN_CONNECTIONS):
        conn.close()


class _TursoCursor:
    def __init__(self, result_set):
        self._rows = list(result_set.rows)
        self.lastrowid = getattr(result_set, "last_insert_rowid", None)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class _TursoConnection:
    """Minimal sqlite3.Connection look-alike backed by libsql over HTTP."""

    def __init__(self, url: str, auth_token: str):
        import libsql_client  # lazy import

        https = url.replace("libsql://", "https://")
        self._c = libsql_client.create_client_sync(url=https, auth_token=auth_token)
        self.row_factory = None  # libsql rows support ["col"] natively
        _OPEN_CONNECTIONS.append(self)

    def execute(self, sql: str, params=()):
        rs = self._c.execute(sql, list(params)) if params else self._c.execute(sql)
        return _TursoCursor(rs)

    def executescript(self, script: str):
        for stmt in (s.strip() for s in script.split(";")):
            if stmt:
                self._c.execute(stmt)

    def commit(self):
        pass  # libsql commits each statement individually

    def close(self):
        try:
            self._c.close()
        except Exception:
            pass
        finally:
            if self in _OPEN_CONNECTIONS:
                _OPEN_CONNECTIONS.remove(self)


class TursoRepository(SqliteRepository):
    def __init__(self, url: str | None = None, auth_token: str | None = None):
        url = url or os.environ["TURSO_DATABASE_URL"]
        auth_token = auth_token or os.environ["TURSO_AUTH_TOKEN"]
        self.conn = _TursoConnection(url, auth_token)
