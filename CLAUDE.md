# CLAUDE.md — xbot project guide

Guidance for Claude Code working in this repo (`Polar_ConsumerNews`, package
name `xbot`). Originally developed on Windows (`C:\Users\KOS\Documents\Argus\x-bot`);
now checked out on macOS at
`~/Documents/projects_active26/Polar_ConsumerNews`. The full design rationale
lives in `ARCHITECTURE.md`; this file is the working guide + the hard-won lessons.

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
xbot initdb | collect | score [--top N] | draft | review | publish | run
     | reply-scan | reply-queue | reply-nudge | snapshot | harvest
     | agent-smoke | detect [--json] | mechanic | curate | briefing
     | strategist | list-sync [--dry-run] | report
```
`list-sync` builds/refreshes a PRIVATE curated read-List from author yield (keep =
ever-posted or ever-scored >= --min-qw) so collection reads only worthwhile authors
without changing who you follow; flip `scoping.source_timeline: list` + `list_id` to use it.
`python -m xbot <cmd>` also works. NOTE: config.yaml is LIVE (api source/publisher
+ Turso when TURSO_DATABASE_URL is set) — local runs touch production state.

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
  this" breakdown** (hook + 2-4 bullets + takeaway) or an **adaptive tutorial
  thread** when the source is substantial (`posting.adaptive_threads`),
  **straight tone** (report claims neutrally), **about the owner** (no leading @;
  subtle `h/t @handle` tail), **never fabricate** (safety rejects any claim
  number not in the source; list markers/@handle/URL digits exempt).
- **NO URL in the main post — ever** (`posting.format: mention`). X buries
  URL-bearing posts from non-Premium accounts (2026 algo) AND bills them $0.20 vs
  $0.015. The source link is a self-reply at the bottom (`posting.attribution_reply`,
  $0.20 — the biggest cost line; flip off to save ~$18/mo).
- **Auto-reply engine** (`replies.*`): 0-1 reply per collect run to fresh
  (<3h), on-topic posts from >5K-follower followed accounts. The follower-growth
  lever (replies ≈27x a like). Hard caps + `replies.dry_run` rollout switch +
  same kill switch. Replies never contain links/hashtags/@mentions/praise-only.
- **Rollout:** manual `xbot review` first, then flip `mode.autonomous: true`.

## Config & secrets

- `config.yaml` = all tunables (weights, thresholds, voice, models, caps). Dotted
  access via `cfg.get("ranking.teaching_weight")`.
- `.env` = secrets ONLY, gitignored. `cli.load_dotenv()` loads it and **non-empty
  .env values override existing OS env vars** (intentional — see lesson below).
- LLM provider is config-driven (`llm.provider`: anthropic|groq|xai|gemini|openai|auto),
  all OpenAI-compatible except Anthropic. Currently **Anthropic**
  (`claude-sonnet-5` for commentary + judge; Groq free tier was the original
  provider and remains a fallback option).
- **Env var names the code actually reads** (X is OAuth 1.0a, NOT OAuth 2):
  `X_API_KEY`, `X_API_SECRET`, `X_ACCESS_TOKEN`, `X_ACCESS_TOKEN_SECRET`,
  `X_USER_ID`, `ANTHROPIC_API_KEY`, `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN`,
  `SEARCH_API_KEY` (Brave, list-sync only), `CLAUDE_CODE_OAUTH_TOKEN` (CI
  curator/strategist only). Same names are the GitHub Actions repo secrets.
- Storage backend is chosen by env: `TURSO_DATABASE_URL` set → Turso (shared
  production state); unset → local SQLite in `data/`.

## Local setup (macOS)

```
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[llm,x,turso,dev]"   # dry-run only needs: pip install -e ".[dev]"
pytest                                # 139 pass, 2 skip as of 2026-09-06
cp .env.example .env                  # then fill in keys (see below)
```

**Where each key comes from** (production copies already live as GitHub repo
secrets; they cannot be read back — re-obtain from the provider if lost):
- **X:** developer.x.com → Projects & Apps → the bot's app → Keys and tokens.
  Set app permissions to Read+Write *before* generating the Access Token pair.
  `X_USER_ID` = numeric id of the bot account (`GET /2/users/me`).
- **Anthropic:** console.anthropic.com → API keys.
- **Turso:** `brew install tursodatabase/tap/turso`, `turso auth login`, then
  `turso db show <db> --url` and `turso db tokens create <db>`. Creating a *new*
  DB starts from empty state (lost posted-history → possible re-quotes); reuse the
  existing DB if at all possible.
- **Brave Search:** brave.com/search/api (free 2,000 queries/mo). Optional.
- **CLAUDE_CODE_OAUTH_TOKEN:** `claude setup-token` on a logged-in machine.
  GitHub secret only.
- To update a secret in CI: `gh secret set NAME` (repo `mr008/Polar_ConsumerNews`).

**Local runs hit production.** With `.env` filled and `TURSO_DATABASE_URL` set,
`xbot collect/draft/publish` read the live feed, write the shared DB, and can post
to X (config is api/api/autonomous). For safe local experiments leave the X and
Turso vars empty, or temporarily set `mode.source: sample` + `mode.publisher:
dry_run` in a local, uncommitted config change.

## Lessons learned (the gotchas — read before editing)

1. **Windows console is cp1252** and crashes on the UI's unicode (`•✓↱═…`). Fixed by
   reconfiguring stdout/stderr to UTF-8 in `xbot/__init__.py` (runs on import, so
   every entry path is safe). Don't remove it — harmless on macOS/Linux, and CI
   runs on ubuntu.
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
8. **A generator refusal got TWEETED (2026-06-10).** With nothing to teach, the
   LLM wrote "This post doesn't contain enough tactical content… drop the full
   thread" as commentary; the only gates that day (keywords/numbers/length)
   passed it, and `publish_due` trusted the stale `safety_passed` stamp 26h
   later. Defenses now: SKIP sentinel in the prompt, deterministic
   `REFUSAL_MARKERS` in safety.py, and a publish-time re-vet (fail-closed QA).
   Don't weaken any of the three.
9. **Truncated retweet stubs** ("RT @x: …cut off…") read like cliffhangers — the
   judge prompt explicitly scores truncated/teaser text ≤0.3, and reply targets
   exclude RTs. The 06-10 incident started with one.
10. **Digits in @handles, list markers, and URLs are NOT fabrication** — the
   number gate strips them first (real drafts got blocked over "@gregpr07").
11. **Two weeks of silent non-posting (2026-09-06 → 09-21).** The X secrets were
   re-set with an Access Token generated while the app was Read-only → every
   `POST /2/tweets` 403'd ("oauth1 app permissions"), each draft was marked
   `failed` (burning the queue every window), and `xbot publish` still exited 0,
   so runs stayed green. Now: account-level errors (401/402/permission-403) raise
   `AccountError` → the run stops, the queue stays pending, `publish` exits 1 and
   the workflow goes RED. Per-draft 403s (duplicate, reply limits) still skip.
   A 402 on reads = pay-per-use balance exhausted (collect went red 09-19).
12. **A missing LLM key must STOP, never degrade.** `get_generator` falls back to an
   offline template and QA treats "no key" as offline → template text could
   publish. With `mode.publisher: api`, a missing key raises `LLMUnavailable`
   (draft/publish/reply-back exit 2, `::error` annotation, run goes RED) and
   publish-time QA fails closed. Offline sample/dry_run keeps the template path.

## Phase status

- ✅ LIVE + AUTONOMOUS since 2026-06-06: api source + api publisher + Turso +
  GitHub Actions. Cadence (UTC crons in `.github/workflows/`): collect every 6h
  (was 3h; cut because metrics-refresh reads are not since_id-deduped), publish
  3x/day PT windows with jitter, web-content 2x/day, mechanic daily, list-sync +
  strategist weekly (Mon). Down 09-06→09-21 (read-only token + unpaid
  balance, lesson 11); all green again as of 2026-09-21.
- ✅ Growth overhaul (2026-06-11): mention format (no URL in main post), adaptive
  tutorial threads, hidden-link attribution reply, graded topic judge
  (threshold 0.45), smart_trim, publish-time re-vet, follower snapshots.
- ⚠️ Auto-reply engine: flipped to `replies.dry_run: false` on 2026-06-12, but
  X's Feb-2026 policy now 403s replies to accounts that have not mentioned you
  ("not allowed because you have not been mentioned…"). The engine is
  effectively dead; `reply-nudge` (weekday copilot) is the surviving reply path.
- ✅ Growth pass (2026-09-21): point-of-view voice (`agent/voice.md`), source
  freshness ranking (`posting.source_half_life_hours` / `max_source_age_hours`),
  and **`xbot reply-back`** (LIVE, runs in collect): auto-replies to people who
  replied to OUR posts — the reply path the X policy still allows.
- ✅ Autonomy overhaul Phases 0-2 merged (senses, governor, mechanic, curator
  shadow, strategist scaffold) — see `AUTONOMY.md`.

## Conventions

- New code goes behind the existing interfaces (Source/Publisher/Repository). Don't
  hardwire a provider or the X API into the pipeline.
- Keep the dry-run path dependency-light (stdlib + pyyaml); lazy-import `openai`/`httpx`.
- Run `pytest` before claiming done. Add safety golden-tests when touching filters.
- Never commit `.env` or `data/`.
- `.env.example` must use the exact env var names above (it once listed OAuth-2
  names like `X_CLIENT_ID` that nothing reads — 2026-09-06 fix).
