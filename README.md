# xbot — X quote-tweet curator

Finds viral **consumer-app content skills** in your warmed X feed and quote-tweets
the best with compact, growth-first commentary (in *your* voice, played straight,
with a subtle `h/t`). Fully remote, official-API, ~$18/mo. See `ARCHITECTURE.md`
for the full design.

## Status

**Phase 0 — scaffolding (done).** The full pipeline runs **offline on sample data**
in dry-run, with no API keys. The two live integrations (X API read/write and the
LLM) are documented stubs that swap in via config.

## Quickstart (offline dry-run — no keys needed)

```powershell
pip install -e .            # installs pyyaml + the `xbot` command

xbot initdb                 # create the local SQLite DB
xbot run                    # collect (sample) -> score -> draft -> dry-run publish
```

Or step by step:

```powershell
xbot collect                # pull the feed (sample fixtures) into the DB
xbot score --top 10         # see the ranked feed
xbot draft                  # generate commentary for eligible posts
xbot review                 # approve/reject pending drafts (interactive)
xbot publish                # post due items (auto mode) / list awaiting review
xbot report                 # daily summary
```

`python -m xbot <cmd>` works too if you'd rather not install.

## How it's wired

```
collect ─▶ score ─▶ draft ─▶ [review] ─▶ publish
 source    signals   LLM/      manual or    dry_run
(sample/   + quote-  template  autonomous   / api
 api)      worthy    + safety
                         │
                     SQLite / Turso
```

- **`config.yaml`** — every tunable (weights, thresholds, voice, models, caps).
- **`.env`** — secrets only (copy from `.env.example`). Not needed for dry-run.
- Swap to live by setting `mode.source: api`, `mode.publisher: api`, adding keys.

## Going live (later phases)

| Phase | Do |
|---|---|
| 1 | Implement `ingest/api_source.py` (reverse_chronological timeline) |
| 3 | Add `XAI_API_KEY` → real Grok commentary (auto-detected) |
| 4 | Implement `publish/api_publisher.py` (POST /2/tweets quote_tweet_id) |
| 5 | Flip `mode.autonomous: true`; deploy via `.github/workflows/` + Turso |


## Safety

Hard gates run on every post and every generated comment: excluded topics
(politics/NSFW/medical/legal/investment), and an **anti-fabrication** rule that
rejects any number in the commentary not present in the source. App-revenue /
"$X/mo" growth content is in-scope. The kill switch is `data/STOP` (create the
file to halt publishing).
