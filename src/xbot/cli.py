"""Command-line interface.

    xbot initdb        # create the SQLite schema
    xbot collect       # pull the feed into the DB
    xbot score         # rank what's been collected
    xbot draft         # generate commentary for eligible posts -> review queue
    xbot review        # manually approve/reject pending drafts (interactive)
    xbot publish       # post due items (auto mode) or report what's awaiting review
    xbot run           # collect -> draft -> publish (the full collector pass)
    xbot report        # daily summary
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import db_path, load_config
from .orchestrator import Orchestrator
from .storage import SqliteRepository


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
    repo = SqliteRepository(db_path(cfg))
    repo.init_schema()
    return Orchestrator(cfg, repo)


def _fmt_score(s) -> str:
    return (f"qs={s.quote_score:.3f} stage1={s.stage1_score:.3f} "
            f"topic={s.topic_fit:.2f} qw={s.quote_worthy:.2f}")


def cmd_initdb(args):
    load_dotenv()
    cfg = load_config(args.config)
    repo = SqliteRepository(db_path(cfg))
    repo.init_schema()
    print(f"✓ schema ready at {db_path(cfg)}")


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


def cmd_report(args):
    orch = _setup(args)
    r = orch.report()
    print("Daily report")
    for k, v in r.items():
        print(f"  {k:<16} {v}")


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
    sub.add_parser("report").set_defaults(func=cmd_report)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    main()
