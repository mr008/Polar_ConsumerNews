# Growth experiments — design

Date: 2026-10-08. Status: approved by owner (conversation), ready for planning.

## Why

Reach is flat. The last 7 days (publish-run daily report, 2026-10-08): 7 posts
against a cap of 21, 8–21 views per post, 0–1 engagements, followers 13 → 11 → 13
over two weeks. The `dead_man` detector trips most days. We have ideas about
what would help but no way to tell which one actually does.

This spec adds a small, config-driven experiment layer so each idea is run as
an A/B test against data the bot already collects (`post_outcomes` at 1h/6h/
24h/72h/7d, daily follower snapshots), and graded automatically by the weekly
strategist memo.

## Goals and metrics

- Primary metric: **views at 24h per post** (median across posts in an arm).
- Long-run goal: **followers per week** (from `account_metrics`).
- Secondary: engagements (likes+reposts+replies+quotes) at 24h; posts/day.

## Scope

First batch, running together:

| Test | Type | Arms |
|---|---|---|
| `image_card` | per-post | `text` (today) vs `card` (self-rendered image) |
| `author_bait` | per-post | `tail` (today: `h/t @author`) vs `question` (closing question to `@author`) |
| `volume` | period (1 week) | three levers below, on vs off |
| readable voice | baseline change (§6) | before vs after, applies to every arm |

A **manual reply sprint** (owner spends 10–15 min/day posting the
`reply-nudge` suggestions) is the planned week-2 period test. It is not in this
spec beyond the grading table being able to show it (it needs no code).

Out of scope: source images, AI-generated images, original (non-curated)
posts, changes to the safety/refusal/fabrication gates' *rules*.

## 1. Experiment system

### Config

Target state once rolled out (merged with every `enabled: false`, see Rollout):

```yaml
experiments:
  image_card:
    enabled: true
    arms: [text, card]
    started: 2026-10-09
  author_bait:
    enabled: true
    arms: [tail, question]
    started: 2026-10-09
  volume:
    enabled: true
    period: true            # no arms; on/off for a period, graded vs prior weeks
    started: 2026-10-09
```

A disabled or missing test behaves exactly like today (arm = the first,
"control" arm, and nothing is recorded for it).

### Assignment (per-post tests)

- Happens **at draft time**, because `author_bait` changes the generated text.
- **Strict alternation**, not random: the arm is `arms[n % len(arms)]` where
  `n` = number of posts already *published* with any arm recorded for that
  test. At ~1 post/day alternation keeps arms balanced; random would not.
- Exceptions:
  - Web-sourced posts (no author) are always `author_bait = tail`.
  - If a `card` upload fails at publish, the post goes out as text and is
    recorded as `text` (the record reflects what was actually posted).
- Arms are stored on the draft (`drafts.arms` TEXT, JSON) and copied to
  `post_features.arms` at publish. Both columns are added via the existing
  `ALTER TABLE … ADD COLUMN` try/except list in `SqliteRepository.init_schema`
  (works for Turso too, same SQL).

  `arms` example: `{"image_card": "card", "author_bait": "question"}`.

### Invariants

- Safety, refusal (`REFUSAL_MARKERS`), fabrication-number and publish-time
  re-vet run unchanged on every arm. Experiments never bypass a gate.
- Experiments never change *what* is posted about — only presentation, tail,
  and volume.
- Dry-run (`mode.publisher: dry_run`) assigns arms and logs features the same
  way, so the whole layer is testable offline.

## 2. `image_card`

Arm `card`:

- Render a 1200×675 PNG from the draft's own text: hook line large at top,
  bullets below, small `@handle` footer (the bot's handle, not the source's).
  Dark background, light text, one bundled open-licence font (TTF in the repo
  under `assets/fonts/`). No source media, no generated imagery.
- New optional extra `media = ["Pillow"]` in `pyproject.toml`; the dry-run path
  stays stdlib+pyyaml (lazy import; if Pillow is missing, arm falls back to
  `text` and a warning is logged).
- Upload via X media upload (`POST /2/media/upload`, OAuth 1.0a, same session
  as tweets), then attach `{"media": {"media_ids": [id]}}` to the normal
  `{"text": …}` payload. Only the main post (hook) gets the image; thread
  parts stay text.
- Failure handling: any render or upload error, including an upload
  401/402/403, → post as text, arm recorded `text`, loud
  `[publish] card skipped (…) — posting text` warning in the run log. The
  upload never stops the run: a real account problem is surfaced by the text
  `POST /2/tweets` itself (same credentials → `AccountError`, run stops), and an
  upload-only failure (e.g. a rejected multipart signature) cannot block every
  window. (Amended 2026-10-08 by the whole-branch review; the first draft
  re-raised `AccountError` from the upload.)
- Kill switch: the publishers attach/render the card only while
  `experiments.image_card.enabled` is true AND the draft's arm is `card`;
  disabling the test reaches card drafts already queued (they post as text,
  recorded `text`).
- A media post that X rejects with a non-account 4xx is retried once as text.
  Timeouts and connection errors are NOT retried (X may have accepted the
  card post; a text retry would duplicate it).
- `DryRunPublisher` writes the PNG to the scratch/`data/` dir and prints its
  path.
- Cost: billed as a normal $0.015 post. Media upload may carry a small
  per-request fee (unconfirmed from third-party guides); verify on the first
  invoice after launch.

## 3. `author_bait`

Arm `question`:

- The commentary ends with **one** pointed, specific question addressed to
  `@author` (e.g. "…did the ~4x hold past week 2, @adriamatz?") instead of the
  `h/t @author` tail. The question must reference a concrete claim in the post.
- Implementation: a per-arm instruction appended to the user prompt in
  `commentary/generate.py` (`"End with: h/t @…"` today). The offline template
  keeps the tail.
- `compose_text` (`publish/publisher.py`) currently force-appends
  `h/t @author` if the tail regex is missing. Change: an in-body `@author`
  mention counts as credit; only append the tail if the handle appears nowhere.
- `qa_commentary` META rule ("addresses the source author instead of
  teaching") becomes arm-aware: for `question` it permits exactly one closing
  question to the author, and still rejects drafts whose *lesson* is missing.
  Golden tests cover: question arm passes with lesson+question; question arm
  fails with question only; tail arm still fails on author-addressing.
- Never for web posts.

## 4. `volume` (period test, one week)

Three levers, applied together while `experiments.volume.enabled` is true.
They are a set; graded as one.

1. **Fabricated-number → revise before block.** Today a draft with a number
   not present in the source is blocked (`fabricated_number:10`,
   `fabricated_number:100` this week). New flow: call the existing `revise()`
   once with "remove every number not in the source text; do not add any",
   re-run the number gate; block only if it still fails. The gate's rule
   (including the @handle/list-marker/URL exemptions) is unchanged.
2. **Thresholds −0.05:** `ranking.topic_fit_min` 0.45 → 0.40,
   `ranking.quote_worthy_min` 0.35 → 0.30. Implemented as overrides read by
   `select/rules.py` when the test is on, so flipping it off restores the base
   values without editing them.
3. **`posting.per_run` 1 → 2**, so a window can catch up after an empty one.
   `per_day` stays 3.

Implementation notes (controller ruling, 2026-10-08 whole-branch review):

- The revision feedback that names the allowed numbers ("The ONLY numbers
  you may write as digits are: …") applies with `volume` OFF too: it is the
  normal one-revision feedback for any `fabricated_number` failure.
- What `volume` adds is a third vet attempt (`max_vet_attempts` 2 → 3), and
  that extra revision applies to EVERY failure kind (too long, fabricated
  number, QA reject, readability), not only fabricated numbers.

Graded against the two weeks before `started`: posts/day, median 24h views,
followers/week. Revert rule: if median 24h views fall by more than a third,
revert levers 2 and 3; keep lever 1 (it only rescues drafts that then pass the
unchanged gate).

## 5. Grading

- New CLI `xbot experiments [--json]`: for each enabled test, per arm: n posts,
  median and mean 24h views, mean 24h engagements, days running, and
  `verdict` (`continue` / `winner: <arm>` / `no difference`). Period tests show
  the on-period vs prior-two-weeks comparison.
- The same table is appended to the briefing pack (`briefing.py`), and each
  per-post line in the pack gains `arms=…`.
- Strategist prompt (`agent/prompts/strategist.md`) gets a fixed rule:
  **declare a winner at ≥10 posts per arm or 3 weeks after `started`,
  whichever comes first, if the median 24h-view lift is ≥30%; otherwise
  `continue`.** Below that, call it noise. The strategist proposes; it still
  never edits config. Making a winner permanent = owner sets the base config
  and disables the test.
- Decision rule is deliberately crude: at n≈10 per arm anything under ~30% is
  indistinguishable from noise.

## 6. Readable voice (baseline change, not an arm)

Owner decision 2026-10-08 after reading a draft in the current voice: "I
don't even understand this." The current `agent/voice.md` writes operator
shorthand ("channel", "CPT", "kill a test") for people who already run
growth. New target reader: **any founder or creator, no background.** This is
a dated baseline change, applied to every arm, so the per-post tests above
measure on top of it. The strategist grades it like a period test (median
24h views before vs after `voice_changed: 2026-10-09` in config).

Rules (go into `agent/voice.md`, replacing the "compact breakdown" format;
the embedded fallback prompt in `generate.py` is updated to stay byte-identical):

1. **One point per post, explained fully.** The source may have several
   tips; the post teaches ONE, end to end. The generator picks the single
   most concrete, surprising one. (Owner, 2026-10-08: "no need to cover every
   point.") Other points can become later posts only if the source is
   re-drafted, which dedupe currently prevents; accepted.
2. **Explain, don't compress.** Say what the thing is, or show the arithmetic
   ("he divided what he spent by the trials it brought in") instead of naming
   the metric. No ad-buyer or growth shorthand. A banned-term list (`CPT`,
   `CAC`, `PMF`, `LTV`, `ROAS`, `channel` as a noun, `kill`, `creative` as a
   noun, `UGC` unless spelled out) is enforced in QA as a hard FORMAT check.
3. **Tiny story, not a list:** who did it, what they did, what they found,
   what to do. No `•`/`-` bullets; one idea per line; blank line between
   thoughts.
4. **One idea per line, under 16 words.** QA rejects any line over 20 words.
5. **Every number explained**: "4 times more" says 4 times more *per what*.
6. **Last line is what to do**, then the credit (`tail`) or question.
7. **Length follows clarity.** `llm.max_commentary_chars` rises from 245 to
   262 (= 280 minus the longest `h/t @handle` tail). The `question` arm adds
   the question's length on top; `smart_trim` still guarantees ≤ 280 total.
   If a point cannot be explained in 280, the adaptive-thread path (hook +
   one reply) is used instead of compressing.
8. Unchanged: point of view, straight tone, never fabricate, no links, SKIP
   sentinel, no em dashes.

Reference example (250 chars with the tail; 265 with the question
"Did the gap hold, @adriamatz?"):

```
A founder ran one ad on two platforms to get people into his app's free trial.

He divided what he spent on each by the trials it brought in.

One platform cost 4 times more per trial. Same ad, same app.

Measure this before you spend more.

h/t @adriamatz
```

The source also had a "$30 a day per test, stop at double your cheapest"
rule. It is deliberately left out: one point, explained, beats three
compressed.

## Data flow (summary)

```
draft:    assign arms ─▶ generate (arm-aware prompt) ─▶ gates (+ revise on number) ─▶ drafts.arms
publish:  render card? ─▶ upload ─▶ POST /2/tweets ─▶ post_features.arms (actual)
harvest:  post_outcomes (unchanged)
grade:    xbot experiments / briefing table ─▶ strategist memo verdict
```

## Testing

- Unit: alternation assignment (balanced after n posts, stable under disabled
  tests, web posts forced to `tail`); card renderer produces a PNG of the right
  size with long text wrapped/trimmed; `compose_text` credit rule; QA
  arm-awareness goldens; number-gate revise-then-block; threshold overrides;
  `experiments` aggregation and verdict rule on fixture data.
- Integration: full `xbot run` offline (sample source + dry_run) with all
  three tests enabled produces drafts with arms, a PNG, and feature rows.
- `pytest` green before merge; current baseline 139 pass / 2 skip.

## Rollout

1. Merge with all three tests `enabled: false`; run one offline `xbot run`.
2. Enable `volume` + `author_bait` first (no new dependency); watch two publish
   windows.
3. Add Pillow to the CI install, enable `image_card`; check the first `card`
   post by eye on X.
4. First grading in the 2026-10-13 strategist memo.
