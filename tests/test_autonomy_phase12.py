"""Phases 1-2 of the autonomy overhaul: detectors, the shared voice spec
golden equality, Curator shadow parsing/storage, the bounds validator, and the
briefing pack."""
import importlib.util
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from xbot.config import NS
from xbot.detectors import run_detectors
from xbot.models import Metrics, Post, Score, utcnow
from xbot.orchestrator import Orchestrator
from xbot.storage.sqlite_repo import SqliteRepository


def _repo():
    repo = SqliteRepository(":memory:")
    repo.init_schema()
    return repo


def _cfg(extra=None):
    data = {"mode": {"autonomous": True, "curator": "shadow"},
            "posting": {"per_day": 3},
            "scoping": {"monthly_read_budget": 4000}}
    if extra:
        data.update(extra)
    return NS(data)


def _orch(repo, cfg=None):
    orch = object.__new__(Orchestrator)
    orch.cfg = cfg or _cfg()
    orch.repo = repo
    orch.source = SimpleNamespace()
    return orch


def _mark_posted(repo, our_id="our_1", hours_ago=1.0):
    posted = (utcnow() - timedelta(hours=hours_ago)).isoformat()
    repo.conn.execute(
        "INSERT INTO posted_log (source_tweet_id, our_tweet_id, author_handle, "
        "source_text, commentary, posted_at, posted_at_pt) VALUES (?,?,?,?,?,?,?)",
        (f"s_{our_id}", our_id, "a", "t", "c", posted, posted))
    repo.conn.commit()


# ---------------- detectors ----------------

def test_dead_man_trips_only_when_autonomous_and_silent():
    repo = _repo()
    trips = run_detectors(repo, _cfg())
    assert any(t["detector"] == "dead_man" for t in trips)
    _mark_posted(repo, hours_ago=2)
    trips = run_detectors(repo, _cfg())
    assert not any(t["detector"] == "dead_man" for t in trips)
    # manual-review mode never dead-mans
    repo2 = _repo()
    trips = run_detectors(repo2, _cfg({"mode": {"autonomous": False}}))
    assert not any(t["detector"] == "dead_man" for t in trips)


def test_budget_near_limit_severities():
    repo = _repo()
    repo.log_run("collect", read=3700)          # 92.5% of 4000
    trips = run_detectors(repo, _cfg())
    trip = next(t for t in trips if t["detector"] == "budget_near_limit")
    assert trip["severity"] == "medium"
    repo.log_run("collect", read=400)           # over budget
    trips = run_detectors(repo, _cfg())
    trip = next(t for t in trips if t["detector"] == "budget_near_limit")
    assert trip["severity"] == "high"


def test_supply_drought_and_agent_auth_and_harvest_stall():
    repo = _repo()
    repo.log_run("collect", read=0)
    repo.log_run("agent", detail="smoke: auth_error 401")
    _mark_posted(repo, hours_ago=6)             # no outcome snapshots
    names = {t["detector"] for t in run_detectors(repo, _cfg())}
    assert {"supply_drought", "agent_auth", "harvest_stall"} <= names
    # snapshots captured + reads flowing + healthy agents -> quiet again
    repo.log_run("collect", read=25)
    repo.log_outcome("our_1", "1h", Metrics(likes=1))
    repo2_names = {t["detector"] for t in run_detectors(repo, _cfg())}
    assert "supply_drought" not in repo2_names
    assert "harvest_stall" not in repo2_names


def test_detector_crash_is_reported_not_raised(monkeypatch):
    import xbot.detectors as d
    def boom(repo, cfg):
        raise RuntimeError("sensor down")
    monkeypatch.setattr(d, "DETECTORS", [boom])
    trips = run_detectors(_repo(), _cfg())
    assert trips and "crashed" in trips[0]["summary"]


# ---------------- shared voice spec (golden equality) ----------------

def test_voice_spec_file_matches_embedded_fallback(monkeypatch):
    """agent/voice.md must produce a byte-identical system prompt to the
    embedded fallback — the refactor can never drift the live public voice."""
    from xbot.commentary import generate as g
    cfg = NS({"voice": {"style": "growth_first"},
              "llm": {"max_commentary_chars": 245}})
    from_file = g.build_system_prompt(cfg)
    monkeypatch.setattr(g, "_voice_spec_text", lambda: None)
    embedded = g.build_system_prompt(cfg)
    assert from_file == embedded


# ---------------- Curator shadow ----------------

def _seed_candidate(repo, tid, qs=0.5):
    repo.upsert_post(Post(tweet_id=tid, author_handle=f"u{tid}",
                          author_name=f"u{tid}", text=f"tactic {tid} alpha beta",
                          created_at=utcnow(), author_follower_count=5000,
                          metrics=Metrics(likes=5)))
    repo.save_score(Score(tweet_id=tid, quote_score=qs, judged=True))


def test_curate_shadow_stores_verdicts(monkeypatch):
    repo = _repo()
    _seed_candidate(repo, "1", 0.9)
    _seed_candidate(repo, "2", 0.4)
    verdicts = [{"tweet_id": "1", "verdict": "pick", "teaching": 0.8,
                 "reason": "concrete steps", "draft": "steal this move"},
                {"tweet_id": "2", "verdict": "skip", "teaching": 0.2,
                 "reason": "flex, no method"},
                {"tweet_id": "999", "verdict": "pick"}]      # not in batch: dropped
    fenced = "```json\n" + json.dumps(verdicts) + "\n```"
    import xbot.agents as agents_mod
    monkeypatch.setattr(agents_mod, "run_session",
                        lambda *a, **kw: {"status": "ok", "result": fenced,
                                          "turns": 3})
    res = _orch(repo).curate_shadow()
    assert res["status"] == "ok"
    assert res["judged"] == 2 and res["picks"] == 1
    stored = repo.curator_shadow_recent(1)
    assert {s["tweet_id"] for s in stored} == {"1", "2"}
    pick = next(s for s in stored if s["tweet_id"] == "1")
    assert pick["draft"] == "steal this move"


def test_curate_shadow_off_and_failquiet(monkeypatch):
    repo = _repo()
    assert _orch(repo, _cfg({"mode": {"curator": "off"}})).curate_shadow() == \
        {"status": "off"}
    _seed_candidate(repo, "1")
    import xbot.agents as agents_mod
    monkeypatch.setattr(agents_mod, "run_session",
                        lambda *a, **kw: {"status": "cli_missing"})
    assert _orch(repo).curate_shadow()["status"] == "cli_missing"
    monkeypatch.setattr(agents_mod, "run_session",
                        lambda *a, **kw: {"status": "ok", "result": "not json",
                                          "turns": 1})
    assert _orch(repo).curate_shadow()["status"] == "unparseable"
    assert repo.curator_shadow_recent(1) == []


# ---------------- bounds validator ----------------

def _bounds_mod():
    spec = importlib.util.spec_from_file_location(
        "validate_bounds", Path("scripts/validate_bounds.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


BOUNDS = {
    "posting.per_day": {"immutable": True},
    "safety": {"immutable": True},
    "scoping.monthly_read_budget": {"raise_requires_owner": True},
    "ranking.teaching_weight": {"min": 0.5, "max": 0.8, "max_delta_per_week": 0.1},
    "posting.windows": {"constraint": "each window inside 07:00-21:00 local"},
}


def test_bounds_immutable_and_prefix_match():
    vb = _bounds_mod()
    base = {"posting": {"per_day": 3}, "safety": {"toxicity_max": 0.3}}
    new = {"posting": {"per_day": 5}, "safety": {"toxicity_max": 0.9}}
    violations, _ = vb.validate(base, new, BOUNDS, strict=False)
    assert len(violations) == 2                  # per_day + safety.* (prefix rule)


def test_bounds_range_delta_and_owner_raise():
    vb = _bounds_mod()
    base = {"ranking": {"teaching_weight": 0.65},
            "scoping": {"monthly_read_budget": 4000}}
    # in range, small delta -> clean in strict
    v, _ = vb.validate(base, {"ranking": {"teaching_weight": 0.7},
                              "scoping": {"monthly_read_budget": 4000}},
                       BOUNDS, strict=True)
    assert v == []
    # out of range fails everyone; big delta + budget raise fail strict
    v, _ = vb.validate(base, {"ranking": {"teaching_weight": 0.95},
                              "scoping": {"monthly_read_budget": 4000}},
                       BOUNDS, strict=False)
    assert any("above max" in x for x in v)
    v, _ = vb.validate(base, {"ranking": {"teaching_weight": 0.5},
                              "scoping": {"monthly_read_budget": 6000}},
                       BOUNDS, strict=True)
    assert any("delta" in x for x in v)
    assert any("raise requires owner" in x for x in v)
    # owner may raise the budget in lenient mode
    v, _ = vb.validate(base, {"ranking": {"teaching_weight": 0.65},
                              "scoping": {"monthly_read_budget": 6000}},
                       BOUNDS, strict=False)
    assert v == []


def test_bounds_unlisted_key_strict_vs_lenient_and_windows():
    vb = _bounds_mod()
    base = {"llm": {"temperature": 0.7}, "posting": {"windows": ["08:00-09:30"]}}
    new = {"llm": {"temperature": 0.2}, "posting": {"windows": ["05:00-06:00"]}}
    v, notes = vb.validate(base, new, BOUNDS, strict=False)
    assert any("outside 07:00-21:00" in x for x in v)
    assert any("no constitution entry" in n for n in notes)
    v, _ = vb.validate(base, new, BOUNDS, strict=True)
    assert any("no constitution entry" in x for x in v)


# ---------------- briefing pack ----------------

def test_briefing_joins_features_and_outcomes():
    from xbot.briefing import build_briefing
    repo = _repo()
    repo.tz_name = "America/Los_Angeles"
    _mark_posted(repo, "our_9", hours_ago=30)
    repo.log_features({"our_tweet_id": "our_9", "route": "pipeline",
                       "kind": "qt", "format": "single", "hook": "steal this",
                       "teaching": 0.8, "window_hour": 8})
    repo.log_outcome("our_9", "24h", Metrics(likes=4, reposts=1, views=900))
    repo.snapshot_account("2026-08-01", followers=10, following=5, tweet_count=40)
    text = build_briefing(repo, _cfg())
    assert "route=pipeline" in text
    assert "24h: 900v/5e" in text
    assert "Follower curve" in text and "2026-08-01: 10" in text
