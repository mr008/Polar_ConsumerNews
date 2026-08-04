"""Deterministic health detectors — the Mechanic's senses (AUTONOMY.md Phase 1).

Pure DB reads, no LLM, no network. Each detector returns None (healthy) or a
dict {detector, severity, summary, context}. The CLI (`xbot detect`) prints the
JSON report and ALWAYS exits 0 — deciding what to do about a trip (open an
issue, run a Mechanic session) is the workflow layer's job, and a red detector
must never break the pipeline run it rides on.

Every June incident maps to a detector here:
  dead_man            — the silent stop (budget freeze, workflow gap)
  budget_near_limit   — the breaker freeze that stopped posting for days
  publish_failing     — the all_failed windows
  supply_drought      — the List going quiet
  agent_auth          — OAuth token death / billing rework (the fallback trigger)
  harvest_stall       — the learning substrate silently starving
"""
from __future__ import annotations

from datetime import timedelta

from .models import parse_dt, utcnow


def _runs(repo, kind: str, within_hours: float) -> list[dict]:
    return [r for r in repo.recent_runs(within_hours) if r["kind"] == kind]


def detect_dead_man(repo, cfg) -> dict | None:
    """No successful post in >24h while the account is in autonomous mode.
    The one alarm that must never be silenced — a healthy bot posts daily."""
    if not cfg.get("mode.autonomous", False):
        return None
    rows = repo.activity_posted(within_hours=24)
    if rows:
        return None
    return {"detector": "dead_man", "severity": "high",
            "summary": "no successful post in 24h",
            "context": {"pending_drafts": len(repo.pending_drafts())}}


def detect_budget_near_limit(repo, cfg) -> dict | None:
    """Monthly read budget ≥90% spent — the June freeze gave no warning."""
    budget = int(cfg.get("scoping.monthly_read_budget", 0) or 0)
    if not budget:
        return None
    used = repo.reads_this_month()
    if used < 0.9 * budget:
        return None
    return {"detector": "budget_near_limit",
            "severity": "high" if used >= budget else "medium",
            "summary": f"monthly reads {used}/{budget}",
            "context": {"used": used, "budget": budget}}


def detect_publish_failing(repo, cfg) -> dict | None:
    """Publish runs erroring: any all_failed, or repeated failure details."""
    runs = _runs(repo, "publish", 26)
    bad = [r for r in runs if "all_failed" in (r["detail"] or "")]
    if not bad:
        return None
    return {"detector": "publish_failing", "severity": "high",
            "summary": f"{len(bad)} publish run(s) failed all drafts in 26h",
            "context": {"details": [b["detail"][:120] for b in bad[-3:]]}}


def detect_supply_drought(repo, cfg) -> dict | None:
    """Collect runs succeeding but returning nothing for a full day — the
    List has gone quiet (or since_id state corrupted)."""
    runs = _runs(repo, "collect", 26)
    if not runs:  # collect never ran — dead_man / workflow gap territory
        return {"detector": "supply_drought", "severity": "medium",
                "summary": "no collect run in 26h", "context": {}}
    if any(r["read"] > 0 for r in runs):
        return None
    return {"detector": "supply_drought", "severity": "medium",
            "summary": f"{len(runs)} collect run(s) in 26h read 0 new posts",
            "context": {"runs": len(runs)}}


def detect_agent_auth(repo, cfg) -> dict | None:
    """An agent session hit an auth failure (401/403) — the structural-break
    signal that the owner may need to flip the pipeline fallback flag. A
    window throttle is NOT this (fail-quiet by design)."""
    runs = _runs(repo, "agent", 48)
    bad = [r for r in runs if "auth" in (r["detail"] or "").lower()]
    if not bad:
        return None
    return {"detector": "agent_auth", "severity": "high",
            "summary": "agent session auth failure — subscription path may be broken",
            "context": {"details": [b["detail"][:120] for b in bad[-3:]]}}


def detect_harvest_stall(repo, cfg) -> dict | None:
    """Posts published recently but no outcome snapshot captured in 48h —
    the learning substrate is starving (only fires when there was material)."""
    posted = repo.posted_recent(within_days=2)
    if not posted:
        return None
    old_enough = [r for r in posted
                  if (utcnow() - parse_dt(r["posted_at"])) > timedelta(hours=2)]
    if not old_enough:
        return None
    captured = any(repo.outcome_milestones(r["our_tweet_id"]) for r in old_enough)
    if captured:
        return None
    return {"detector": "harvest_stall", "severity": "low",
            "summary": f"{len(old_enough)} recent post(s), zero outcome snapshots",
            "context": {"posts": len(old_enough)}}


DETECTORS = [detect_dead_man, detect_budget_near_limit, detect_publish_failing,
             detect_supply_drought, detect_agent_auth, detect_harvest_stall]


def run_detectors(repo, cfg) -> list[dict]:
    """All trips, worst first. A detector that itself crashes is reported as a
    trip (a blind sensor is a fault, not a pass) but never raises."""
    trips: list[dict] = []
    for fn in DETECTORS:
        try:
            t = fn(repo, cfg)
        except Exception as e:
            t = {"detector": fn.__name__, "severity": "medium",
                 "summary": f"detector crashed: {type(e).__name__}", "context": {}}
        if t:
            trips.append(t)
    order = {"high": 0, "medium": 1, "low": 2}
    trips.sort(key=lambda t: order.get(t["severity"], 3))
    return trips
