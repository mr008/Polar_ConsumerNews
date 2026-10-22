"""Turso wrapper must survive a dropped keep-alive connection (lesson 13).

Turso's HTTP edge closes idle connections; after a slow LLM/X step the next
query on the stale pooled socket raises aiohttp ServerDisconnectedError. The
wrapper must reconnect and retry instead of killing the run.
"""
import pytest

from xbot.storage import turso_repo


class _Disconnected(Exception):
    """Stands in for aiohttp.client_exceptions.ServerDisconnectedError."""


_Disconnected.__name__ = "ServerDisconnectedError"
_Disconnected.__module__ = "aiohttp.client_exceptions"


class _RS:
    rows = [{"x": 1}]
    last_insert_rowid = None


class _FakeClient:
    def __init__(self, failures_before_ok: int, log: list):
        self.failures = failures_before_ok
        self.log = log
        self.closed = False

    def execute(self, sql, args=None):
        self.log.append(("execute", sql, args))
        if self.failures > 0:
            self.failures -= 1
            raise _Disconnected("Server disconnected")
        return _RS()

    def close(self):
        self.closed = True


def _conn(failures_per_client, log, **kw):
    clients = []

    def factory():
        c = _FakeClient(failures_per_client[len(clients)], log)
        clients.append(c)
        return c

    conn = turso_repo._TursoConnection.__new__(turso_repo._TursoConnection)
    conn._init_with_factory(factory, **kw)
    return conn, clients


def test_execute_reconnects_and_retries_after_disconnect():
    log = []
    conn, clients = _conn([1, 0], log, backoff=0)
    row = conn.execute("SELECT 1 FROM posted_log WHERE source_tweet_id=?", ("a",)).fetchone()
    assert row == {"x": 1}
    assert len(clients) == 2, "a fresh client must be built after a disconnect"
    assert clients[0].closed, "the stale client must be closed before retrying"
    assert [e[0] for e in log] == ["execute", "execute"]
    conn.close()


def test_execute_gives_up_after_max_attempts():
    log = []
    conn, clients = _conn([1, 1, 1, 1], log, backoff=0, attempts=3)
    with pytest.raises(_Disconnected):
        conn.execute("SELECT 1")
    assert len(log) == 3
    conn.close()


def test_non_transient_errors_are_not_retried():
    log = []

    class _Boom(_FakeClient):
        def execute(self, sql, args=None):
            self.log.append(("execute", sql, args))
            raise ValueError("bad sql")

    conn = turso_repo._TursoConnection.__new__(turso_repo._TursoConnection)
    conn._init_with_factory(lambda: _Boom(0, log), backoff=0)
    with pytest.raises(ValueError):
        conn.execute("SELECT 1")
    assert len(log) == 1
    conn.close()
