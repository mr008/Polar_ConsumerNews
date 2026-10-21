from xbot.config import NS
from xbot.orchestrator import Orchestrator


def _orch(exclude):
    o = object.__new__(Orchestrator)
    o.cfg = NS({"listsync": {"min_judged": 3, "promote_avg": 0.35, "exclude": exclude}})
    return o


def test_excluded_handle_never_qualifies():
    row = {"handle": "RevenueCat", "judged": 10, "avg_qw": 0.9}
    assert not _orch(["@revenuecat"])._qualifies(row)   # case + @ insensitive
    assert _orch([])._qualifies(row)
