"""Regressions from the 2026-09-07 CI failures (strategist + list-sync)."""
from xbot.briefing import _row_dict
from xbot.ingest.api_source import ApiSourceAdapter


class _LibsqlRow:
    """Mimics libsql_client.Row: asdict() but NO keys()."""
    def __init__(self, d): self._d = d
    def asdict(self): return dict(self._d)
    def __getitem__(self, k): return self._d[k]


def test_row_dict_handles_libsql_rows():
    assert _row_dict(_LibsqlRow({"a": 1, "b": "x"})) == {"a": 1, "b": "x"}


def test_row_dict_handles_sqlite3_rows():
    import sqlite3
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    r = c.execute("select 1 as a, 'x' as b").fetchone()
    assert _row_dict(r) == {"a": 1, "b": "x"}


class _RecordingSession:
    def __init__(self): self.calls = []
    def get(self, url, params=None, timeout=None):
        self.calls.append(params["usernames"])
        class R:
            def raise_for_status(self): pass
            def json(self_inner): return {"data": [
                {"username": u, "id": str(i)} for i, u in enumerate(params["usernames"].split(","))]}
        return R()


def test_resolve_user_ids_drops_domains_and_invalid_handles():
    src = ApiSourceAdapter.__new__(ApiSourceAdapter)
    sess = _RecordingSession()
    src._session = lambda: sess
    out = src.resolve_user_ids(["@good_one", "faceless.so", "captions.ai", "way_too_long_handle_x", "", "Ok2"])
    assert sess.calls == ["good_one,Ok2"]
    assert set(out) == {"good_one", "ok2"}


def test_resolve_user_ids_skips_call_when_nothing_valid():
    src = ApiSourceAdapter.__new__(ApiSourceAdapter)
    sess = _RecordingSession()
    src._session = lambda: sess
    assert src.resolve_user_ids(["faceless.so"]) == {}
    assert sess.calls == []
