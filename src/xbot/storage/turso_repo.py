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
import time

# aiohttp exception names that mean "the socket died", not "the SQL was bad".
_TRANSIENT_NAMES = {
    "ServerDisconnectedError",
    "ClientConnectionError",
    "ClientConnectorError",
    "ClientOSError",
    "ClientConnectionResetError",
    "ClientPayloadError",
    "ServerTimeoutError",
    "TimeoutError",
    "ConnectionResetError",
}


def _is_transient(exc: BaseException) -> bool:
    """True for connection-level failures worth a reconnect + retry."""
    for klass in type(exc).__mro__:
        if klass.__name__ in _TRANSIENT_NAMES:
            return True
    return isinstance(exc, (ConnectionError, TimeoutError))

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

    def __init__(self, url: str, auth_token: str, attempts: int = 3, backoff: float = 0.5):
        import libsql_client  # lazy import

        https = url.replace("libsql://", "https://")

        def factory():
            return libsql_client.create_client_sync(url=https, auth_token=auth_token)

        self._init_with_factory(factory, attempts=attempts, backoff=backoff)

    def _init_with_factory(self, factory, attempts: int = 3, backoff: float = 0.5):
        self._factory = factory
        self._attempts = max(1, int(attempts))
        self._backoff = backoff
        self._c = factory()
        self.row_factory = None  # libsql rows support ["col"] natively
        _OPEN_CONNECTIONS.append(self)

    def _reconnect(self) -> None:
        try:
            self._c.close()
        except Exception:
            pass
        self._c = self._factory()

    def _run(self, sql: str, args=None):
        """Execute one statement; reconnect + retry on a dropped connection.

        Turso's HTTP edge closes idle keep-alive sockets. After a slow step (LLM
        call, X read) the next query on the stale pooled socket raises
        aiohttp ServerDisconnectedError (3 red runs 2026-10-17..22). The error
        is raised before the server processed the request, so a retry is safe.
        """
        for attempt in range(1, self._attempts + 1):
            try:
                return self._c.execute(sql, args) if args else self._c.execute(sql)
            except Exception as e:  # noqa: BLE001 - classified below
                if not _is_transient(e) or attempt == self._attempts:
                    raise
                print(f"[turso] {type(e).__name__}: {e} -- reconnecting (attempt {attempt}/{self._attempts})")
                if self._backoff:
                    time.sleep(self._backoff * attempt)
                self._reconnect()

    def execute(self, sql: str, params=()):
        return _TursoCursor(self._run(sql, list(params) if params else None))

    def executescript(self, script: str):
        for stmt in (s.strip() for s in script.split(";")):
            if stmt:
                self._run(stmt)

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
