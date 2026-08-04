"""Constitution bounds validator (AUTONOMY.md Phase 2) — a REQUIRED CI check.

    python scripts/validate_bounds.py <base_config.yaml> <new_config.yaml> [--strict]

Diffs the two configs and validates every changed dotted key against
agent/constitution.yaml `config_bounds`:

  immutable             any change is a violation (everyone — owner included;
                        the owner widens the constitution first, in the same
                        protected-path PR)
  min/max               out-of-range value is a violation (everyone)
  max_delta_per_week    per-PR delta beyond the cap is a violation in --strict
                        (agent PRs land at most weekly, so per-PR ≈ per-week)
  raise_requires_owner  an increase is a violation in --strict only
  constraint (prose)    machine-checked where concrete (posting.windows);
                        otherwise reported as a note
  unlisted key changed  violation in --strict (agents may only touch listed
                        keys); note otherwise (the owner edits config freely)

--strict is passed by CI when the PR author is the agent identity. Exit code:
0 = clean, 1 = violations.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

CONSTITUTION = Path("agent/constitution.yaml")
WINDOW_EARLIEST, WINDOW_LATEST = 7 * 60, 21 * 60  # 07:00–21:00 local, minutes


def flatten(d: dict, prefix: str = "") -> dict:
    out: dict = {}
    for k, v in (d or {}).items():
        key = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, dict):
            out.update(flatten(v, key))
        else:
            out[key] = v
    return out


def bound_for(key: str, bounds: dict):
    """Most specific bound entry covering `key` (exact, else nearest prefix)."""
    if key in bounds:
        return key, bounds[key]
    parts = key.split(".")
    for i in range(len(parts) - 1, 0, -1):
        prefix = ".".join(parts[:i])
        if prefix in bounds:
            return prefix, bounds[prefix]
    return None, None


def check_windows(value) -> str | None:
    try:
        for w in value:
            start, end = str(w).split("-")
            for hhmm in (start, end):
                h, m = hhmm.split(":")
                minutes = int(h) * 60 + int(m)
                if not (WINDOW_EARLIEST <= minutes <= WINDOW_LATEST):
                    return f"window {w} outside 07:00-21:00"
        return None
    except Exception:
        return f"unparseable windows value: {value!r}"


def validate(base: dict, new: dict, bounds: dict, strict: bool
             ) -> tuple[list[str], list[str]]:
    violations, notes = [], []
    fb, fn = flatten(base), flatten(new)
    changed = {k for k in set(fb) | set(fn) if fb.get(k) != fn.get(k)}
    for key in sorted(changed):
        old, val = fb.get(key), fn.get(key)
        rule_key, rule = bound_for(key, bounds)
        if rule is None:
            (violations if strict else notes).append(
                f"{key}: changed ({old!r} -> {val!r}) with no constitution entry")
            continue
        rule = rule or {}
        if rule.get("immutable"):
            violations.append(f"{key}: immutable (rule {rule_key}), "
                              f"changed {old!r} -> {val!r}")
            continue
        if rule.get("raise_requires_owner") and strict:
            try:
                if val is not None and old is not None and float(val) > float(old):
                    violations.append(f"{key}: raise requires owner "
                                      f"({old!r} -> {val!r})")
                    continue
            except (TypeError, ValueError):
                violations.append(f"{key}: non-numeric change on "
                                  f"raise_requires_owner key")
                continue
        if "min" in rule or "max" in rule:
            try:
                v = float(val)
            except (TypeError, ValueError):
                violations.append(f"{key}: non-numeric value {val!r} for a "
                                  f"ranged key")
                continue
            if "min" in rule and v < float(rule["min"]):
                violations.append(f"{key}: {v} below min {rule['min']}")
                continue
            if "max" in rule and v > float(rule["max"]):
                violations.append(f"{key}: {v} above max {rule['max']}")
                continue
            delta_cap = rule.get("max_delta_per_week")
            if strict and delta_cap is not None and old is not None:
                try:
                    if abs(v - float(old)) > float(delta_cap) + 1e-9:
                        violations.append(
                            f"{key}: delta {abs(v - float(old)):.3f} exceeds "
                            f"max_delta_per_week {delta_cap}")
                        continue
                except (TypeError, ValueError):
                    pass
        if rule_key == "posting.windows":
            err = check_windows(val)
            if err:
                violations.append(f"{key}: {err}")
                continue
        if "constraint" in rule:
            notes.append(f"{key}: prose constraint applies — {rule['constraint']}")
    return violations, notes


def main(argv: list[str]) -> int:
    strict = "--strict" in argv
    paths = [a for a in argv if not a.startswith("--")]
    if len(paths) != 2:
        print(__doc__)
        return 1
    base = yaml.safe_load(Path(paths[0]).read_text(encoding="utf-8")) or {}
    new = yaml.safe_load(Path(paths[1]).read_text(encoding="utf-8")) or {}
    constitution = yaml.safe_load(CONSTITUTION.read_text(encoding="utf-8")) or {}
    bounds = constitution.get("config_bounds", {}) or {}
    flat_bounds = {}
    for k, v in bounds.items():
        flat_bounds[str(k)] = v or {}
    violations, notes = validate(base, new, flat_bounds, strict)
    mode = "STRICT (agent PR)" if strict else "lenient (owner PR)"
    print(f"validate_bounds [{mode}]")
    for n in notes:
        print(f"  note: {n}")
    if violations:
        for v in violations:
            print(f"  VIOLATION: {v}")
        return 1
    print("  clean")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
