"""Headless Claude Code session runner + USAGE GOVERNOR.

Every agent session (Curator/Strategist/Mechanic, and the Phase-0 smoke test)
goes through run_session(), which:

  1. checks the governor — the bot may spend at most `daily_turn_ceiling`
     agent turns per local day (agent/constitution.yaml), so it can never
     crowd the owner's own subscription window. Over the ceiling, the session
     is SKIPPED (fail quiet), never queued and never billed elsewhere;
  2. invokes `claude -p` with --output-format json and a hard --max-turns;
  3. logs actual usage (turns/tokens/cost) to agent_usage — the governor's
     ledger AND the Strategist's own telemetry.

Auth comes from the environment: CLAUDE_CODE_OAUTH_TOKEN (subscription, $0)
in CI. There is deliberately NO pay-per-token fallback here — when the
subscription surface is structurally broken, the fallback is the OLD PIPELINE
route on the API key (owner decision, see AUTONOMY.md), never an agentic
session on per-token billing.

Stdlib-only (subprocess/json) + pyyaml for the constitution — safe to import
anywhere in the dry-run path.
"""
from __future__ import annotations

import json
import subprocess

CONSTITUTION_PATH = "agent/constitution.yaml"
DEFAULT_TURN_CEILING = 60          # ~10% of a Max-5x day; constitution overrides
SESSION_TIMEOUT_S = 900            # hard wall-clock cap per session


def governor_ceiling(path: str = CONSTITUTION_PATH) -> int:
    """Daily agent-turn ceiling from the constitution. Falls back to the
    conservative default when the file is missing or unreadable — the governor
    must never fail OPEN."""
    try:
        import yaml
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return int(data.get("usage_governor", {}).get("daily_turn_ceiling",
                                                      DEFAULT_TURN_CEILING))
    except Exception:
        return DEFAULT_TURN_CEILING


def governor_allows(repo, ceiling: int | None = None) -> tuple[bool, int, int]:
    """(allowed, used_today, ceiling). Checked BEFORE the model is invoked."""
    if ceiling is None:
        ceiling = governor_ceiling()
    used = repo.agent_turns_today()
    return used < ceiling, used, ceiling


def parse_claude_json(text: str) -> dict:
    """Extract the fields we track from `claude -p --output-format json`.
    Unknown/absent fields degrade to zeros rather than raising — the session
    already ran; usage accounting must not turn a success into a failure."""
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return {"ok": False, "error": "unparseable_output", "raw": (text or "")[:200]}
    usage = data.get("usage") or {}
    return {
        "ok": not data.get("is_error", False),
        # Generous cap: Curator verdict JSON and Strategist memos ride through
        # here; truncating them would corrupt valid output.
        "result": str(data.get("result", ""))[:40000],
        "turns": int(data.get("num_turns", 0) or 0),
        "input_tokens": int(usage.get("input_tokens", 0) or 0),
        "output_tokens": int(usage.get("output_tokens", 0) or 0),
        "cost_usd": float(data.get("total_cost_usd", 0.0) or 0.0),
        "session_id": data.get("session_id", ""),
    }


def run_session(name: str, prompt: str, repo, *, allowed_tools: str = "Read",
                max_turns: int = 8, model: str | None = None,
                cli: str = "claude", ceiling: int | None = None) -> dict:
    """Run one governed headless session. Returns a status dict; never raises
    on session failure (callers decide whether a red result fails the job)."""
    allowed, used, cap = governor_allows(repo, ceiling)
    if not allowed:
        detail = f"governor: {used}/{cap} turns today — session skipped"
        print(f"  [agent:{name}] {detail}")
        repo.log_run("agent", detail=f"{name} {detail}"[:300])
        return {"status": "skipped_governor", "used": used, "ceiling": cap}

    cmd = [cli, "-p", prompt, "--output-format", "json",
           "--max-turns", str(max_turns), "--allowedTools", allowed_tools]
    if model:
        cmd += ["--model", model]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", timeout=SESSION_TIMEOUT_S)
    except FileNotFoundError:
        detail = f"claude CLI not found ({cli})"
        repo.log_run("agent", detail=f"{name}: {detail}")
        return {"status": "cli_missing", "detail": detail}
    except subprocess.TimeoutExpired:
        # Bill the ceiling conservatively: we can't know what a hung session
        # spent, so charge max_turns rather than letting it look free.
        repo.log_agent_usage(name, model or "default", max_turns,
                             detail="timeout")
        repo.log_run("agent", detail=f"{name}: timeout after {SESSION_TIMEOUT_S}s")
        return {"status": "timeout"}

    parsed = parse_claude_json(proc.stdout)
    auth_error = proc.returncode != 0 and any(
        marker in (proc.stderr or "") + (proc.stdout or "")
        for marker in ("401", "403", "authentication", "OAuth", "log in"))
    repo.log_agent_usage(
        name, model or "default", parsed.get("turns", 0),
        parsed.get("input_tokens", 0), parsed.get("output_tokens", 0),
        parsed.get("cost_usd", 0.0),
        detail=(parsed.get("result") or parsed.get("error") or "")[:200])
    repo.log_run("agent", detail=(
        f"{name}: turns={parsed.get('turns', 0)} "
        f"cost=${parsed.get('cost_usd', 0.0):.4f} "
        f"rc={proc.returncode}")[:300])

    if proc.returncode != 0 or not parsed.get("ok", False):
        return {"status": "auth_error" if auth_error else "session_error",
                "rc": proc.returncode,
                "detail": (proc.stderr or parsed.get("error") or "")[:300],
                **{k: parsed.get(k) for k in ("turns", "cost_usd")}}
    return {"status": "ok", "result": parsed["result"], "turns": parsed["turns"],
            "cost_usd": parsed["cost_usd"], "used_today": used + parsed["turns"],
            "ceiling": cap}
