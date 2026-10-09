"""Growth experiments: config-driven A/B arms per post + period switches.

Spec: docs/superpowers/specs/2026-10-08-growth-experiments-design.md

    experiments:
      image_card:  {enabled: true, arms: [text, card],     started: 2026-10-09}
      author_bait: {enabled: true, arms: [tail, question], started: 2026-10-09}
      volume:      {enabled: true, period: true,           started: 2026-10-09}

Per-post tests assign an arm at DRAFT time by strict alternation (post 1 ->
arms[0], post 2 -> arms[1], ...) counted over live drafts, so at ~1 post/day
the arms stay balanced (random assignment would not). The first arm of each
test is the control (today's behavior). A disabled or missing test assigns
nothing and every consumer falls back to the control via arm().
"""
from __future__ import annotations

from .models import Post, is_web_source

PER_POST = ("image_card", "author_bait")
CONTROL = {"image_card": "text", "author_bait": "tail"}


def enabled(cfg, name: str) -> bool:
    return bool(cfg.get(f"experiments.{name}.enabled", False))


def arms_of(cfg, name: str) -> list[str]:
    arms = cfg.get(f"experiments.{name}.arms", None)
    return [str(a) for a in arms] if arms else [CONTROL[name]]


def started(cfg, name: str) -> str:
    return str(cfg.get(f"experiments.{name}.started", "") or "")


def arm(arms: dict | None, name: str) -> str:
    """The arm a draft/post is in for one test; control when unassigned."""
    return str((arms or {}).get(name) or CONTROL[name])


def assign_arms(cfg, repo, post: Post) -> dict[str, str]:
    """Arms for a new draft of `post`. Web sources (no author) are always the
    author_bait control and don't consume an alternation slot."""
    out: dict[str, str] = {}
    for name in PER_POST:
        if not enabled(cfg, name):
            continue
        arms = arms_of(cfg, name)
        if name == "author_bait" and is_web_source(post):
            out[name] = arms[0]
            continue
        n = sum(repo.arm_counts(name).values())
        out[name] = arms[n % len(arms)]
    return out


def volume_on(cfg) -> bool:
    return enabled(cfg, "volume")


# ---- volume period test (spec §4): three levers, graded as one ----

def threshold_delta(cfg) -> float:
    """Subtracted from thresholds.topic_fit_min / quote_worthy_min while on."""
    return float(cfg.get("experiments.volume.threshold_delta", 0.05)) if volume_on(cfg) else 0.0


def per_run(cfg) -> int:
    """Drafts a publish window may post: posting.per_run, or the volume override."""
    if volume_on(cfg):
        return int(cfg.get("experiments.volume.per_run", 2))
    return int(cfg.get("posting.per_run", 1))


def max_vet_attempts(cfg) -> int:
    """Generate + revisions the vet loop may spend on one draft (2 today)."""
    return 3 if volume_on(cfg) else 2


# ---- grading (spec §5) ----

MIN_N_PER_ARM = 10
MAX_DAYS = 21
MIN_LIFT = 0.30


def _median(xs: list[float]) -> float:
    xs = sorted(xs)
    if not xs:
        return 0.0
    m = len(xs) // 2
    return float(xs[m]) if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2.0


def _stats(rows: list[dict]) -> dict:
    """Stats over posts that HAVE a 24h outcome; n counts only those."""
    done = [r for r in rows if r.get("views") is not None]
    views = [float(r["views"] or 0) for r in done]
    eng = [float((r.get("likes") or 0) + (r.get("reposts") or 0)
                 + (r.get("replies") or 0) + (r.get("quotes") or 0)) for r in done]
    return {"n": len(done), "posted": len(rows),
            "median_views": _median(views),
            "mean_views": round(sum(views) / len(views), 1) if views else 0.0,
            "mean_eng": round(sum(eng) / len(eng), 2) if eng else 0.0}


def verdict(control: dict, other: dict, days_running: int) -> str:
    """Spec rule: decide at >=10 posts per arm OR >=21 days; a |lift| >= 30%
    in median 24h views names a winner, else 'no difference'. An arm with no
    24h outcome has no median to compare, so either one empty -> 'no data'."""
    if control["n"] == 0 or other["n"] == 0:
        return "no data"
    enough = (control["n"] >= MIN_N_PER_ARM and other["n"] >= MIN_N_PER_ARM) \
        or days_running >= MAX_DAYS
    if not enough:
        return "continue"
    # Floor at 1 view: avoids divide-by-zero and absurd lifts off a near-zero control.
    base = max(control["median_views"], 1.0)
    lift = (other["median_views"] - control["median_views"]) / base
    if lift >= MIN_LIFT:
        return "winner: b"
    if lift <= -MIN_LIFT:
        return "winner: control"
    return "no difference"


def _day(iso: str):
    """The date of an ISO timestamp, or None when it is not ISO (e.g. a
    hand-typed `started: 2026-10-9`): treated as unknown, never a crash in
    `xbot briefing` / the strategist build."""
    from datetime import date
    try:
        return date.fromisoformat(str(iso)[:10])
    except ValueError:
        return None


def summarize(rows: list[dict], cfg, today=None) -> list[dict]:
    """One dict per ENABLED test. Per-post tests: per-arm stats + verdict.
    Period tests: the on-period vs the 14 days before `started`."""
    from datetime import date, timedelta
    today = today or date.today()
    report = []
    for name in PER_POST:
        if not enabled(cfg, name):
            continue
        arms = arms_of(cfg, name)
        start = started(cfg, name)
        d = _day(start) if start else None
        days = (today - d).days if d else 0
        by_arm = {a: _stats([r for r in rows if (r.get("arms") or {}).get(name) == a])
                  for a in arms}
        v = verdict(by_arm[arms[0]], by_arm[arms[1]], days) if len(arms) > 1 else "n/a"
        if v == "winner: b":
            v = f"winner: {arms[1]}"
        elif v == "winner: control":
            v = f"winner: {arms[0]}"
        report.append({"name": name, "period": False, "started": start,
                       "days": days, "arms": by_arm, "verdict": v})
    if enabled(cfg, "volume"):
        start = started(cfg, "volume")
        s = (_day(start) if start else None) or today
        before_from = s - timedelta(days=14)
        dated = [(_day(r["posted_at"]), r) for r in rows]
        on = [r for d, r in dated if d and d >= s]
        before = [r for d, r in dated if d and before_from <= d < s]
        days_on = max((today - s).days, 1)
        report.append({
            "name": "volume", "period": True, "started": start, "days": days_on,
            "on": {**_stats(on), "posts_per_day": round(len(on) / days_on, 2)},
            "before": {**_stats(before), "posts_per_day": round(len(before) / 14, 2)},
        })
    return report


def render(report: list[dict]) -> str:
    """Markdown table(s) for the CLI and the briefing pack."""
    if not report:
        return "(no experiments enabled)"
    lines = []
    for t in report:
        if t["period"]:
            lines += [f"### {t['name']} (period, started {t['started']}, day {t['days']})", "",
                      "| window | posts | posts/day | n w/ 24h | median 24h views | mean eng |",
                      "|---|---|---|---|---|---|"]
            for label in ("before", "on"):
                s = t[label]
                lines.append(f"| {label} | {s['posted']} | {s['posts_per_day']} | {s['n']} "
                             f"| {s['median_views']} | {s['mean_eng']} |")
        else:
            lines += [f"### {t['name']} (started {t['started']}, day {t['days']}) — "
                      f"verdict: {t['verdict']}", "",
                      "| arm | posted | n w/ 24h | median 24h views | mean views | mean eng |",
                      "|---|---|---|---|---|---|"]
            for a, s in t["arms"].items():
                lines.append(f"| {a} | {s['posted']} | {s['n']} | {s['median_views']} "
                             f"| {s['mean_views']} | {s['mean_eng']} |")
        lines.append("")
    return "\n".join(lines).rstrip()
