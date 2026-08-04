# AUTONOMY.md — the autonomous-agent overhaul (design of record)

> Companion to ARCHITECTURE.md (the v1 pipeline design). This document covers
> the evolution from open-loop pipeline to closed-loop autonomous agent.
> Decided 2026-07-31 → 2026-08-04 with the owner; built in phases (status §6).

## 0. The gap being closed

The v1 bot is automated but open-loop: it acts (collect → judge → draft →
publish) but never perceives outcomes, never learns, never strategizes, never
operates itself, never evolves. Every June incident traces to a missing loop —
the budget freeze that silently stopped posting, the missed publish windows,
34 doomed LLM calls against X's reply block. Autonomy = closing five loops,
each behind its own guardrail.

## 1. Architecture — five layers

```
┌─ CONSTITUTION ─ agent/constitution.yaml: caps, bounds, protected paths,     ─┐
│                 usage governor, audit; kill switch stays                     │
│  STRATEGIST   weekly session: grades its own past predictions, tunes config  │
│               within bounds, evolves the voice spec, designs experiments,    │
│               opens PRs                                                      │
│  CURATOR      one session per collect run owns the editorial loop: judge     │
│               the batch → compare → select → draft → self-QA → or "nothing   │
│               today" (replaces judge/commentary/QA as separate API calls)    │
│  MECHANIC     on-exception: deterministic detectors trip → session diagnoses │
│               → reduce-only repairs, 48h post-merge canary + auto-revert     │
│  SENSORS      collectors + OUTCOME HARVESTER (own-post metrics at fixed      │
│               milestones, ~$0.001/read) + post_features tagged at publish    │
└─ MACHINERY    deterministic, never agentic: publisher, safety gates,        ─┘
               publish-time re-vet, caps, windows, idempotency, kill switch
```

Agents own **judgment**; actuators and guardrails stay deterministic. The
2026-06-10 refusal-tweet incident is the permanent argument for that line.

## 2. Compute & auth (owner decisions, 2026-08-04)

- **All cognition runs on the owner's Claude subscription (Max 5x)** as
  headless Claude Code sessions (`claude -p` / claude-code-action) with
  `CLAUDE_CODE_OAUTH_TOKEN`. $0 marginal. Verified: this is the supported
  status quo (Anthropic's June-15-2026 SDK-billing change was PAUSED; the
  Agent SDK *library* is API-key-only — we don't use it).
- **Usage governor:** the bot may spend at most `daily_turn_ceiling` agent
  turns/day (constitution; ~10% of plan capacity), enforced in
  `agents/runner.py` before every session. The subscription is primarily the
  owner's — the bot never crowds it.
- **Auth ladder:** window throttled (429) → skip quietly, next run catches up.
  Auth structurally broken (401/403, billing rework) → go quiet + notify;
  owner flips `mode.editorial: pipeline` → the OLD pipeline route runs on the
  API key (~$10/mo, token-efficient by design). **The fallback is the old
  route, never the Curator on pay-per-token** (agentic sessions ≈ $60–180/mo
  on per-token billing). The pipeline route is therefore PERMANENT: always
  tested, never deleted, and it shares one voice spec with the Curator so it
  can't go stale.
- Raw Messages API calls (pipeline judge/commentary when in fallback) and
  OpenAI embeddings stay on their keys — never subscription-covered.

## 3. Guardrails and how each is enforced

| Guardrail | Enforcement |
|---|---|
| Content gates (topics, fabrication/number gate, refusal markers, re-vet) | code, fail-closed, at draft AND publish time — unchanged from v1, kept forever |
| 3/day cap, min gap, idempotency | code, checked against the DB at send time |
| Monthly read breaker, kill-switch file | code, checked before acting |
| Usage governor | code (`agents/runner.py`), checked before every session |
| Constitution bounds + change-rate | `scripts/validate_bounds.py` as a REQUIRED CI check (Phase 2) |
| Protected paths | CODEOWNERS + require-code-owner-review; agent token lacks `workflows` permission and repo admin (Phase 2) |
| Self-merge | branch protection: merge is impossible with red required checks (Phase 2+) |
| Post-merge | 48h canary vs baseline → auto-revert PR + notification (Phase 4) |
| Reduce-only asymmetry | Mechanic may make the system quieter/cheaper alone; louder/costlier needs bounds or the owner |
| Injection containment | Curator sessions get no publish creds, no network tools; untrusted feed text can at worst produce a bad draft, which faces the gates |

Prompt-level rules are guidance; every load-bearing rule has a hard backstop
in one of the rows above. The residual (unguarded) risk is taste — a safe but
mediocre post — caught days-later by the outcome loop, accepted since the
June autonomous flip.

## 4. Learning substrate

- `post_features` — tagged at publish: route, kind (qt/web), format
  (single/thread), hook line, window hour, scores, question-tail.
- `post_outcomes` — own-post engagement at milestones 1h/6h/24h/72h/7d
  (window-gated so late capture never masquerades as early), via owned reads.
- `agent/memos/` — the Strategist's committed weekly memos with graded
  predictions; `agent/decisions.log` — append-only audit of autonomous acts.
- N is small (~90 posts/mo): the design is LLM-reads-qualitatively over
  curated briefings + deterministic guardrails, not bandit math.

## 5. Cost model

X reads ~$20 (capped) + writes ~$1.5 + harvester ~$0.5 + embeddings ~$0.1
≈ **$22–24/mo hard costs**; cognition $0 on subscription. Fallback mode adds
~$10/mo (pipeline LLM on API key) only while active.

## 6. Rollout ladder (each phase earns the next)

| Phase | Ships | Status |
|---|---|---|
| 0 — Senses + auth | outcome harvester, feature tagging, usage governor + agent_usage ledger, agent-smoke workflow, this doc, constitution + prompt skeletons | **BUILT** |
| 1 — Mechanic | `detectors.py` (dead-man, budget, publish-failing, supply, agent-auth, harvest-stall) + `xbot detect`/`mechanic`, daily heartbeat workflow with rolling-issue notifications + governed LLM diagnosis | **BUILT** |
| 2 — Curator shadow | `xbot curate` per collect run: blind verdicts + drafts into curator_shadow (live queue untouched); shared voice spec (`agent/voice.md`, golden-tested byte-identical); CODEOWNERS + `validate_bounds.py` + ci.yml | **BUILT** — activates when the OAuth secret lands; cutover gated on shadow agreement |
| 3 — Curator cutover | `mode.editorial: curator`; pipeline stays as permanent fallback | pending shadow data |
| 4 — Advisory Strategist | weekly memo-only sessions **BUILT** (`xbot briefing`/`strategist` + strategist.yml commits memos); config/code PRs the owner merges come next; ≥80% merged-unmodified graduates | scaffold BUILT |
| 5 — Bounded → full self-modification | config-within-bounds auto-merges, then code PRs behind the full gate stack + canary | pending 2-4 |
| ∥ tracks | reply copilot **BUILT** (`xbot reply-nudge` + workflow pings at reply-optimal times); original-content ramp = Strategist experiments once data flows | partial |

Owner manual steps to activate everything built:
1. push + merge this branch (as mr008);
2. `claude setup-token` → repo secret `CLAUDE_CODE_OAUTH_TOKEN`;
3. dispatch `agent-smoke` once, confirm green (Curator shadow + Strategist
   then run themselves on schedule);
4. when ready for Phase 2 gates: branch-protect `main` with required checks
   `ci / tests` + `ci / bounds` and "require review from code owners".
