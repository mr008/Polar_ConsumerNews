# AGENTS.md — working conventions for xbot

For any AI agent (or human) working in this repo. Pairs with `CLAUDE.md` (project
guide + lessons) and `ARCHITECTURE.md` (design). This file is the **runbook**.

## Golden rules

1. **Secrets live in `.env` only** (gitignored). Never commit them, never echo a key
   back in chat. Keys pasted into chat during setup MUST be rotated by the owner.
2. **Keep the offline dry-run working with no keys.** Hard dep is PyYAML; lazy-import
   `openai` / `httpx`. If `xbot run` breaks without an API key, you broke something.
3. **Extend behind interfaces** — `SourceAdapter`, `Publisher`, `Repository`,
   `TeachingJudge`, `CommentaryGenerator`. Don't hardwire a provider or HTTP call
   into the pipeline.
4. **Teaching value is the point, not engagement.** Don't reintroduce engagement as
   the primary ranking signal. If you tune ranking, preserve teaching-first.
5. **Never make the bot fabricate.** The anti-fabrication check (commentary numbers
   must exist in the source) and the safety gates are load-bearing for autonomous
   posting. Don't weaken them.
6. **Run `pytest` before declaring done.** Don't claim live (Phase 1/4) features are
   verified — they can't be until X API access exists.

## Run / test

```powershell
pip install -e ".[dev]"      # + [llm] for Groq/LLM, + [x] for live X API
xbot initdb
xbot run                     # collect(sample) -> score -> draft -> dry-run
xbot score --top 10          # ranked feed with the judge's reasons
pytest -q
```

## Common tasks

- **Tune what gets posted:** `config.yaml` → `ranking.*`, `thresholds.*`,
  `scoring_weights.*`.
- **Teach the judge your taste:** add `{text, verdict, why}` entries to
  `ranking.judge_examples` (few-shot). This is how owner review decisions should feed
  back in — wire `xbot review` approvals to append here later.
- **Change the voice:** `config.yaml` → `voice.*` and `commentary/generate.py:build_system_prompt`.
- **Swap LLM provider:** `config.yaml` → `llm.provider` + the matching key in `.env`.
  Verify model ids with `client.models.list()`.

## Going live (Phase 1 + 4) — the runbook

The pipeline is ready; it needs X API credentials. Steps for the owner:

1. **X developer account:** developer.x.com → create a Project + App.
2. **OAuth 2.0 (user context, PKCE).** Scopes: `tweet.read users.read tweet.write
   offline.access`. Complete the 3-legged flow to get a **user access token** (and
   refresh token). The app authorizes via X's "Authorize app" screen — no password
   sharing.
3. **Add pay-per-use credit** on the X API billing page (a few $; reads are the cost).
4. **Fill `.env`:** `X_ACCESS_TOKEN`, `X_REFRESH_TOKEN`, `X_USER_ID`.
5. **Flip config:** `mode.source: api`, then later `mode.publisher: api`.
6. **Token refresh:** OAuth2 access tokens expire (~2h). A refresh helper (POST
   /2/oauth2/token with the refresh token + client creds) is still TODO — add it in
   `ingest/api_source.py` / a shared `xauth.py` before unattended runs.

Live API shapes (already implemented):
- Read: `GET /2/users/:id/timelines/reverse_chronological` (see `ingest/api_source.py`).
- Write: `POST /2/tweets` body `{text, quote_tweet_id}` (see `publish/api_publisher.py`).

## Deploy (Phase 5)

GitHub Actions crons in `.github/workflows/` (collect hourly, publish 3x/day). Swap
SQLite → Turso (same SQL) for persistent state on ephemeral runners. Put all keys in
Actions secrets. Keep `mode.autonomous: false` until the review phase has tuned the
judge + thresholds; then flip to `true`. The kill switch is the file `data/STOP`.
