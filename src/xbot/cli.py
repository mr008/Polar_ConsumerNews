"""Command-line interface.

    xbot initdb        # create the SQLite schema
    xbot collect       # pull the feed into the DB
    xbot score         # rank what's been collected
    xbot draft         # generate commentary for eligible posts -> review queue
    xbot review        # manually approve/reject pending drafts (interactive)
    xbot publish       # post due items (auto mode) or report what's awaiting review
    xbot run           # collect -> draft -> publish (the full collector pass)
    xbot reply-scan    # auto-reply engine (DISABLED — blocked by X Feb-2026 policy)
    xbot reply-queue   # human-in-the-loop: bot drafts replies, you post them manually
    xbot snapshot      # record today's follower count (once per PT day)
    xbot harvest       # capture engagement milestones for our own recent posts
    xbot agent-smoke   # prove subscription (OAuth) agent auth works headless
    xbot list-sync     # build/refresh the curated read-List from author yield (--dry-run to preview)
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


def cmd_collect_web(args):
    orch = _setup(args)
    n = orch.collect_web()
    print(f"✓ collected {n} web article candidate(s) "
          f"(enabled={orch.cfg.get('webcontent.enabled', False)})")


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


def _wrap(text: str, width: int = 62) -> list[str]:
    import textwrap
    out: list[str] = []
    for para in ((text or "").splitlines() or [""]):
        out.extend(textwrap.wrap(para, width) or [""])
    return out


def _open_in_browser(url: str) -> bool:
    import webbrowser
    try:
        return webbrowser.open(url)
    except Exception:
        return False


def _copy_to_clipboard(text: str) -> bool:
    """Best-effort. pyperclip if installed (cross-platform); else PowerShell
    Set-Clipboard, which is UTF-16 native and survives the bot's `— • ✓` that
    the cp1252 console chokes on. Falls back to False so the caller can tell the
    user to copy the on-screen draft by hand."""
    try:
        import pyperclip  # optional extra
        pyperclip.copy(text)
        return True
    except Exception:
        pass
    import os
    import subprocess
    import tempfile
    path = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".txt", encoding="utf-8",
                                         delete=False) as f:
            f.write(text)
            path = f.name
        subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Set-Clipboard -Value ((Get-Content -Raw -Encoding UTF8 "
             f"-LiteralPath '{path}').TrimEnd())"],
            check=True, capture_output=True, timeout=15,
        )
        return True
    except Exception:
        return False
    finally:
        if path:
            try:
                os.unlink(path)
            except Exception:
                pass


def cmd_reply_queue(args):
    """Human-in-the-loop reply machine. Programmatic replies are blocked by X's
    Feb-2026 policy, so the bot finds targets + drafts replies and the OWNER
    posts them manually within the velocity window. Local + interactive only —
    never runs in CI."""
    orch = _setup(args)
    from .commentary.reply import get_reply_generator
    gen = getattr(orch, "reply_generator", None) or get_reply_generator(orch.cfg)
    if gen is None:
        print("No LLM key found — set GROQ_API_KEY (free) or another provider in .env.")
        return
    if args.fresh:
        print("Pulling a fresh home-feed read …")
        try:
            print(f"  +{orch.collect()} posts collected.")
        except Exception as e:
            print(f"  collect failed ({type(e).__name__}: {e}) — using stored posts.")

    targets = orch.reply_queue_targets(limit=args.limit)
    if not targets:
        print("No eligible reply targets right now.")
        if not args.fresh:
            print("Tip: `xbot reply-queue --fresh` pulls the latest posts (and their reply settings).")
        return

    interactive = sys.stdin.isatty()
    print(f"\n═══ REPLY SESSION — {len(targets)} candidate(s) ═══")
    if not interactive:
        print("(non-interactive shell — drafts listed only; run in a real terminal to act)")
    posted = skipped = shown = 0
    for i, post in enumerate(targets, 1):
        text, model = orch.draft_reply(post, gen)
        if not text:
            continue  # SKIP/low-material — logged blocked, won't reappear
        shown += 1
        age_min = post.age_hours * 60
        window = "FRESH" if age_min <= 30 else "ok" if age_min <= 90 else "LATE"
        url = post.url or f"https://x.com/{post.author_handle}/status/{post.tweet_id}"
        print("\n" + "─" * 66)
        print(f"[{i}/{len(targets)}] @{post.author_handle} · "
              f"{post.author_follower_count:,} followers · {age_min:.0f} min old · [{window}]")
        print("  their post:")
        for ln in _wrap(post.text):
            print("    " + ln)
        print("  your draft reply:")
        for ln in _wrap(text):
            print("  » " + ln)
        if not interactive:
            print(f"  → {url}")
            continue

        choice = input("  [p]ost · [e]dit · [o]pen · [s]kip · [q]uit > ").strip().lower()
        if choice == "q":
            break
        if choice == "o":
            _open_in_browser(url)
            choice = input("  [p]ost · [e]dit · [s]kip · [q]uit > ").strip().lower()
            if choice == "q":
                break
        if choice == "e":
            edited = input("  new reply text > ").strip()
            if edited:
                text = edited
        if choice in ("p", "e"):
            copied = _copy_to_clipboard(text)
            _open_in_browser(url)
            print("  ✓ reply on clipboard — paste (Ctrl+V) on X, then Reply."
                  if copied else "  (clipboard unavailable — copy the draft text above)")
            done = input("    [enter]=posted · paste the reply URL · [s]=didn't post > ").strip()
            if done.lower() == "s":
                orch.repo.log_reply(post.tweet_id, post.author_handle, post.text,
                                    text, model, "skipped", "owner_passed_after_open")
                skipped += 1
                print("  skipped")
            else:
                our = done.rstrip("/").split("/")[-1].split("?")[0] if done.startswith("http") else ""
                orch.repo.log_reply(post.tweet_id, post.author_handle, post.text,
                                    text, model, "posted", "manual", our)
                posted += 1
                print(f"  ✓ logged as posted ({posted} this session)")
        elif choice == "s":
            orch.repo.log_reply(post.tweet_id, post.author_handle, post.text,
                                text, model, "skipped", "owner_passed")
            skipped += 1
            print("  skipped")

    if shown == 0:
        print("\nAll candidates were low-material (nothing genuine to add). Try later or --fresh.")
    else:
        print(f"\nSession done — {posted} posted, {skipped} skipped.")


def cmd_briefing(args):
    """Compile the Strategist's briefing pack from the DB to data/briefing.md."""
    orch = _setup(args)
    from .briefing import build_briefing
    text = build_briefing(orch.repo, orch.cfg)
    out = Path("data/briefing.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"briefing: {len(text.splitlines())} lines -> {out}")


def cmd_strategist(args):
    """MEMO-ONLY Strategist session (AUTONOMY.md Phase 4 scaffold, run weekly).
    Reads the briefing + its own recent memos, writes agent/memos/<date>.md.
    Proposals only — it applies nothing; the workflow commits the memo. Always
    exits 0 (fail quiet)."""
    orch = _setup(args)
    from .agents import run_session
    from .briefing import build_briefing
    from .models import to_local, utcnow

    briefing = build_briefing(orch.repo, orch.cfg)
    memos_dir = Path("agent/memos")
    memos_dir.mkdir(parents=True, exist_ok=True)
    past = sorted(memos_dir.glob("*.md"))[-3:]
    past_text = "\n\n".join(
        f"--- memo {p.name} ---\n{p.read_text(encoding='utf-8')[:4000]}"
        for p in past) or "(no past memos — this is the first session)"
    prompt_path = Path("agent/prompts/strategist.md")
    base = prompt_path.read_text(encoding="utf-8") if prompt_path.exists() else ""
    prompt = (base
              + "\n\nTHIS SESSION IS MEMO-ONLY (Phase 0-1 of the rollout): "
              "analyze and PROPOSE — apply nothing, edit nothing. Reply with "
              "the memo markdown only.\n\nYOUR PAST MEMOS:\n" + past_text
              + "\n\nBRIEFING PACK:\n" + briefing)
    res = run_session("strategist", prompt, orch.repo, allowed_tools="Read",
                      max_turns=12)
    print(f"strategist: {res['status']}")
    if res["status"] != "ok":
        return 0
    day = to_local(utcnow(), getattr(orch.repo, "tz_name", "UTC")).date().isoformat()
    out = memos_dir / f"{day}.md"
    out.write_text(res["result"], encoding="utf-8")
    print(f"  memo -> {out} (turns={res['turns']}, "
          f"governor {res['used_today']}/{res['ceiling']})")
    return 0


def cmd_reply_nudge(args):
    """Reply copilot (AUTONOMY.md parallel track): count eligible reply targets
    from data already in the DB (zero X reads) and write data/reply_nudge.md
    for the workflow to ping the owner with. Replies stay human — X policy."""
    orch = _setup(args)
    targets = orch.reply_queue_targets(limit=10)
    if not targets:
        print("reply-nudge: no eligible targets")
        return 0
    lines = [f"## {len(targets)} reply target(s) ready",
             "", "Replies are the 27x growth lever and X forces them to stay "
             "manual. Run `xbot reply-queue` locally while these are fresh:", ""]
    for p in targets[:6]:
        age = int(p.age_hours * 60)
        lines.append(f"- @{p.author_handle} ({p.author_follower_count:,} followers, "
                     f"{age}m old): {p.text.splitlines()[0][:90]}")
    out = Path("data/reply_nudge.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"reply-nudge: {len(targets)} target(s) -> {out}")
    return 0


def cmd_snapshot(args):
    orch = _setup(args)
    print("snapshot:", orch.snapshot())


def cmd_detect(args):
    """Run the deterministic health detectors. Always exits 0 — the workflow
    layer decides what a trip means. --json emits machine output only."""
    import json as _json
    orch = _setup(args)
    from .detectors import run_detectors
    trips = run_detectors(orch.repo, orch.cfg)
    if args.json:
        print(_json.dumps(trips))
        return 0
    if not trips:
        print("detect: all healthy")
        return 0
    print(f"detect: {len(trips)} trip(s)")
    for t in trips:
        print(f"  [{t['severity']}] {t['detector']}: {t['summary']}")
    return 0


def cmd_mechanic(args):
    """Detect → (if trips) diagnose with a governed read-only session → write
    data/mechanic_report.md for the workflow to post as an issue. Fail-quiet:
    without the OAuth token the report carries the raw detector JSON only."""
    import json as _json
    orch = _setup(args)
    from .detectors import run_detectors
    trips = run_detectors(orch.repo, orch.cfg)
    if not trips:
        print("mechanic: all healthy — no report")
        return 0
    lines = ["## xbot mechanic report", ""]
    for t in trips:
        lines.append(f"- **[{t['severity']}] {t['detector']}** — {t['summary']}")
    lines += ["", "```json", _json.dumps(trips, indent=2), "```", ""]

    from .agents import run_session
    prompt_path = Path("agent/prompts/mechanic.md")
    base = prompt_path.read_text(encoding="utf-8") if prompt_path.exists() else ""
    prompt = (base + "\n\nDetector report (JSON):\n" + _json.dumps(trips)
              + "\n\nDiagnose the most likely root cause per trip and recommend "
              "the smallest reduce-only action. You are read-only this session: "
              "reply with the diagnosis as markdown, nothing else.")
    res = run_session("mechanic", prompt, orch.repo, allowed_tools="Read",
                      max_turns=8)
    if res["status"] == "ok":
        lines += ["### Diagnosis (Mechanic session)", "", res["result"], ""]
    else:
        lines += [f"_(no LLM diagnosis: {res['status']})_", ""]
    out = Path("data/mechanic_report.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"mechanic: {len(trips)} trip(s) — report at {out}")
    return 0


def cmd_curate(args):
    """Curator shadow session (Phase 2): judge recent candidates blind, store
    verdicts for agreement analysis. Never touches the live queue. Always
    exits 0 — fail-quiet is the contract."""
    orch = _setup(args)
    res = orch.curate_shadow()
    print(f"curate[shadow]: {res['status']}"
          + (f" — judged {res.get('judged', 0)}, picks {res.get('picks', 0)}"
             if res.get("status") == "ok" else ""))
    return 0


def cmd_harvest(args):
    orch = _setup(args)
    res = orch.harvest()
    print(f"harvest: {res['status']} — captured {res.get('count', 0)} snapshot(s)")
    for tid, m in (res.get("milestones") or {}).items():
        print(f"  {m:>4} → https://x.com/i/status/{tid}")


def cmd_agent_smoke(args):
    """Prove subscription auth (CLAUDE_CODE_OAUTH_TOKEN) works headless in CI
    before any real brain depends on it. Read-only, few turns, logged to the
    governor ledger like every future session. Exits non-zero on failure so the
    smoke workflow shows red — production agent flows fail quiet instead."""
    orch = _setup(args)
    from .agents import run_session
    prompt = ("You are the xbot agent-auth smoke test. Read config.yaml in the "
              "current directory and answer in exactly ONE line of the form "
              "`per_day=<posting.per_day> autonomous=<mode.autonomous>`. "
              "Do not modify anything.")
    res = run_session("smoke", prompt, orch.repo, allowed_tools="Read", max_turns=6)
    print(f"agent-smoke: {res['status']}")
    if res["status"] == "ok":
        print(f"  reply: {res['result'][:120]}")
        print(f"  turns={res['turns']} cost=${res['cost_usd']:.4f} "
              f"governor {res['used_today']}/{res['ceiling']}")
        return 0
    print(f"  detail: {res.get('detail', '')}")
    return 1


def cmd_list_sync(args):
    """Auto-update the curated read-List (xbot list-sync). Default: discovery sweep
    + apply promote/demote (creates a PRIVATE List if none configured). --dry-run
    shows the diff with no API writes / no discovery read. Following is untouched."""
    orch = _setup(args)
    if args.dry_run:
        res = orch.sync_keep_list(apply=False)
        if res["status"] == "unsupported_source":
            print("Source is not the live X API (mode.source != api) — cannot manage Lists.")
            return
        prom, dem = res.get("promote", []), res.get("demote", [])
        if res["status"] == "no_list":
            print(f"No List yet. Would CREATE it and add {len(prom)} accounts:")
        else:
            print(f"List {res['list_id']} · {res['members_n']} members")
        print(f"  PROMOTE ({len(prom)}): " + (", ".join('@' + h for h in prom) or "none"))
        print(f"  DEMOTE  ({len(dem)}): " + (", ".join('@' + h for h in dem) or "none"))
        print("\nRun without --dry-run to apply. Add --discover to also search for "
              "new accounts (billed).")
        return
    res = orch.auto_list_update(discover=args.discover)
    if res["status"] == "unsupported_source":
        print("Source is not the live X API — cannot manage Lists.")
        return
    if args.discover:
        mode = orch.cfg.get("listsync.discovery_mode", "web")
        print(f"discovery sweep ({mode}): judged {res.get('discovery_posts', 0)} new posts")
    else:
        print("promote/demote only (run with --discover to search for new accounts)")
    if res.get("created"):
        print(f"List CREATED: id={res['list_id']}")
        print(f"  NEXT: set scoping.list_id: \"{res['list_id']}\" and "
              f"scoping.source_timeline: list in config.yaml to read it.")
    print(f"applied: {res.get('summary') or '(no changes)'}")
    if res.get("failed"):
        print("could not resolve (skipped): " + ", ".join('@' + h for h in res["failed"]))


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
    list_sync = r.pop("list_sync", "")
    print("Daily report")
    for k, v in r.items():
        print(f"  {k:<16} {v}")
    if list_sync:
        print(f"  last List update  {list_sync}")

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
    sub.add_parser("collect-web").set_defaults(func=cmd_collect_web)
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
    p_rq = sub.add_parser("reply-queue")
    p_rq.add_argument("--fresh", action="store_true",
                      help="pull a fresh home-feed read first, so targets are minutes old")
    p_rq.add_argument("--limit", type=int, default=15,
                      help="max candidates to walk through (default 15)")
    p_rq.set_defaults(func=cmd_reply_queue)
    sub.add_parser("snapshot").set_defaults(func=cmd_snapshot)
    sub.add_parser("harvest").set_defaults(func=cmd_harvest)
    sub.add_parser("agent-smoke").set_defaults(func=cmd_agent_smoke)
    p_detect = sub.add_parser("detect")
    p_detect.add_argument("--json", action="store_true",
                          help="machine-readable trips only")
    p_detect.set_defaults(func=cmd_detect)
    sub.add_parser("mechanic").set_defaults(func=cmd_mechanic)
    sub.add_parser("curate").set_defaults(func=cmd_curate)
    sub.add_parser("briefing").set_defaults(func=cmd_briefing)
    sub.add_parser("strategist").set_defaults(func=cmd_strategist)
    sub.add_parser("reply-nudge").set_defaults(func=cmd_reply_nudge)
    p_ls = sub.add_parser("list-sync")
    p_ls.add_argument("--dry-run", action="store_true",
                      help="show the promote/demote diff; no API writes, no discovery read")
    p_ls.add_argument("--discover", action="store_true",
                      help="also search for NEW accounts (billed web-search + vetting); "
                           "off by default so the weekly cron only does promote/demote")
    p_ls.set_defaults(func=cmd_list_sync)
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
