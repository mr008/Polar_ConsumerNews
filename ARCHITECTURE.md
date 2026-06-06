# X Quote-Tweet Curator Bot — Architecture (v1 plan)

> Status: design only. No implementation in this document.
> Author voice for output: "smart curator, not hype account."

---

## 0. Three hard truths up front (read these first)

1. **The hard part is reading, not writing.** Posting 3x/day is ~90 writes/month — trivial, and within even the X API Free tier. The expensive, fragile, ToS-sensitive part is *reading the timeline of accounts you follow*. Design the whole system around the read problem; the write problem is nearly solved.

2. **Scraping and automated posting are different risk tiers.** Reading your own warmed, logged-in timeline at low volume is a gray-area ToS violation but low detection risk. **Automated *posting* through an unofficial/browser session is the thing that gets accounts flagged and banned.** Therefore: keep the **write path on the official API from day one**, even while the read path is a temporary local collector. This single decision satisfies your "balanced risk" constraint better than anything else.

3. **GitHub Actions cannot scrape your timeline.** No logged-in session, datacenter IP, headless detection. So the moment you want portability (Actions/server), the read path *must* be the official API. That means the scraping path is a genuine throwaway prototype — not a foundation. Plan for it to be deleted.

**Challenge to your plan:** Do **not** start fully automatic. Start with a review queue (details in §20). With balanced risk + a reputation you care about + platform automation detection, human-in-the-loop for the first few weeks is the correct call. The architecture supports flipping to auto with one config flag.

---

## 1. System overview

A pipeline that runs on a schedule:

```
                ┌─────────────────────────────────────────────────────────┐
                │                     ORCHESTRATOR                          │
                └─────────────────────────────────────────────────────────┘
   INGEST          SCORE              SELECT            DRAFT           PUBLISH
┌──────────┐   ┌──────────┐      ┌──────────┐      ┌──────────┐    ┌──────────────┐
│ Source   │──▶│ Viral +  │─────▶│ Dedup +  │─────▶│ LLM      │───▶│ Review queue │
│ Adapter  │   │ topic +  │      │ rules +  │      │ commentary│   │   OR         │
│ (API or  │   │ quote-   │      │ cooldown │      │ + safety  │   │ API publisher│
│ local)   │   │ worthy   │      │          │      │ filters   │    │ (official)   │
└──────────┘   └──────────┘      └──────────┘      └──────────┘    └──────────────┘
      │              │                  │                 │                 │
      └──────────────┴──────────────────┴────────────────┴─────────────────┘
                                     │
                              ┌──────────────┐
                              │   SQLite     │  posts, metrics (time-series),
                              │   (state)    │  scores, candidates, posted_log
                              └──────────────┘
```

Two pluggable boundaries make the whole thing portable and ToS-safe:

- **`SourceAdapter`** (read): `ApiSourceAdapter` ⟷ `LocalCollectorAdapter`. Same normalized `Post` output.
- **`Publisher`** (write): `ApiPublisher` ⟷ `ReviewQueuePublisher` (human approves) ⟷ `DryRunPublisher`.

Ingestion is a **sampling loop, not a single fetch** — "fast engagement growth" requires re-polling candidates over time to compute velocity (likes/hour). The orchestrator runs frequently to *collect and score*, and rarely (3x/day) to *publish*.

**Recommended language: Python.** Best LLM SDKs, stdlib SQLite, clean GitHub Actions story, mature HTTP/scraping libs. (Node is viable; Python wins on the data/LLM glue.)

---

## 2. Recommended architecture — v1, local

- Single Python package, run by a local scheduler (Windows Task Scheduler or an APScheduler long-running process).
- **Read:** `LocalCollectorAdapter` (temporary) *or* `ApiSourceAdapter` if you have access. Reads are scoped to a **single X List** you create containing your high-quality accounts (cleaner than the full following graph — see Open Questions).
- **Write:** `ApiPublisher` on the official API (Free tier covers 90 posts/mo). If you have zero API access at all yet, write goes to `ReviewQueuePublisher` and you post the approved draft manually until API write access exists.
- **State:** local SQLite file `data/state.db`.
- **Secrets:** `.env` (gitignored), never in config.
- **Two cadences:**
  - *Collector job* every 20–30 min: ingest new posts, re-poll watchlisted candidates, score.
  - *Publisher job* 3x/day at jittered times: pick best unposted candidate, draft, filter, queue/post.

This is the most defensible v1: cheap, observable, kill-switchable, and the risky read path is isolated behind one swappable class.

---

## 2a. DEPLOYMENT DECISION (locked): Fully remote, official API, pay-per-use

**Supersedes the earlier hybrid plan.** Three constraints set this: (1) budget < ~$20/mo, (2) **no always-on laptop** → must be fully remote, (3) **X API now bills pay-per-use** (launched Nov 2025, default for new developers), which removes the old $200/mo Basic-tier wall.

**Result: the clean, fully-remote, official-API design is now also the cheapest. The local scraper is no longer needed — drop it.** That eliminates the only ToS/ban-risk component from the system.

### Pay-per-use pricing (verify on first bill)
- **Post read: $0.005** per post returned.
- **Create post: $0.015** — but **$0.20 if the post contains a URL.** Requirement #7 (no attribution link) keeps every post at $0.015.
- **User lookup: $0.01** (cache; follower counts move slowly). **Owned reads: $0.001.**
- **24h deduplication (reported):** re-reading the same post within 24h is charged once → **velocity re-polling is effectively free.** This makes cost depend on *unique posts ingested per day*, not poll frequency.
- No subscription / no minimum. 10–20% back in xAI/Grok credits when buying X API credits.

### Cost model
> **Monthly read cost ≈ (unique posts evaluated per day) × $0.005 × 30.** Frequency of polling is free (dedup); only breadth costs money.

| Item | Assumption | Cost/mo |
|---|---|---|
| Reads | ~100–120 unique posts/day × $0.005 × 30 | ~$15–18 (capped by config) |
| Author lookups | ~100 accounts, cached, weekly refresh | ~$1 |
| Writes | 3 single posts/day × $0.015 (no URLs) | ~$1.40 |
| LLM commentary | a few calls/day (offset by xAI credit kickback) | ~$1–3 |
| Hosting (Actions + Turso free tiers) | — | $0 |
| **Total** | | **~$18–23/mo, held under $20 via the read cap** |

The **compact single-post** format keeps writes near-free (~$1.40/mo); cost is almost all reads, so `max_posts_per_day` is the dial — 100 reads/day ≈ **~$18/mo all-in**.

### Components & hosting (all free tiers)
- **Compute → GitHub Actions cron** (recommended): a *collector* workflow ~hourly + a *publisher* workflow 3x/day. Short runs keep you inside the 2,000 free Actions-minutes/mo (≈810 min/mo at this cadence). Alternatives: Google Cloud Run + Cloud Scheduler, Oracle Cloud Always-Free VM (real always-on box), or Vercel Cron.
- **State → Turso** free tier (SQLite-compatible — schema from §10 unchanged).
- **Reads + writes → official X API** (pay-per-use), via `ApiSourceAdapter` / `ApiPublisher`.
- **Secrets →** GitHub Actions encrypted secrets.

### Human-in-the-loop: manual review during test, then fully automatic
- **Test phase (first ~2–4 weeks):** `publisher: review_queue`. The cloud **collector** keeps filling a draft queue in Turso. You **review manually** by running a local CLI — `xbot review` — whenever you like (laptop only needs to be on *when you review*, not 24/7). It shows each proposed quote tweet (source link + generated commentary); you approve → it posts via the API, or you reject → it's discarded. No Telegram, no web app, no extra service.
- **Production:** flip `autonomous: true` → **no human in the loop.** Bot ingests, scores, drafts, safety-filters, and posts 3x/day on its own. Safety filters (§9), rate caps (§16), the daily report, and the kill switch stay on forever even with no human approving — they are what make unattended posting safe.

---

## 3. Recommended architecture — later (GitHub Actions / server)

**Forcing function: Actions can't scrape → read path becomes official API.** The adapter pattern makes this a config swap, not a rewrite.

- **Compute:** GitHub Actions cron workflows (one for collect, one for publish) OR a small always-on box (Fly.io / a $5 VPS / Railway) running the same scheduler. Actions is simplest if API read limits fit in scheduled bursts; a tiny VPS is better if you need frequent velocity sampling (Actions cron min granularity + cold starts make 20-min sampling awkward).
- **State:** SQLite won't persist on ephemeral Actions runners. Swap to **Turso / libSQL** (SQLite-compatible, hosted — schema unchanged) or a small Postgres. Keep the storage layer behind a repository interface so this is a driver swap.
- **Secrets:** GitHub Actions encrypted secrets / server env vars.
- **Observability:** structured logs + a daily summary (what was scored, what posted, why) to a private channel (email/Slack/Discord webhook).

Portability checklist baked into v1: storage behind a repo interface, source/publish behind adapters, all tunables in config, secrets in env, no machine-specific paths.

---

## 4. Data ingestion strategy — with and without official API

### Normalized `Post` model (both paths converge here)
```
Post {
  tweet_id, author_handle, author_name, author_follower_count,
  text, created_at, url, lang,
  is_reply, is_retweet, is_quote, has_media, has_link,
  metrics_snapshot { likes, reposts, replies, quotes, views?, captured_at }
}
```

### Path A — Official API (production target)
- **Read home timeline of follows:** `GET /2/users/:id/timelines/reverse_chronological` (user-context OAuth2), or iterate a **List**'s tweets, or pull per-followed-user tweets. Request `public_metrics` + author `public_metrics` (follower count) via expansions.
- **Velocity:** store a metrics snapshot each poll; velocity = Δengagement / Δt.
- **Write/quote:** create a tweet with `quote_tweet_id` set to the source. ~90/mo fits Free tier writes.
- **Reality check (verify current pricing/limits — they change):** Free ≈ write-only + near-zero reads; Basic ≈ ~$200/mo with modest read caps; Pro ≈ ~$5k/mo. **Read caps, not write caps, are the binding constraint.** Scope reads with a List and poll only a candidate watchlist (not the whole feed repeatedly) to stay inside caps. *This pricing question gates the whole project — answer it before building (see §19).*

### Path B — No API yet (temporary local collector, prototype only)
- Reads from **your own logged-in session** (browser automation / your authenticated session) at **low volume**, scoped to one List.
- **Explicitly temporary.** Risks: violates ToS; brittle to DOM/markup changes; can trigger automation defenses; **must not drive automated posting** (read-only). Treat output as a data file, not a live service.
- **Hard rule:** even in Path B, **writes go through the official API or a human.** Never automate posting through the scraped session. This is the line between "balanced risk" and "ban risk."
- This code is a throwaway. It will not survive the move to Actions/server (§3).

**Both paths emit identical `Post` objects**, so scoring/dedup/selection/publish never know which source produced the data.

---

## 5. Ranking & viral scoring design

Two-stage funnel for cost and quality:

**Stage 1 — cheap, runs on everything (no LLM):**
`score = Σ wᵢ · normalize(signalᵢ)`

| Signal | Definition | Notes |
|---|---|---|
| Likes | log-scaled like count | log dampens whales |
| Reposts | log-scaled repost count | strongest virality signal |
| Velocity | Δengagement / Δhours since post | **requires re-polling** |
| Engagement / follower | total_eng / max(follower_count, k) | finds breakout posts from smaller accounts |
| Echo | # distinct follows surfacing same original/idea | exact (tweet_id) + near-dup (embeddings) |
| Recency | exp(−age / τ) decay | τ ≈ 6–12h, configurable |
| Topic fit | similarity to target-topic anchors | embeddings or zero-shot |

Normalize each signal to [0,1] (percentile or min-max over a rolling window) so weights are comparable. All weights live in config (§12).

**Stage 2 — LLM, runs only on top ~N (e.g. 15–20/day):**
- **Quote-worthiness** classifier: strong insight / useful framing / sharp opinion / newsworthy / concise lesson → 0–1. Reject pure links, threads-needing-context, screenshots-without-text, vague hot takes.
- **Topic fit** confirm (catches embedding false positives).
- Output a final `quote_score` combining Stage-1 score × quote-worthiness × topic-fit, gated by safety (§9).

This keeps LLM spend to a few calls/day while letting engagement math run over the full feed.

**Velocity caveat:** you can't compute growth from one fetch. Newly-seen high-potential posts go on a **watchlist** and get re-polled a few times over their first hours; the slope is the signal.

---

## 6. Deduplication strategy (layered)

1. **Exact source dedup:** never quote a `tweet_id` already in `posted_log`. Also collapse retweets/quotes to their **canonical original** before scoring.
2. **Idea/near-dup dedup:** embed candidate text; if cosine similarity to any previously *posted* item > threshold (e.g. 0.85), skip. Prevents quoting three rewordings of the same take across days.
3. **Author cooldown:** don't quote the same author within N days (config; e.g. 5) — keeps the feed varied and avoids looking like you're farming one person.
4. **Echo handling:** when many follows surface the same idea, that's a *boost* (signal #5) but you still quote **one** canonical post and mark the whole idea-cluster as consumed.

Store embeddings to make 2–4 cheap on every run.

---

## 7. Post selection rules

A candidate is **eligible** only if all hold:
- Source is an account you follow (or List member). No discovery of new accounts.
- Passes topic fit ≥ threshold.
- Passes quote-worthiness ≥ threshold.
- Not a reply/low-context fragment, not media-only, not a pure link drop.
- Passes all safety filters (§9).
- Not a dedup hit (§6), author not in cooldown.
- Author is a public creator/professional posting publicly (not a private individual).
- Source still exists at publish time (re-check immediately before posting).

Among eligible candidates, **pick the highest `quote_score`** for the slot. Enforce **≤3 published/day** and **min spacing** between posts (§16). If nothing clears thresholds, **post nothing** — silence beats a weak quote.

---

## 8. Commentary generation strategy

- **Inputs:** source text, author **name/handle**, topic, detected angle.
- **Output format: COMPACT "STEAL THIS" BREAKDOWN** (chosen default — single post, not a thread). Skeleton: **(1) hook line** naming the author + what they did → **(2) 2–4 bullets** distilling the tactic/steps → **(3) one-line takeaway** (the key lever). Fits one tweet, scannable, save-able. Optional: promote a *standout* post to a full thread (`allow_thread_for_top_posts`).
- **Tone: STRAIGHT** (decided). Report the author's claims neutrally — "he shares a case study of an account at 14M+ views/mo." No skeptical hedge ("forget the numbers"), no vouching. The bot never asserts an unverified number as fact, but doesn't editorialize doubt either; it attributes and teaches.
- **Constraints (system prompt):** **growth-first / operator voice** (punchy, energetic — matches the voice anchors), NOT measured-curator; **skill-sharing angle** — teach the tactic (why it works / the move to steal / the part people miss); **make it about YOU (the teacher), not the source** — lead with the insight in your own voice, **de-emphasize the author** (no leading @mention; subtle/optional credit only). The quote embed already shows whose post it is, so the source is never hidden — this supersedes the earlier "name the author" note. **No separate attribution link** (the embed carries the source — and a pasted URL triggers the $0.20 write tier vs $0.015); light emoji OK; no hashtag spam. **Hard floor: never fabricate** — elaborate only on (a) what's in the source post and (b) safe well-known general technique; never invent tool mechanics, steps, or numbers. Frame unverifiable claims as *their* result.
- **Grounding:** the commentary *should* add your own take/insight — opinion and framing are the value, encouraged. But a post-gen check rejects any new **factual claim or number** about the source's results that isn't in the source (anti-fabrication ≠ anti-opinion).
- **Variety:** rotate among skill-sharing framings (the move to steal / why this works / the part people miss / what to do with it) to avoid a robotic template signature.
- **Length guard:** total quote tweet (commentary only; quoted post doesn't count) within limit with margin.
- **Two-pass:** generate → self-critique against the rules → revise. Cheap and meaningfully improves quality.

Output is a **draft**, never posted directly — it goes to safety filters then queue/publisher.

---

## 9. Safety & quality filters (hard gates, fail-closed)

Run on **both** the source post and the generated commentary. Any failure → drop the candidate (don't try to "fix and post" automatically).

**Topic/exclusion filters (reject):** politics, ragebait/outrage, NSFW, harassment/personal drama, medical/legal advice, investment/trading advice, doxxing, anything targeting a private individual. *(App-revenue & growth / "$X-mo" stories are in-scope marketing content — NOT "financial advice.")*

**Quality filters (reject):**
- Source needs missing context (reply chains, "this" with no referent).
- Commentary asserts facts/numbers not in source (energetic tone is fine in lean-in mode; fabricated *facts* are not).
- Toxicity/profanity over threshold.
- Near-duplicate of past output.
- Author not clearly a public creator/professional.

**Implementation:** keyword/blocklist + a zero-shot LLM classifier for the nuanced categories + a final "does the commentary make any claim absent from the source?" check. Log every rejection with reason for audit and tuning.

---

## 10. Storage design (SQLite for v1)

```sql
accounts(handle PK, name, follower_count, is_public_creator, added_at)

posts(                          -- one row per seen tweet (canonical)
  tweet_id PK, author_handle, text, created_at, url, lang,
  is_reply, is_retweet, is_quote, has_media, has_link,
  first_seen_at, embedding BLOB)

post_metrics(                   -- time-series for velocity
  id PK, tweet_id FK, likes, reposts, replies, quotes, views,
  captured_at,
  UNIQUE(tweet_id, captured_at))

scores(                         -- latest computed scores
  tweet_id FK, stage1_score, velocity, eng_per_follower, echo_count,
  recency, topic_fit, quote_worthy, quote_score, scored_at)

candidates(                     -- watchlist + selection state
  tweet_id FK, status,         -- watching|eligible|drafted|queued|posted|skipped
  skip_reason, idea_cluster_id, updated_at)

drafts(
  id PK, tweet_id FK, commentary, model, created_at,
  safety_passed, safety_notes)

posted_log(                     -- already posted (dedup source of truth)
  id PK, source_tweet_id, our_tweet_id, author_handle,
  commentary, posted_at)

idea_clusters(id PK, centroid_embedding BLOB, example_tweet_id, created_at)

state(key PK, value)            -- cursors, last_run, counters
```

Indexes on `posts.author_handle`, `post_metrics.tweet_id`, `posted_log.source_tweet_id`, `candidates.status`. Wrap all access in a **repository layer** so the later Turso/Postgres swap is a driver change, not a rewrite.

---

## 11. Scheduler design (3x/day posting)

- **Two jobs, two cadences:**
  - **Collector:** every ~20–30 min — ingest, re-poll watchlist, score. (On Actions, the practical floor is coarser; a small VPS is better for fine-grained velocity.)
  - **Publisher:** 3 slots/day inside **configurable windows** (e.g. morning / midday / evening in your audience's timezone) with **randomized jitter** (±X min) — never exactly every 8 hours (that cadence reads as a bot).
- **Spacing guard:** enforce ≥ min_gap (e.g. 90 min) between posts even if a window fires early.
- **Idempotency:** publisher checks `posted_log` + daily counter before acting; safe to re-run.
- **Local:** APScheduler in a long-running process, or Windows Task Scheduler calling the CLI. **Later:** GitHub Actions `schedule:` cron (one workflow per job).
- **Kill switch:** a config flag / sentinel file that halts publishing instantly.

---

## 12. Config file design (`config.yaml`)

Tunables in config; **secrets in `.env`** only.

```yaml
mode:
  source: api                  # api (official, pay-per-use) | local_collector (deprecated)
  publisher: review_queue      # TEST: review_queue (manual `xbot review`) → PROD: api
  autonomous: false            # TEST: false → flip true for fully automatic posting

scoping:
  source_timeline: home        # full home timeline — following is already curated, no List
  max_posts_per_day: 120       # HARD read cap → pins X API read cost under budget
  languages: [en]

scoring_weights:               # must sum ~1.0 (normalized signals)
  likes: 0.15
  reposts: 0.20
  velocity: 0.20
  eng_per_follower: 0.15
  echo: 0.15
  recency: 0.10
  topic_fit: 0.05
thresholds:
  topic_fit_min: 0.6
  quote_worthy_min: 0.65
  near_dup_cosine: 0.85
recency_tau_hours: 8

topics:
  # Mission: share skills for making VIRAL CONSUMER-APP CONTENT.
  focus: viral_consumer_app_content
  include: [ai ugc, content marketing, viral content tactics, app growth,
            distribution hacks, consumer apps, product, startups]

posting:
  per_day: 3
  windows: ["08:30-10:00", "12:30-14:00", "18:00-20:00"]
  timezone: "America/Los_Angeles"
  jitter_minutes: 25
  min_gap_minutes: 90
  author_cooldown_days: 5

safety:
  exclude: [politics, ragebait, nsfw, harassment, personal_drama,
            medical_advice, legal_advice]
  # NOTE: app-revenue / growth / "$X/mo" stories are IN-SCOPE marketing content.
  # The "financial advice" we still avoid = investment/trading/"buy this asset" only.
  block_investment_advice: true
  require_public_author: true
  toxicity_max: 0.3

llm:
  # Two-tier routing: cheap model for high-volume scoring, good model for the few published posts.
  classifier_model: "grok-4.1"        # quote-worthiness + topic-fit (cheap; or claude-haiku-4-5)
  commentary_model: "grok-4.1"        # DEFAULT start — kickback-funded ≈ $0 net.
                                      # one-line upgrade to "claude-sonnet-4-6" if voice needs it.
  embeddings_model: "text-embedding-3-small"   # dedup + idea clustering; ~$0.10/mo (local option exists)
  max_commentary_chars: 240
  temperature: 0.7
  # All models swappable via config → A/B Grok vs Sonnet during the manual-review phase.
  # NOTE: buying X API credits earns 10–20% back in xAI credits → Grok LLM cost ≈ fully offset.

voice:
  style: growth_first          # punchy operator energy; NOT measured-curator
  angle: skill_sharing         # teach the tactic: why it works / move to steal
  format: compact_breakdown    # single post: hook + 2–4 bullets + takeaway (chosen default)
  allow_thread_for_top_posts: true   # optional: full thread only for standout posts
  tone: straight               # report author's claims neutrally — no skeptical hedge, no vouching
  protagonist: me              # post is about ME (the teacher), not the source author
  author_emphasis: low         # no leading @mention; de-emphasize the source
  credit_style: subtle_tail    # CHOSEN: small "h/t @handle" tail only (embed shows the source)
  hype_ok: true                # energetic tone allowed
  include_source_link: false   # quote embed already carries it; a URL triggers $0.20 write tier
  fabrication: forbidden       # elaborate only on source + safe general technique; invent nothing
  restate_author_claims_as: author_result   # "he shares a case study of…", not verified fact
  anchors:                     # examples that define the target energy
    - https://x.com/adriamatz/status/2062619382631497931
    - https://x.com/ErnestoSOFTWARE/status/2062621170042917373

ops:
  collector_interval_minutes: 25
  watchlist_repoll_hours: [1, 3, 6]
  daily_report_webhook: <env:REPORT_WEBHOOK>
  kill_switch_file: "data/STOP"
```

---

## 13. Suggested project structure

```
x-bot/
  config.yaml
  .env                      # secrets, gitignored
  pyproject.toml
  data/
    state.db
  src/xbot/
    __init__.py
    config.py               # load + validate config
    models.py               # Post, Draft, Score dataclasses
    storage/
      repo.py               # repository interface
      sqlite_repo.py        # v1 driver  (turso_repo.py later)
    ingest/
      adapter.py            # SourceAdapter interface
      api_source.py         # official API reader
      local_source.py       # temporary collector (throwaway)
      normalize.py
    score/
      signals.py            # engagement/velocity/recency/echo math
      topic.py              # topic-fit classifier
      quoteworthy.py        # LLM quote-worthiness
      ranker.py
    dedup/
      embeddings.py
      dedup.py
    select/
      rules.py
    commentary/
      generate.py           # LLM draft + self-critique
      safety.py             # hard gates
    publish/
      publisher.py          # Publisher interface
      api_publisher.py
      review_queue.py
      dryrun.py
    schedule/
      collector_job.py
      publisher_job.py
    cli.py                  # collect | score | review | publish | report
    orchestrator.py
  tests/
  .github/workflows/        # collect.yml, publish.yml (later)
  ARCHITECTURE.md
```

---

## 14. Main components & responsibilities

| Component | Responsibility |
|---|---|
| **Orchestrator** | Wires the pipeline; runs collect vs publish flows |
| **SourceAdapter** | Read timeline → normalized `Post`s (API or local) |
| **Normalizer** | Collapse retweets/quotes to canonical; fill model |
| **Scorer/Ranker** | Stage-1 weighted score + Stage-2 LLM gating |
| **Dedup** | Exact + near-dup + author cooldown + echo clustering |
| **Selector** | Apply eligibility rules; pick best for the slot |
| **CommentaryGen** | Draft + self-critique commentary, name the author |
| **SafetyFilter** | Hard fail-closed gates on source + commentary |
| **Publisher** | Review queue / official API / dry-run |
| **Scheduler** | Two cadences, windows+jitter, spacing, idempotency |
| **Repository** | All DB access behind one interface |
| **Reporter** | Daily "what/why" summary; audit trail |
| **Kill switch** | Instant halt of publishing |

---

## 15. Failure modes & mitigations

| Failure | Mitigation |
|---|---|
| API read limit exhausted | Scope via List; poll only watchlist; backoff; cache; degrade to "post nothing" |
| Local collector breaks (DOM change) | It's throwaway by design; alert + pause; do not auto-post stale data |
| Source deleted between scoring and posting | Re-fetch + verify existence immediately before publishing |
| LLM hallucinates a fact | Claim-grounding check vs source; self-critique pass; safety gate; review queue |
| Duplicate post (race / re-run) | `posted_log` idempotency + daily counter check before publish |
| Posting cadence looks botlike | Windows + jitter + min-gap; no fixed interval |
| Account flagged/limited | Conservative caps, review queue first, monitor reach, kill switch |
| State lost (ephemeral runner) | Hosted DB (Turso/Postgres) for Actions/server |
| Secret leak | `.env`/Actions secrets only; never in config or repo |
| Bad take slips through | Human review gate during ramp; per-rejection logging to tune filters |
| Nothing clears thresholds | Allowed outcome: post nothing that day |

---

## 16. Rate-limit & anti-spam precautions

- **Hard cap 3/day**, enforced in code, not just config.
- **Min gap** (e.g. ≥90 min) between posts; randomized within windows.
- **No fixed interval** posting (avoid the every-8-hours bot tell).
- **Per-author cooldown** so you don't farm one account.
- **Commentary variety** (rotating framings) — no repeated template.
- **Human-plausible hours** only (your config windows).
- **Ramp up**: 1/day for the first week, then 2, then 3.
- **Reach monitoring**: watch your own quote tweets' impressions; a sudden cliff can indicate limiting → auto-pause + alert.
- **Reads stay on your own session/List, low volume** (Path B); writes on official API.
- **Global kill switch** + daily report so a human always sees what's happening.

---

## 17. Test plan

- **Unit:** scoring math (normalization, velocity, decay), dedup (exact + cosine threshold), selection rules, template/length, config validation.
- **Safety golden tests:** curated adversarial inputs (politics, ragebait, medical/financial/legal advice, private individuals, hallucination bait) must all be rejected. This suite gates every release.
- **Fixtures/integration:** recorded `Post` JSON fixtures drive the pipeline end-to-end with no network.
- **Dry-run mode:** full pipeline, `DryRunPublisher` logs the exact tweet it *would* send.
- **Backtest:** run scorer over a labeled sample; check the items you'd have quoted match your judgment (tune weights).
- **Idempotency test:** re-run publisher → no double post.
- **Canary:** review-queue period where you approve/reject and the disagreement rate tunes thresholds before any automation.

---

## 18. Phased implementation roadmap

- **Phase 0 — Scaffolding:** repo, config, models, SQLite repo, CLI, dry-run plumbing.
- **Phase 1 — Ingest:** one `SourceAdapter` (local collector *or* API), normalize, store, watchlist re-polling. Verify you can compute velocity.
- **Phase 2 — Score + dedup:** Stage-1 signals, embeddings, dedup, daily ranked report. **No posting** — just "here's what I'd consider."
- **Phase 3 — Draft + safety:** LLM quote-worthiness, commentary + self-critique, safety gates → **review queue**. You approve; post manually or via API on approve.
- **Phase 4 — Scheduler + API publish:** windows/jitter/spacing/idempotency; official API write path; review gate still on; ramp 1→2→3/day.
- **Phase 5 — Graduate + port:** flip `autonomous: true` for vetted categories once disagreement rate is low; monitoring + daily report; move to Actions/server with API reads + hosted DB.

Each phase is shippable and independently useful.

---

## 19. Open questions to answer before implementation

1. **API access & budget** — Will you get official API access, which tier, and what monthly budget for API + LLM? *(This gates feasibility — reads are the cost driver. Answer first.)*
2. **Account universe** — Use a curated **X List** (recommended) or the full following graph? How many accounts? (Drives read volume.)
3. **Scraping comfort** — Are you OK with the ToS gray area of *any* read scraping as a temporary bridge, given writes stay on the API?
4. **Timezone & windows** — Your audience's timezone and preferred posting windows.
5. **Author cooldown & echo** — How aggressively to avoid repeat authors; is the cross-feed echo a strong boost or a tiebreaker?
6. **Voice samples** — 5–10 examples of commentary you'd be proud of (anchors the LLM voice).
7. **"Public creator" line** — Your rule for who's quotable vs a private individual.
8. **Language scope** — English only?
9. **Hosting** — Acceptable to adopt a hosted DB (Turso/Postgres) at the Actions/server stage?
10. **Definition of done for "viral"** — Absolute thresholds, percentile within your feed, or velocity-first?

---

## 20. Recommendation — review queue first, then automate

**Start with a review queue. Do not start fully automatic.** Reasons, given your stated balanced risk + "don't get flagged/banned" + reputation:

- **Reputation risk dominates.** One overstated, tone-deaf, or out-of-context quote does more damage than a week of good ones does good. A human gate during ramp catches these while you tune.
- **Filter tuning needs ground truth.** Your approve/reject decisions are the labeled data that calibrates thresholds and safety filters. Going straight to auto throws that signal away.
- **The cost is tiny:** running `xbot review` and approving ~3 drafts is a 2-minute task; the bot already did the hard ranking and drafting.
- *(Note: with the official API write path, the queue is a **quality** gate, not a platform-risk one — posting via API is ToS-clean. So the only question the human is answering during the test is "is this a good take?")*

**Decision (locked): test with a manual review queue (local `xbot review` CLI), then go fully automatic — no human in the loop.** Graduation criteria → flip `autonomous: true`: after ~2–4 weeks, once your approval rate is consistently high (e.g. >90%) and the safety golden-test suite is green. Optionally graduate **per-category** first (auto for the safest topics, queue lingering on edge cases) before full auto. The safety filters (§9), rate caps (§16), daily report, and kill switch stay on **forever** — they are what make unattended posting safe.

This is the design that gets you growth without betting your account on an unreviewed AI.
