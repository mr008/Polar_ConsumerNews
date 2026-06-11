"""Command-line interface.

    xbot initdb        # create the SQLite schema
    xbot collect       # pull the feed into the DB
    xbot score         # rank what's been collected
    xbot draft         # generate commentary for eligible posts -> review queue
    xbot review        # manually approve/reject pending drafts (interactive)
    xbot publish       # post due items (auto mode) or report what's awaiting review
    xbot run           # collect -> draft -> publish (the full collector pass)
    xbot reply-scan    # auto-reply engine: reply to 0-1 fresh post from the feed
    xbot snapshot      # record today's follower count (once per PT day)
    xbot report        # daily summary
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import db_path, load_config
from .orchestrator import Orchestrator
from .storage import get_repository


def load_dotenv(path: str = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    import os
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if v:  # non-empty .env values WIN over stale/invalid OS env vars
            os.environ[k] = v


def _setup(args) -> Orchestrator:
    load_dotenv()
    cfg = load_config(args.config)
    repo = get_repository(cfg)
    repo.init_schema()
    return Orchestrator(cfg, repo)


def _fmt_score(s) -> str:
    return (f"qs={s.quote_score:.3f} stage1={s.stage1_score:.3f} "
            f"topic={s.topic_fit:.2f} qw={s.quote_worthy:.2f}")


def cmd_initdb(args):
    load_dotenv()
    cfg = load_config(args.config)
    repo = get_repository(cfg)
    repo.init_schema()
    backend = "Turso" if __import__("os").environ.get("TURSO_DATABASE_URL") else db_path(cfg)
    print(f"✓ schema ready ({backend})")


def cmd_collect(args):
    orch = _setup(args)
    n = orch.collect()
    print(f"✓ collected {n} posts (source={orch.cfg.get('mode.source')})")


def cmd_score(args):
    orch = _setup(args)
    posts, scores = orch.score()
    print(f"Scored {len(scores)} posts. Top {min(args.top, len(scores))}:\n")
    by_id = {p.tweet_id: p for p in posts}
    for s in scores[: args.top]:
        p = by_id[s.tweet_id]
        print(f"  @{p.author_handle:<18} {_fmt_score(s)}")
        print(f"     {p.text.splitlines()[0][:80]}")
        reason = orch.judge_reasons.get(s.tweet_id)
        if reason:
            print(f"     judge: {reason}")


def cmd_draft(args):
    orch = _setup(args)
    created = orch.make_drafts()
    print(f"✓ created {len(created)} draft(s) (generator={orch.generator.__class__.__name__})\n")
    for c in created:
        flag = "PASS" if c["ok"] else f"BLOCKED ({c['notes']})"
        print(f"--- draft #{c['draft_id']}  [{flag}]  qs={c['score'].quote_score:.3f} ---")
        print(c["draft"].commentary)
        print(f"   ↱ quoting @{c['post'].author_handle}\n")


def cmd_review(args):
    orch = _setup(args)
    pending = orch.repo.pending_drafts()
    if not pending:
        print("No pending drafts. Run `xbot draft` first.")
        return
    if not sys.stdin.isatty():
        print(f"{len(pending)} pending draft(s) (non-interactive — listing only):\n")
        for did, draft, post in pending:
            print(f"#{did}  @{post.author_handle}")
            print(draft.commentary + "\n")
        print("Run in a real terminal to approve/reject interactively.")
        return
    print(f"{len(pending)} pending draft(s). [a]pprove / [r]eject / [s]kip / [q]uit\n")
    for did, draft, post in pending:
        print("=" * 60)
        print(draft.commentary)
        print(f"   ↱ quoting @{post.author_handle}: {post.text.splitlines()[0][:60]}")
        print("=" * 60)
        choice = input("a/r/s/q > ").strip().lower()
        if choice == "q":
            break
        if choice == "a":
            print("  ->", orch.approve(did))
        elif choice == "r":
            print("  ->", orch.reject(did, "manual"))
        else:
            print("  skipped")


def cmd_approve(args):
    orch = _setup(args)
    res = orch.approve(args.draft_id)
    print("approve:", res)
    if res.get("our_id"):
        print("posted -> https://x.com/i/status/" + res["our_id"])


def cmd_publish(args):
    orch = _setup(args)
    result = orch.publish_due()
    print("publish:", result)
    if result.get("status") == "review_required":
        print(f"  {result['pending']} draft(s) awaiting `xbot review` "
              f"(mode.autonomous is false).")


def cmd_run(args):
    orch = _setup(args)
    n = orch.collect()
    created = orch.make_drafts()
    print(f"✓ collected {n}, drafted {len(created)}.")
    result = orch.publish_due()
    print("publish:", result)


def cmd_reply_scan(args):
    orch = _setup(args)
    result = orch.reply_scan()
    mode = "DRY-RUN" if orch.cfg.get("replies.dry_run", True) else "LIVE"
    print(f"reply-scan [{mode}]: {result}")


def cmd_snapshot(args):
    orch = _setup(args)
    print("snapshot:", orch.snapshot())


def _write_cost_per_post(cfg) -> float:
    """Per published item: main post + thread parts at $0.015 each (estimated 1
    part avg) + the $0.20 attribution link reply when enabled. Legacy link mode
    = $0.20 main post."""
    from .publish.publisher import posting_format, wants_attribution_reply
    fmt = posting_format(cfg)
    if fmt == "link":
        return 0.20
    cost = 0.015
    if cfg.get("posting.adaptive_threads", False):
        cost += 0.015                       # ~1 thread part on average
    if wants_attribution_reply(cfg):
        cost += 0.20                        # the hidden source-link reply
    return cost


def cmd_report(args):
    orch = _setup(args)
    r = orch.report()
    activity = r.pop("activity", {})
    print("Daily report")
    for k, v in r.items():
        print(f"  {k:<16} {v}")

    followers = activity.get("followers", [])
    if followers:
        print("\nFollower trend (daily snapshots)")
        prev = None
        for f in followers:
            delta = "" if prev is None else f"  ({f['followers'] - prev:+d})"
            print(f"  {f['day']}  followers {f['followers']:>5}{delta} · "
                  f"following {f['following']}")
            prev = f["followers"]

    posted = activity.get("posted", [])
    problems = activity.get("problems", [])
    print(f"\nActivity log (last 72h) — {len(posted)} posted, {len(problems)} problem(s)")
    for e in posted:
        when = e["posted_at"][:16].replace("T", " ")
        print(f"  ✓ {when} {e.get('tz', 'UTC')}  h/t @{e['author']}  {e['url']}")
        print(f"      {e['commentary']}")
    for e in problems:
        print(f"  ✗ [{e['status']}] draft #{e['draft_id']} (@{e['author']})  {e['note']}")
    if not posted and not problems:
        print("  (nothing posted, no failures)")

    replies = activity.get("replies", [])
    if replies:
        print(f"\nReplies (last 72h) — {len(replies)}")
        for e in replies:
            when = e["at"][:16].replace("T", " ")
            mark = {"posted": "✓", "dry_run": "·"}.get(e["status"], "✗")
            tail = e["url"] or e["note"]
            print(f"  {mark} [{e['status']}] {when}  → @{e['author']}  {tail}")
            print(f"      {e['reply']}")

    days = activity.get("days", [])
    if days:
        post_cost = _write_cost_per_post(orch.cfg)
        print("\nRun log — daily (PT days, last 7)")
        totals = {"read": 0, "judged": 0, "drafted": 0, "posted": 0, "replied": 0}
        # Display-only spend estimate: reads ~$0.005; writes priced per
        # posting.format; engine replies $0.015.
        for d in days:
            spend = (d["read"] * 0.005 + d["posted"] * post_cost
                     + d.get("replied", 0) * 0.015)
            print(f"  {d['day']}  read {d['read']:>3} · judged {d['judged']:>3} · "
                  f"drafted {d['drafted']:>2} · posted {d['posted']} · "
                  f"replied {d.get('replied', 0)}   ≈${spend:.2f}")
            for k in totals:
                totals[k] += d.get(k, 0)
        total_spend = (totals["read"] * 0.005 + totals["posted"] * post_cost
                       + totals["replied"] * 0.015)
        print(f"  {'total':<10}  read {totals['read']:>3} · judged {totals['judged']:>3} · "
              f"drafted {totals['drafted']:>2} · posted {totals['posted']} · "
              f"replied {totals['replied']}   ≈${total_spend:.2f}")


def main(argv=None):
    # Windows consoles default to cp1252; the UI uses unicode (•, ✓, box chars).
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    parser = argparse.ArgumentParser(prog="xbot", description="X quote-tweet curator bot")
    parser.add_argument("--config", default="config.yaml")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("initdb").set_defaults(func=cmd_initdb)
    sub.add_parser("collect").set_defaults(func=cmd_collect)
    p_score = sub.add_parser("score")
    p_score.add_argument("--top", type=int, default=10)
    p_score.set_defaults(func=cmd_score)
    sub.add_parser("draft").set_defaults(func=cmd_draft)
    sub.add_parser("review").set_defaults(func=cmd_review)
    p_approve = sub.add_parser("approve")
    p_approve.add_argument("draft_id", type=int)
    p_approve.set_defaults(func=cmd_approve)
    sub.add_parser("publish").set_defaults(func=cmd_publish)
    sub.add_parser("run").set_defaults(func=cmd_run)
    sub.add_parser("reply-scan").set_defaults(func=cmd_reply_scan)
    sub.add_parser("snapshot").set_defaults(func=cmd_snapshot)
    sub.add_parser("report").set_defaults(func=cmd_report)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    finally:
        # libsql's sync client keeps a non-daemon thread alive; close it so we exit.
        try:
            from .storage.turso_repo import close_all
            close_all()
        except Exception:
            pass


if __name__ == "__main__":
    main()
