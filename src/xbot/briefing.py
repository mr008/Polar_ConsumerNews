"""Briefing pack builder — what the Strategist sees (AUTONOMY.md).

Deterministic pre-step: compiles the last 14 days of the bot's life from the
DB into one markdown document. The agent never gets DB credentials or free
rein to wander — it gets this curated, reproducible pack. Joins features to
outcomes so editorial choices are attributable, and includes post TEXT because
with ~20 posts/week qualitative reads outrank statistics.
"""
from __future__ import annotations

from datetime import timedelta

from .models import utcnow

WINDOW_DAYS = 14


def _posted_with_outcomes(repo) -> list[dict]:
    cutoff = (utcnow() - timedelta(days=WINDOW_DAYS)).isoformat()
    rows = repo.conn.execute(
        "SELECT pl.our_tweet_id, pl.author_handle, pl.commentary, pl.posted_at_pt "
        "FROM posted_log pl WHERE pl.posted_at >= ? AND pl.our_tweet_id != '' "
        "ORDER BY pl.posted_at DESC", (cutoff,)).fetchall()
    out = []
    for r in rows:
        tid = r["our_tweet_id"]
        f = repo.conn.execute(
            "SELECT * FROM post_features WHERE our_tweet_id=?", (tid,)).fetchone()
        oc = repo.conn.execute(
            "SELECT milestone, likes, reposts, replies, views FROM post_outcomes "
            "WHERE our_tweet_id=? ORDER BY captured_at ASC", (tid,)).fetchall()
        out.append({
            "id": tid, "author": r["author_handle"],
            "posted_pt": (r["posted_at_pt"] or "")[:16],
            "text": (r["commentary"] or "")[:400],
            "features": {k: f[k] for k in f.keys()} if f else {},
            "outcomes": [{k: o[k] for k in o.keys()} for o in oc],
        })
    return out


def build_briefing(repo, cfg) -> str:
    lines = ["# xbot briefing pack", "",
             f"Window: last {WINDOW_DAYS} days. Generated {utcnow().isoformat()[:16]}Z.",
             ""]

    followers = repo.account_history(WINDOW_DAYS)
    if followers:
        lines += ["## Follower curve", ""]
        prev = None
        for f in followers:
            delta = "" if prev is None else f" ({f['followers'] - prev:+d})"
            lines.append(f"- {f['day']}: {f['followers']}{delta}")
            prev = f["followers"]
        lines.append("")

    lines += ["## Published posts (features + outcome milestones)", ""]
    posted = _posted_with_outcomes(repo)
    if not posted:
        lines += ["(nothing published in the window)", ""]
    for p in posted:
        f = p["features"]
        tag = (f"route={f.get('route', '?')} kind={f.get('kind', '?')} "
               f"fmt={f.get('format', '?')} hour={f.get('window_hour', '?')} "
               f"teach={f.get('teaching')}") if f else "(untagged)"
        lines += [f"### {p['posted_pt']} · h/t @{p['author']} · {tag}", "",
                  "```", p["text"], "```"]
        if p["outcomes"]:
            oc = " · ".join(
                f"{o['milestone']}: {o.get('views', 0) or 0}v/"
                f"{(o.get('likes', 0) or 0) + (o.get('reposts', 0) or 0) + (o.get('replies', 0) or 0)}e"
                for o in p["outcomes"])
            lines.append(f"outcomes: {oc}")
        else:
            lines.append("outcomes: (none captured yet)")
        lines.append("")

    lines += ["## Daily run totals (reads/judged/drafted/posted)", ""]
    for d in repo.daily_run_totals(WINDOW_DAYS):
        lines.append(f"- {d['day']}: read {d['read']}, judged {d['judged']}, "
                     f"drafted {d['drafted']}, posted {d['posted']}")
    lines.append("")

    problems = repo.activity_drafts(["failed", "blocked", "stale"],
                                    within_hours=WINDOW_DAYS * 24)
    lines += [f"## Draft problems ({len(problems)})", ""]
    for e in problems[:20]:
        lines.append(f"- [{e['status']}] @{e['author']}: {e['note']}")
    lines.append("")

    shadow = repo.curator_shadow_recent(within_hours=WINDOW_DAYS * 24)
    picks = [s for s in shadow if s["verdict"] == "pick"]
    lines += [f"## Curator shadow ({len(shadow)} verdicts, {len(picks)} picks)", ""]
    for s in picks[:10]:
        lines.append(f"- pick {s['tweet_id']} teach={s['teaching']}: "
                     f"{(s['reason'] or '')[:80]}")
    lines.append("")

    used = repo.agent_turns_today()
    lines += ["## Agent usage", "",
              f"- turns today: {used} (governor ledger; ceiling in "
              f"agent/constitution.yaml)", "",
              f"- reads this month: {repo.reads_this_month()} / "
              f"{cfg.get('scoping.monthly_read_budget', 0)}", ""]
    return "\n".join(lines)
