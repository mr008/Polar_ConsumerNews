# CLAUDE.md — xbot project guide

Guidance for Claude Code working in `C:\Users\KOS\Documents\Argus\x-bot`.
(There is an unrelated global `C:\Users\KOS\CLAUDE.md` for a Playwright workspace —
ignore it here.) The full design rationale lives in `ARCHITECTURE.md`; this file is
the working guide + the hard-won lessons.

## What this is

An X/Twitter quote-tweet **curator bot**. It reads the owner's already-curated home
feed, finds posts that teach **real, useful practices for making viral consumer-app
content** (AI UGC, growth, distribution), and quote-tweets the best ~3/day with
compact, growth-first commentary. Fully remote, official-API, ~$18/mo target.

## Pipeline

```
collect ─▶ score ─▶ draft ─▶ [review] ─▶ publish
 source    teaching   LLM        manual or    dry_run
(sample/   judge +    commentary autonomous   / api
 api)      engagement + safety
```

Two swappable boundaries keep it portable (and keep the risky parts isolated):
- **`SourceAdapter`** (`ingest/`): `SampleSource` (offline fixtures) ⟷ `ApiSourceAdapter` (live X read).
- **`Publisher`** (`publish/`): `DryRunPublisher` ⟷ `ApiPublisher` (live X write).
- **`Repository`** (`storage/`): `SqliteRepository` now; same interface → Turso later.

## Stack & layout

- **Python 3.13**, `src/` layout, console script `xbot` (`xbot.cli:main`).
- Hard dep: **PyYAML only**. The whole dry-run runs on stdlib + pyyaml.
- Optional extras (`pip install -e ".[llm,x,dev]"`): `openai` (LLM, incl. Groq/xAI),
  `httpx` (live X API), `pytest`.
- Key modules: `config.py` (NS dotted-access), `models.py` (Post/Score/Draft),
  `score/` (signals, ranker, **teaching_judge**), `commentary/` (generate + safety),
  `select/rules.py`, `publish/`, `orchestrator.py`, `cli.py`.

## Commands

```
xbot initdb | collect | score [--top N] | draft | review | publish | run | report
```
`python -m xbot <cmd>` also works. Dry-run (default config) needs no keys.

## Locked product decisions (do not silently change)

- **Deployment:** fully remote, free hosting (GitHub Actions + Turso), official X API
  **pay-per-use** (read ~$0.005/post, write $0.015; a URL in a post = $0.20). Budget
  held by `scoping.max_posts_per_day`.
- **Ranking is TEACHING-FIRST.** High engagement ≠ good teaching. `quote_score =
  teaching_weight*teaching + (1-teaching_weight)*engagement`, gated by topic.
  Engagement composite favors eng/follower + echo over raw likes.
- **Teaching judge is SEMANTIC** (`score/teaching_judge.py`): an LLM reads each
  candidate and scores real teaching value, few-shot calibrated by the owner's own
  labels in `config.yaml > ranking.judge_examples`. Lexical heuristic is only the
  cheap pre-filter + offline fallback.
- **Voice:** growth-first / operator energy, skill-sharing angle, **compact "steal
  this" breakdown** (hook + 2-4 bullets + takeaway), **straight tone** (report
  claims neutrally), **about the owner** (no leading @; subtle `h/t @handle` tail),
  **no links** (embed carries source + keeps the $0.015 write tier), **never
  fabricate** (safety rejects any number not in the source).
- **Rollout:** manual `xbot review` first, then flip `mode.autonomous: true`.

## Config & secrets

- `config.yaml` = all tunables (weights, thresholds, voice, models, caps). Dotted
  access via `cfg.get("ranking.teaching_weight")`.
- `.env` = secrets ONLY, gitignored. `cli.load_dotenv()` loads it and **non-empty
  .env values override existing OS env vars** (intentional — see lesson below).
- LLM provider is config-driven (`llm.provider`: groq|xai|gemini|anthropic|auto),
  all OpenAI-compatible except Anthropic. Currently **Groq free tier**,
  `llama-3.3-70b-versatile`.

## Lessons learned (the gotchas — read before editing)

1. **Windows console is cp1252** and crashes on the UI's unicode (`•✓↱═…`). Fixed by
   reconfiguring stdout/stderr to UTF-8 in `xbot/__init__.py` (runs on import, so
   every entry path is safe). Don't remove it.
2. **`.env` overrides OS env** (non-empty values win). The user had a *stale* OS
   `GROQ_API_KEY`; with the old `setdefault` behavior the dead key won. Loader now
   does `if v: os.environ[k]=v`.
3. **No literal `{...}` in f-string prompts.** `build_system_prompt` had `{post_handle}`
   inside an f-string → NameError. Use plain text placeholders.
4. **xAI is NOT free.** A valid xAI key still 403s ("team has no credits") until the
   account is funded — pay-per-use. Groq free tier was chosen instead.
5. **Groq = free, OpenAI-compatible.** base_url `https://api.groq.com/openai/v1`.
   Verify model ids with `client.models.list()` (they change); we use
   `llama-3.3-70b-versatile`. (Groq the inference host ≠ Grok the xAI model.)
6. **Token strategy:** the LLM never sees all ~120 posts. Cheap lexical/engagement
   pre-filter narrows to ~15; the **teaching judge scores them in ONE batched call**;
   commentary is **per-post** (voice quality > the few tokens batching would save).
7. **Secrets were pasted in chat** during setup → they must be rotated. Never echo a
   key back; only write to `.env`.
8. **Phase 1/4 (`api_source.py`, `api_publisher.py`) are implemented but UNTESTED** —
   they need a live X API account + OAuth token + credits. Don't claim them verified.

## Phase status

- ✅ Phase 0 (scaffold), teaching-first ranking, semantic judge, Groq commentary — built & verified on sample data.
- 🔧 Phase 1 (live read) + Phase 4 (live write) — code written, needs X API access to test.
- ⬜ Phase 5 — flip `mode.autonomous`, deploy to GitHub Actions + Turso.

## Conventions

- New code goes behind the existing interfaces (Source/Publisher/Repository). Don't
  hardwire a provider or the X API into the pipeline.
- Keep the dry-run path dependency-light (stdlib + pyyaml); lazy-import `openai`/`httpx`.
- Run `pytest` before claiming done. Add safety golden-tests when touching filters.
- Never commit `.env` or `data/`.
