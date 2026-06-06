"""End-to-end demo: run the full pipeline on sample data (which includes the
owner's two real example posts) and surface ONE fully-validated, ready-to-post
draft. Posting itself is dry-run until live X API access exists.

    python e2e_demo.py
"""
from xbot.cli import load_dotenv

load_dotenv()  # must run before the orchestrator picks the LLM provider

from xbot.config import load_config, db_path
from xbot.orchestrator import Orchestrator
from xbot.storage import SqliteRepository


def main():
    cfg = load_config("config.yaml")
    repo = SqliteRepository(db_path(cfg))
    repo.init_schema()
    orch = Orchestrator(cfg, repo)

    print(f"generator: {orch.generator.__class__.__name__} | "
          f"judge: {orch.judge.__class__.__name__}\n")

    collected = orch.collect()
    created = orch.make_drafts()
    pending = repo.pending_drafts()
    print(f"collected {collected} posts -> {len(created)} drafted -> "
          f"{len(pending)} passed every safety gate\n")

    if not pending:
        raise SystemExit("No clean draft produced — check the LLM key / config.")

    draft_id, draft, post = pending[0]  # top-ranked passing draft

    checks = {
        "passed safety filters": draft.safety_passed,
        "no link in text (keeps $0.015 rate + voice rule)":
            "http://" not in draft.commentary and "https://" not in draft.commentary,
        "credits source with subtle h/t": "h/t @" in draft.commentary,
        "fits a tweet (<=280 chars)": len(draft.commentary) <= 280,
        "quote-tweets a real source post": bool(post.url),
        "no fabricated numbers (only source numbers)":
            all(d in post.text for d in __import__("re").findall(r"\d+", draft.commentary)),
    }

    print("=" * 66)
    print("  READY-TO-POST DRAFT")
    print("=" * 66)
    print(draft.commentary)
    print("-" * 66)
    print(f"  quote-tweets @{post.author_handle}: \"{post.text.splitlines()[0][:55]}...\"")
    print(f"  source: {post.url}")
    print("-" * 66)
    print(f"  chars: {len(draft.commentary)}/280   |   model: {draft.model}")
    print("  readiness checks:")
    for label, ok in checks.items():
        print(f"     {'PASS' if ok else 'FAIL'}  {label}")
    print("=" * 66)

    # Dry-run 'post' it through the real publisher path (logs to posted_log).
    result = orch.approve(draft_id)
    print(f"\ndry-run publish result: {result}")

    if all(checks.values()):
        print("\n✅ ALL GATES PASSED — this draft is ready to post for real once X "
              "API access is wired.")
    else:
        raise SystemExit("A readiness check FAILED — not safe to post.")


if __name__ == "__main__":
    main()
