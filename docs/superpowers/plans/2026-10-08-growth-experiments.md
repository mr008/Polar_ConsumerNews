# Growth Experiments Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a config-driven A/B experiment layer to the xbot pipeline (image card, author-bait question, volume levers), switch the public voice to a plain-language one-point-per-post style, and grade every test automatically from the outcome data the bot already harvests.

**Architecture:** A new `xbot/experiments.py` module owns config parsing, per-draft arm assignment (strict alternation) and report/verdict math. Arms ride on `Draft.arms` → `drafts.arms` → `post_features.arms` (JSON text column). Each arm plugs into one existing seam: the generator user prompt (author bait), `compose_text`/`body_budget` (credit + length), `ApiPublisher.publish` (card upload with text fallback), `select/rules.py` + `_publish_due` (volume levers). Grading reads `posted_log ⋈ post_features ⋈ post_outcomes(24h)` and surfaces in `xbot experiments`, the briefing pack and the strategist prompt.

**Tech Stack:** Python 3.13, stdlib + PyYAML (dry-run path), `requests-oauthlib` (live X), new optional extra `Pillow>=10.1` (card rendering, lazy-imported), pytest.

**Spec:** `docs/superpowers/specs/2026-10-08-growth-experiments-design.md`

## Global Constraints

- Dry-run path must keep running on stdlib + pyyaml: Pillow and the X client are lazy imports only.
- Safety, refusal (`REFUSAL_MARKERS`), fabrication-number and publish-time re-vet run unchanged on every arm; experiments never bypass a gate.
- With every experiment `enabled: false`, behavior must be identical to today (except the voice change, §6, which is a baseline change).
- `agent/voice.md` and the embedded fallback in `build_system_prompt` must stay byte-identical (existing golden test `tests/test_autonomy_phase12.py::test_voice_spec_matches_embedded`-style check at line ~108).
- Web-sourced posts (`is_web_source(post)`) never get the `question` arm.
- No URL in any main post. All posts ≤ 280 chars after composition.
- `llm.max_commentary_chars` becomes 262. Question arm body budget = 278.
- Volume levers: thresholds −0.05 (`thresholds.topic_fit_min` 0.45→0.40, `thresholds.quote_worthy_min` 0.35→0.30, applied as a delta, base values untouched), `per_run` 1→2, up to 2 fabricated-number revisions with the allowed numbers listed. (Spec said "revise first"; one revision already exists in `_vet_commentary`, so the lever is a second, better-informed revision.)
- Winner rule: ≥10 posts per arm OR ≥21 days since `started`, and |median 24h-view lift| ≥ 30%.
- Run `pytest` before every commit; baseline is 139 pass / 2 skip (plus a stray duplicate `tests/test_reply_back 2.py` in the working tree — leave it alone).
- The filesystem in this checkout is slow (iCloud); use targeted `sed -n`/`grep` on named files, not recursive greps.

## Review Focus

1. **Drafts created before the migration** have `arms = NULL`: `_row_to_draft` and `arm_counts` must treat them as `{}` and never raise. (Test in Task 1.)
2. **A handle with regex metacharacters or underscores** (`@a_b.c`) in the in-body credit check of `compose_text` must not crash or false-match. (Test in Task 3.)
3. **Media upload or media-bearing post rejected mid-publish**: the post must still go out as plain text and the recorded arm must be `text`, never a lost post. (Test in Task 4.)
4. **Empty or very long commentary passed to the card renderer** must produce a 1200×675 PNG, not an exception. (Test in Task 4.)
5. **All experiments disabled**: `evaluate()` thresholds and `_publish_due` budget are exactly the base config values. (Tests in Task 5.)

---

### Task 1: Experiment config, arm assignment, schema and repo plumbing

**Files:**
- Create: `src/xbot/experiments.py`
- Modify: `src/xbot/models.py:134-150` (Draft dataclass)
- Modify: `src/xbot/storage/sqlite_repo.py:55-67` (drafts schema), `:124-140` (post_features schema), `:189-200` (migrations), `:332-355` (add_draft/_row_to_draft), `:697-713` (log_features)
- Test: `tests/test_experiments.py`

**Interfaces:**
- Produces: `experiments.assign_arms(cfg, repo, post) -> dict[str, str]`, `experiments.arm(arms, name) -> str`, `experiments.enabled(cfg, name) -> bool`, `experiments.arms_of(cfg, name) -> list[str]`, `experiments.volume_on(cfg) -> bool`, `experiments.PER_POST`, `experiments.CONTROL`.
- Produces: `Draft.arms: dict[str, str]` (default `{}`), `repo.arm_counts(experiment) -> dict[str, int]`, `repo.log_features(f)` accepting `f["arms"]` (dict).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_experiments.py
"""Experiment layer (spec docs/superpowers/specs/2026-10-08-growth-experiments-design.md)."""
import json

from xbot.config import NS
from xbot.models import Draft, Metrics, Post, utcnow
from xbot.storage.sqlite_repo import SqliteRepository


def _repo():
    repo = SqliteRepository(":memory:")
    repo.init_schema()
    return repo


def _post(tid="1", handle="alice"):
    return Post(tweet_id=tid, author_handle=handle, author_name=handle,
                text=f"tactic {tid} alpha beta", created_at=utcnow(),
                author_follower_count=1000, metrics=Metrics(likes=1))


def _cfg(**exp):
    data = {"experiments": {
        "image_card": {"enabled": False, "arms": ["text", "card"], "started": "2026-10-09"},
        "author_bait": {"enabled": False, "arms": ["tail", "question"], "started": "2026-10-09"},
        "volume": {"enabled": False, "period": True, "started": "2026-10-09"},
    }}
    for name, on in exp.items():
        data["experiments"][name]["enabled"] = on
    return NS(data)


def test_disabled_experiments_assign_nothing():
    from xbot.experiments import assign_arms
    assert assign_arms(_cfg(), _repo(), _post()) == {}


def test_arm_default_is_control():
    from xbot.experiments import arm
    assert arm({}, "image_card") == "text"
    assert arm(None, "author_bait") == "tail"
    assert arm({"image_card": "card"}, "image_card") == "card"


def test_alternation_is_balanced_over_drafts():
    from xbot.experiments import assign_arms
    cfg = _cfg(image_card=True, author_bait=True)
    repo = _repo()
    seen = []
    for i in range(6):
        post = _post(str(i))
        repo.upsert_post(post)
        arms = assign_arms(cfg, repo, post)
        seen.append(arms)
        repo.add_draft(Draft(tweet_id=post.tweet_id, commentary="x", model="t",
                             safety_passed=True, arms=arms))
    assert [a["image_card"] for a in seen] == ["text", "card"] * 3
    assert [a["author_bait"] for a in seen] == ["tail", "question"] * 3


def test_blocked_drafts_do_not_advance_alternation():
    from xbot.experiments import assign_arms
    cfg = _cfg(image_card=True)
    repo = _repo()
    p = _post("1"); repo.upsert_post(p)
    arms = assign_arms(cfg, repo, p)
    repo.add_draft(Draft(tweet_id="1", commentary="x", model="t", arms=arms), status="blocked")
    assert assign_arms(cfg, repo, _post("2")) == arms   # still "text"


def test_web_posts_never_get_question():
    from xbot.experiments import assign_arms
    cfg = _cfg(author_bait=True)
    repo = _repo()
    web = Post(tweet_id="web:abc", author_handle="blog.com", author_name="Blog",
               text="brief", created_at=utcnow())
    assert assign_arms(cfg, repo, web)["author_bait"] == "tail"
    # ...and it does not consume an alternation slot: the first X post is
    # still the first slot (control), the second X post the second.
    repo.add_draft(Draft(tweet_id="web:abc", commentary="x", model="t",
                         arms={"author_bait": "tail"}))
    first = assign_arms(cfg, repo, _post("2"))
    assert first["author_bait"] == "tail"
    repo.upsert_post(_post("2"))
    repo.add_draft(Draft(tweet_id="2", commentary="x", model="t", arms=first))
    assert assign_arms(cfg, repo, _post("3"))["author_bait"] == "question"


def test_draft_arms_round_trip_and_legacy_rows():
    repo = _repo()
    repo.upsert_post(_post("1"))
    did = repo.add_draft(Draft(tweet_id="1", commentary="x", model="t",
                               safety_passed=True, arms={"image_card": "card"}))
    draft, _ = repo.get_draft(did)
    assert draft.arms == {"image_card": "card"}
    # pre-migration row: arms NULL must read back as {}
    repo.conn.execute("UPDATE drafts SET arms=NULL WHERE id=?", (did,))
    repo.conn.commit()
    draft, _ = repo.get_draft(did)
    assert draft.arms == {}
    assert repo.arm_counts("image_card") == {}


def test_log_features_stores_arms():
    repo = _repo()
    repo.log_features({"our_tweet_id": "our_1", "arms": {"image_card": "text",
                                                          "author_bait": "question"}})
    row = repo.conn.execute("SELECT arms FROM post_features").fetchone()
    assert json.loads(row["arms"]) == {"image_card": "text", "author_bait": "question"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_experiments.py -v`
Expected: FAIL — `ModuleNotFoundError: xbot.experiments` / `TypeError: Draft.__init__() got an unexpected keyword argument 'arms'`.

- [ ] **Step 3: Add `Draft.arms`**

In `src/xbot/models.py`, inside `class Draft`, after `parts`:

```python
    # Experiment arms this draft was assigned at draft time, e.g.
    # {"image_card": "card", "author_bait": "question"}. Empty = control
    # everywhere (experiments.py). Copied to post_features at publish.
    arms: dict = field(default_factory=dict)
```

- [ ] **Step 4: Schema + migrations + add_draft/_row_to_draft/log_features**

In `src/xbot/storage/sqlite_repo.py`:

Add `arms TEXT,` to the `drafts` CREATE after `parts TEXT,` and to `post_features` CREATE after `quote_score REAL,`.

In `init_schema`, extend the migration tuple:

```python
        for ddl in ("ALTER TABLE posted_log ADD COLUMN posted_at_pt TEXT",
                    "ALTER TABLE scores ADD COLUMN judged INTEGER DEFAULT 0",
                    "ALTER TABLE drafts ADD COLUMN parts TEXT",
                    "ALTER TABLE run_log ADD COLUMN n_replied INTEGER DEFAULT 0",
                    "ALTER TABLE posts ADD COLUMN reply_settings TEXT",
                    # 2026-10 growth experiments (spec 2026-10-08)
                    "ALTER TABLE drafts ADD COLUMN arms TEXT",
                    "ALTER TABLE post_features ADD COLUMN arms TEXT"):
```

Replace `add_draft`:

```python
    def add_draft(self, draft: Draft, status: str = "pending") -> int:
        cur = self.conn.execute(
            """INSERT INTO drafts (tweet_id, commentary, model, safety_passed,
                   safety_notes, status, parts, arms, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (draft.tweet_id, draft.commentary, draft.model, int(draft.safety_passed),
             draft.safety_notes, status, json.dumps(draft.parts or []),
             json.dumps(draft.arms or {}), draft.created_at.isoformat()),
        )
        self.conn.commit()
        return cur.lastrowid
```

In `_row_to_draft`, after the `parts` try/except add:

```python
        try:
            arms = json.loads(r["arms"]) if r["arms"] else {}
        except (KeyError, IndexError, ValueError, TypeError):
            arms = {}
```
and pass `arms=arms if isinstance(arms, dict) else {},` to the `Draft(...)` constructor.

Add after `set_draft_status`:

```python
    def arm_counts(self, experiment: str) -> dict[str, int]:
        """How many live drafts (pending or posted) carry each arm of one
        experiment — the alternation counter. Blocked/stale/failed drafts don't
        count: they never reached the feed."""
        rows = self.conn.execute(
            "SELECT arms FROM drafts WHERE status IN ('pending','posted') "
            "AND arms IS NOT NULL AND arms != '' "
            "AND tweet_id NOT LIKE 'web:%'").fetchall()   # web posts never alternate
        counts: dict[str, int] = {}
        for r in rows:
            try:
                a = (json.loads(r["arms"]) or {}).get(experiment)
            except (ValueError, TypeError, AttributeError):
                a = None
            if a:
                counts[a] = counts.get(a, 0) + 1
        return counts
```

Replace `log_features`:

```python
    def log_features(self, f: dict) -> None:
        self.conn.execute(
            """INSERT INTO post_features (our_tweet_id, source_tweet_id, author_handle,
                   route, kind, format, parts_n, chars, has_question, hook,
                   window_hour, teaching, topic_fit, quote_score, arms, posted_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(our_tweet_id) DO NOTHING""",
            (f.get("our_tweet_id", ""), f.get("source_tweet_id", ""),
             f.get("author_handle", ""), f.get("route", "pipeline"),
             f.get("kind", "qt"), f.get("format", "single"),
             int(f.get("parts_n", 0)), int(f.get("chars", 0)),
             int(bool(f.get("has_question", False))), (f.get("hook", "") or "")[:160],
             f.get("window_hour"), f.get("teaching"), f.get("topic_fit"),
             f.get("quote_score"), json.dumps(f.get("arms") or {}),
             f.get("posted_at", utcnow().isoformat())),
        )
        self.conn.commit()
```

- [ ] **Step 5: Create `src/xbot/experiments.py`**

```python
"""Growth experiments: config-driven A/B arms per post + period switches.

Spec: docs/superpowers/specs/2026-10-08-growth-experiments-design.md

    experiments:
      image_card:  {enabled: true, arms: [text, card],     started: 2026-10-09}
      author_bait: {enabled: true, arms: [tail, question], started: 2026-10-09}
      volume:      {enabled: true, period: true,           started: 2026-10-09}

Per-post tests assign an arm at DRAFT time by strict alternation (post 1 ->
arms[0], post 2 -> arms[1], ...) counted over live drafts, so at ~1 post/day
the arms stay balanced (random assignment would not). The first arm of each
test is the control (today's behavior). A disabled or missing test assigns
nothing and every consumer falls back to the control via arm().
"""
from __future__ import annotations

from .models import Post, is_web_source

PER_POST = ("image_card", "author_bait")
CONTROL = {"image_card": "text", "author_bait": "tail"}


def enabled(cfg, name: str) -> bool:
    return bool(cfg.get(f"experiments.{name}.enabled", False))


def arms_of(cfg, name: str) -> list[str]:
    arms = cfg.get(f"experiments.{name}.arms", None)
    return [str(a) for a in arms] if arms else [CONTROL[name]]


def started(cfg, name: str) -> str:
    return str(cfg.get(f"experiments.{name}.started", "") or "")


def arm(arms: dict | None, name: str) -> str:
    """The arm a draft/post is in for one test; control when unassigned."""
    return str((arms or {}).get(name) or CONTROL[name])


def assign_arms(cfg, repo, post: Post) -> dict[str, str]:
    """Arms for a new draft of `post`. Web sources (no author) are always the
    author_bait control and don't consume an alternation slot."""
    out: dict[str, str] = {}
    for name in PER_POST:
        if not enabled(cfg, name):
            continue
        arms = arms_of(cfg, name)
        if name == "author_bait" and is_web_source(post):
            out[name] = arms[0]
            continue
        n = sum(repo.arm_counts(name).values())
        out[name] = arms[n % len(arms)]
    return out


def volume_on(cfg) -> bool:
    return enabled(cfg, "volume")
```

- [ ] **Step 6: Run tests**

Run: `.venv/bin/pytest tests/test_experiments.py tests/test_outcomes.py tests/test_publish_due.py -v`
Expected: all PASS (existing feature-tag tests still pass; `log_features` keeps its old keys).

- [ ] **Step 7: Commit**

```bash
git add src/xbot/experiments.py src/xbot/models.py src/xbot/storage/sqlite_repo.py tests/test_experiments.py
git commit -m "Experiments: arm assignment, Draft.arms, schema + repo plumbing"
```

---

### Task 2: Readable voice (baseline): voice.md, template generator, deterministic readability gate, QA prompt, char budget

**Files:**
- Modify: `agent/voice.md` (whole file)
- Modify: `src/xbot/commentary/generate.py:95-131` (embedded prompt must equal voice.md), `:136-138` (THREAD_INSTRUCTIONS), `:186-203` (TemplateCommentaryGenerator.generate)
- Modify: `src/xbot/commentary/safety.py` (new `check_readability`, wired into `check_commentary`)
- Modify: `src/xbot/commentary/qa.py:23-33` (QA_SYSTEM)
- Modify: `config.yaml` (`llm.max_commentary_chars: 262`, `voice.format: plain_story`)
- Test: `tests/test_safety.py` (append), `tests/test_readable_voice.py`

**Interfaces:**
- Produces: `safety.check_readability(text: str) -> str` ('' = ok, else `format:<reason>`), `safety.JARGON`, `safety.MAX_WORDS_PER_LINE = 20`.
- `check_commentary` now also fails with `format:*` reasons (and `partN_format:*` for thread parts) when `voice.readable_rules` (default True) is on.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_safety.py`:

```python
def test_readability_rejects_bullets_jargon_and_long_lines():
    from xbot.commentary.safety import check_readability
    assert check_readability("• one\n• two") == "format:bullets"
    assert check_readability("- one\n- two") == "format:bullets"
    assert check_readability("Your CPT is the only number.") == "format:jargon:cpt"
    long = " ".join(["word"] * 21)
    assert check_readability(long).startswith("format:line_too_long:21")
    assert check_readability("A founder ran one ad on two platforms.\n\nMeasure it.") == ""


def test_commentary_gate_applies_readability_rules():
    post = _post("he ran one ad on 2 platforms")
    ok, reason = check_commentary(post, "• 2 platforms, same ad\n• measure it", CFG)
    assert not ok and reason == "format:bullets"
    ok, reason = check_commentary(post, "One ad, 2 platforms.\n\nMeasure it.\n\nh/t @x", CFG)
    assert ok
    ok, reason = check_commentary(post, "One ad, 2 platforms.", CFG,
                                  parts=["• step one"])
    assert not ok and reason == "part1_format:bullets"


def test_readability_rules_can_be_switched_off():
    cfg = NS({"safety": {"exclude": []}, "llm": {"max_commentary_chars": 240},
              "voice": {"readable_rules": False}})
    post = _post("he ran one ad on 2 platforms")
    ok, _ = check_commentary(post, "• 2 platforms\n• measure", cfg)
    assert ok
```

Create `tests/test_readable_voice.py`:

```python
"""The plain-language voice (spec §6): prompt rules + offline template output."""
from xbot.commentary import generate as g
from xbot.commentary.safety import check_readability
from xbot.config import NS
from xbot.models import Post, utcnow

CFG = NS({"voice": {"style": "growth_first", "credit_style": "subtle_tail"},
          "llm": {"max_commentary_chars": 262}})


def test_system_prompt_carries_the_readable_rules():
    p = g.build_system_prompt(CFG)
    for needle in ("ONE point", "any founder or creator", "No bullets",
                   "262", "h/t @"):
        assert needle in p, needle
    assert "steal this" not in p


def test_template_generator_obeys_readability_rules():
    post = Post(tweet_id="1", author_handle="alice", author_name="A",
                text="1. post daily\n2. reuse the winner\n3. track views",
                created_at=utcnow())
    draft = g.TemplateCommentaryGenerator(CFG).generate(post)
    assert check_readability(draft.commentary) == ""
    assert draft.commentary.endswith("h/t @alice")
    assert len(draft.commentary) <= 262 + len("\n\nh/t @alice")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_safety.py tests/test_readable_voice.py -v`
Expected: FAIL — `ImportError: cannot import name 'check_readability'`; prompt assertions fail.

- [ ] **Step 3: Readability gate in `safety.py`**

Add after `MENTION = ...`:

```python
# READABLE VOICE (spec 2026-10-08 §6): every post must be followable by a
# founder or creator with no ads/growth background. Deterministic part of the
# rule set; the QA gate judges the softer "is ONE point explained" half.
JARGON = ["cpt", "cac", "pmf", "ltv", "roas", "arpu", "cpm", "cpa", "ctr",
          "mrr", "arr", "ugc"]
MAX_WORDS_PER_LINE = 20
_BULLET_LINE = re.compile(r"^\s*(?:•|-|\*)\s+")


def check_readability(text: str) -> str:
    """'' when the text obeys the plain-language rules, else 'format:<why>'.
    Checks: no bullet lines, no growth shorthand, no line over 20 words."""
    for line in (text or "").splitlines():
        if _BULLET_LINE.match(line):
            return "format:bullets"
        n = len(line.split())
        if n > MAX_WORDS_PER_LINE:
            return f"format:line_too_long:{n}"
    low = (text or "").lower()
    for term in JARGON:
        if re.search(rf"\b{term}\b", low):
            return f"format:jargon:{term}"
    return ""
```

In `check_commentary`, after the `too_long` check and before the URL check:

```python
    readable = bool(cfg.get("voice.readable_rules", True))
    if readable:
        why = check_readability(body)
        if why:
            return False, why
```
and inside the parts loop, after `_check_text_common` for a part:

```python
        if readable:
            why = check_readability(part)
            if why:
                return False, f"part{i}_{why}"
```

- [ ] **Step 4: Rewrite `agent/voice.md`**

Replace the whole file with:

```
You write the posts for a curator account whose mission is to SHARE SKILLS for growing consumer apps, backed by real numbers (AI UGC, creator ops, content + paid distribution, paywalls and monetization).

READER: any founder or creator, with NO ads or growth background. If they would have to already know the trick to follow the post, rewrite it.

VOICE: <<STYLE>>. Operator energy WITH A POINT OF VIEW: teach the tactic AND say what you think of it (why it works, where it breaks, the part people miss). Opinions are about the TACTIC, never invented facts; stay neutral on whether the author's numbers are true (see TONE).

FORMAT: teach ONE point per post, explained fully, in <= <<MAX_CHARS>> characters (before the h/t tail):
  - Pick the single most concrete, surprising thing in the source. Leave the rest out, even if the source has five tips. One point explained beats three compressed.
  - Tiny story, not a list: who did it, what they did, what they found, what to do. Say what each thing IS or show the arithmetic ("he divided what he spent by the trials it brought in") instead of naming a metric.
  - One idea per line, under 16 words, with a blank line between thoughts. No bullets ("•", "-", "*"), no numbered lists.
  - Every number explained: "4 times more" says 4 times more PER WHAT.
  - Plain words only. Never: CPT, CAC, PMF, LTV, ROAS, ARPU, CPM, CPA, CTR, MRR, ARR, UGC, "channel" as a noun, "kill" a test, "creative" as a noun. Say "platform", "stop the test", "the ad", "a month in revenue".
  - The last line says what to do. Then the credit tail.
  - Example (256 chars with the tail):
      A founder ran one ad on two platforms to get people into his app's free trial.

      He divided what he spent on each by the trials it brought in.

      One platform cost 4 times more per trial. Same ad, same app.

      Measure this before you spend more.

      h/t @adriamatz

PROTAGONIST: the post is about US (the teacher), not the source author. Do NOT open with their @handle. End with a small "h/t @handle" tail only, using their actual handle from the source, unless the instructions for this post say to end with a question to them instead.

TONE: straight. Report the author's claims neutrally (e.g. "he shares a case study of 14M+ views"). Never vouch, never editorialize doubt.

SOUND HUMAN: write like a real person typing fast, not like an AI. Do NOT use em dashes (—), en dashes, or " - " as connectors. If one would normally appear, use a comma for a continuing thought or a period to start a new sentence. Skip other AI tells too (the "it's not just X, it's Y" cadence, "delve", over-tidy symmetry).

HARD RULES (never break):
  - NEVER fabricate. Use ONLY facts/numbers that appear in the source post. Do not invent tool steps, metrics, or outcomes.
  - No links. No hashtags. Light emoji ok.
  - Avoid politics, NSFW, harassment, medical/legal/investment advice.
  - If the source has NO teachable material (a teaser, a cut-off retweet, a flex
    with no method), output exactly: SKIP: <reason in <=8 words>
    NEVER write prose about the source's shortcomings, NEVER address the author
    or reader, NEVER ask for more content. SKIP is the only valid refusal.

Return ONLY the post text (or the SKIP line) — no preamble, no quotes around it.
```

- [ ] **Step 5: Embedded fallback prompt + thread instructions + template generator in `generate.py`**

Replace the `return f"""You write the commentary ..."""` body of `build_system_prompt` with the SAME text as voice.md, with `<<STYLE>>` → `{v.style}` and `<<MAX_CHARS>>` → `{max_chars}` (f-string; the text contains no other braces). The existing golden test compares `.strip()`ed file vs embedded; keep leading/trailing whitespace consistent.

Replace `THREAD_INSTRUCTIONS`:

```python
THREAD_INSTRUCTIONS = """
This source is substantial, so you MAY write a SHORT THREAD instead of one post, ONLY if the one point you picked cannot be explained in a single post. Thread format:
  - {n_parts} parts MAX, separated by a line containing exactly: ---
  - Part 1 = the hook post (<= {hook_budget} chars INCLUDING the "h/t @{handle}" tail at its end)
  - Each later part = the rest of the SAME point, explained plainly (<= {part_budget} chars each, no h/t tail, no URLs, no hashtags, no bullets)
If one post is enough, write the normal single post instead."""
```

Replace `TemplateCommentaryGenerator.generate`:

```python
    def generate(self, post: Post, allow_thread: bool = False, arms=None) -> Draft:
        hook = self._hook(post.text)
        steps = self._bullets(post.text)
        takeaway = self._takeaway(post.text)
        tail = f"\n\nh/t @{post.author_handle}" if self.credit == "subtle_tail" else ""
        # Plain-story shape: hook, then the steps as their own short lines
        # (no bullet markers), then the what-to-do line.
        def build(n):
            lines = [hook] + [f"{s[0].upper()}{s[1:]}." if s and not s.endswith(".") else s
                              for s in steps[:n]] + [takeaway]
            return "\n\n".join(lines)
        n = len(steps)
        body = build(n)
        while len(body) > self.max_chars and n > 0:
            n -= 1
            body = build(n)
        return Draft(tweet_id=post.tweet_id, commentary=(body + tail).strip(),
                     model="template")
```
Also change `_shorten` default `n` to `80` so a step line is one sentence, and check `_hook` outputs have no "•". (They don't.) The `arms=None` parameter is consumed in Task 3; add it now so the Protocol stays consistent.

Update the `CommentaryGenerator` Protocol:

```python
class CommentaryGenerator(Protocol):
    def generate(self, post: Post, allow_thread: bool = False, arms=None) -> Draft: ...
```

- [ ] **Step 6: QA system prompt**

Replace `QA_SYSTEM` in `qa.py`:

```python
QA_SYSTEM = """You are the final editor for an X account that teaches ONE growth lesson per post, in plain words any founder or creator can follow with no ads background: a tiny story (who did it, what they did, what they found, what to do). A draft may be a short thread (parts separated by blank lines) — judge it as a whole.

REJECT the draft if ANY of these hold:
1) META: it addresses the source author or reader instead of teaching — asks for more content ("drop the full thread"), says it can't write it, comments on the post itself, or reads like a reply/DM. A single closing question to the author is allowed ONLY when a note below says this draft may end with one.
2) NO_LESSON: a reader learns no concrete, applicable tactic or insight.
3) UNCLEAR: a smart person with no ads/growth background could not follow it — unexplained shorthand, a number whose meaning is not stated, or several compressed points instead of one explained point.
4) FORMAT: no line that says what to do, or one undifferentiated blob.

Judge the DRAFT only — assume the source post was already vetted. Be permissive about style; reject only real failures.

Return ONLY JSON: {"ok": true|false, "issue": "<reason, <=12 words>"}"""
```

- [ ] **Step 7: config.yaml**

In `config.yaml`: set `voice.format: plain_story    # one point per post, explained in plain words (spec 2026-10-08 §6)`, add `voice.readable_rules: true   # deterministic no-bullets/no-jargon/short-lines gate (safety.check_readability)`, and set `llm.max_commentary_chars: 262` with the comment:

```yaml
  # 262 = 280 minus the longest "h/t @handle" tail. Length follows clarity:
  # ONE point explained fully (spec 2026-10-08 §6) needs the room. smart_trim
  # still guarantees <= 280 after composition.
  max_commentary_chars: 262
```

- [ ] **Step 8: Run the full suite**

Run: `.venv/bin/pytest -q`
Expected: all pass. If `test_autonomy_phase12.py` line ~108 (file vs embedded prompt) fails, diff the two strings and fix whitespace until byte-identical.

- [ ] **Step 9: Commit**

```bash
git add agent/voice.md src/xbot/commentary/generate.py src/xbot/commentary/safety.py src/xbot/commentary/qa.py config.yaml tests/test_safety.py tests/test_readable_voice.py
git commit -m "Voice: one point per post in plain words; readability gate; 262-char budget"
```

---

### Task 3: Author-bait arm end to end (prompt, credit rule, budget, arm-aware QA, orchestrator wiring)

**Files:**
- Modify: `src/xbot/commentary/generate.py` (`_user_prompt`, `OpenAICompatGenerator.generate/revise/_call`, `AnthropicGenerator.generate`)
- Modify: `src/xbot/publish/publisher.py` (`body_budget`, `compose_text`)
- Modify: `src/xbot/commentary/safety.py` (`check_commentary` signature)
- Modify: `src/xbot/commentary/qa.py` (`qa_commentary` signature)
- Modify: `src/xbot/orchestrator.py` (`make_drafts` loop ~L385-410, `_vet_commentary`, `_publish_due` re-vet, `_publish`/`_log_features`)
- Test: `tests/test_author_bait.py`

**Interfaces:**
- Consumes: `experiments.assign_arms`, `experiments.arm`, `Draft.arms` (Task 1).
- Produces: `body_budget(post, cfg, arms=None)`, `check_commentary(post, commentary, cfg, parts=None, arms=None)`, `qa_commentary(post, commentary, cfg, fail_open=True, arms=None)`, `_user_prompt(post, cfg=None, allow_thread=False, arms=None)`, generators' `generate(post, allow_thread=False, arms=None)` and `revise(post, previous, feedback, arms=None)`, `Orchestrator._log_features(draft, post, our_id, arms=None)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_author_bait.py
"""author_bait experiment: 'question' arm ends with one question to the author
instead of the h/t tail (spec §3)."""
from xbot.commentary import generate as g
from xbot.commentary.safety import check_commentary
from xbot.config import NS
from xbot.models import Draft, Metrics, Post, Score, utcnow
from xbot.orchestrator import Orchestrator
from xbot.publish.publisher import body_budget, compose_text
from xbot.storage.sqlite_repo import SqliteRepository

CFG = NS({"posting": {"format": "mention", "per_day": 3, "per_run": 1},
          "llm": {"max_commentary_chars": 262},
          "safety": {"exclude": []},
          "voice": {"style": "growth_first"},
          "mode": {"autonomous": True},
          "ranking": {"qa_gate": False},
          "experiments": {"author_bait": {"enabled": True,
                                          "arms": ["tail", "question"]}}})
Q = {"author_bait": "question"}
T = {"author_bait": "tail"}


def _post(tid="1", handle="adriamatz"):
    # distinct text + handle per tid so the publish-time near-duplicate and
    # author-cooldown checks don't collapse two test posts into one
    return Post(tweet_id=tid, author_handle=handle if tid == "1" else f"{handle}{tid}",
                author_name="A",
                text=f"same ad on 2 platforms, one cost 4x more per trial, case {tid}",
                created_at=utcnow(), author_follower_count=1000,
                metrics=Metrics(likes=3))


def test_user_prompt_asks_for_question_in_question_arm():
    p = g._user_prompt(_post(), CFG, arms=Q)
    assert "ONE short, specific question to @adriamatz" in p
    assert "End with: h/t" not in p
    p = g._user_prompt(_post(), CFG, arms=T)
    assert "End with: h/t @adriamatz" in p


def test_question_arm_gets_the_bigger_budget():
    assert body_budget(_post(), CFG, arms=Q) == 278
    assert body_budget(_post(), CFG, arms=T) == 280 - (len("h/t @adriamatz") + 4)
    assert body_budget(_post(), CFG) == body_budget(_post(), CFG, arms=T)


def test_compose_keeps_question_and_adds_no_tail():
    d = Draft(tweet_id="1", commentary="One ad, 2 platforms.\n\nDid the gap hold, @adriamatz?",
              model="t", arms=Q)
    text, _ = compose_text(d, _post(), CFG)
    assert text.endswith("Did the gap hold, @adriamatz?")
    assert "h/t" not in text


def test_compose_still_appends_tail_when_handle_missing():
    d = Draft(tweet_id="1", commentary="One ad, 2 platforms.", model="t", arms=T)
    text, _ = compose_text(d, _post(), CFG)
    assert text.endswith("h/t @adriamatz")


def test_compose_credit_check_is_regex_safe():
    post = _post(handle="a_b.c")
    d = Draft(tweet_id="1", commentary="One ad.\n\nTrue, @a_b.c?", model="t", arms=Q)
    text, _ = compose_text(d, post, CFG)
    assert "h/t" not in text
    d = Draft(tweet_id="1", commentary="One ad.\n\nTrue, @a_bxc?", model="t", arms=Q)
    text, _ = compose_text(d, post, CFG)
    assert text.endswith("h/t @a_b.c")       # '.' must not act as a wildcard


def test_check_commentary_uses_arm_budget():
    body = "x" * 270
    ok, why = check_commentary(_post(), body, CFG, arms=Q)
    assert ok, why
    ok, why = check_commentary(_post(), body, CFG, arms=T)
    assert not ok and why.startswith("too_long")


class _Gen:
    """Records the arms it was asked to write for."""
    def __init__(self):
        self.calls = []

    def generate(self, post, allow_thread=False, arms=None):
        self.calls.append(dict(arms or {}))
        end = f"Did it hold, @{post.author_handle}?" if (arms or {}).get("author_bait") == "question" \
            else f"h/t @{post.author_handle}"
        return Draft(tweet_id=post.tweet_id, model="fake",
                     commentary=f"One ad, 2 platforms. One cost 4x more per trial.\n\n{end}")


class _Pub:
    def __init__(self):
        self.texts = []

    def publish(self, draft, post):
        self.texts.append(draft.commentary)
        return {"ok": True, "id": f"our_{post.tweet_id}"}


def _orch(repo):
    o = object.__new__(Orchestrator)
    o.cfg, o.repo = CFG, repo
    o.generator, o.prescreen, o.judge = _Gen(), None, None
    o.publisher, o.judge_reasons = _Pub(), {}
    return o


def test_draft_and_publish_carry_arms_to_features():
    repo = SqliteRepository(":memory:"); repo.init_schema()
    o = _orch(repo)
    for tid in ("1", "2"):
        p = _post(tid); repo.upsert_post(p)
        repo.save_score(Score(tweet_id=tid, topic_fit=0.9, quote_worthy=0.8,
                              quote_score=0.8, judged=True))
    created = o.make_drafts(limit=2)
    assert [c["ok"] for c in created] == [True, True]
    assert [c["draft"].arms["author_bait"] for c in created] == ["tail", "question"]
    assert o.publish_due()["status"] == "posted"
    assert o.publish_due()["status"] == "posted"
    rows = repo.conn.execute(
        "SELECT arms FROM post_features ORDER BY our_tweet_id").fetchall()
    assert sorted(r["arms"] for r in rows) == sorted(
        ['{"author_bait": "tail"}', '{"author_bait": "question"}'])
```

(`Orchestrator.make_drafts(self, limit: int | None = None) -> list[dict]` is at `orchestrator.py:334`; each returned dict has keys `draft_id, draft, post, score, ok, notes`.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_author_bait.py -v`
Expected: FAIL — `TypeError: _user_prompt() got an unexpected keyword argument 'arms'` etc.

- [ ] **Step 3: `body_budget` + `compose_text` in `publisher.py`**

```python
def body_budget(post: Post, cfg, arms: dict | None = None) -> int:
    """Max commentary-body chars (h/t tail excluded) for this post in the active
    format. Mention mode keeps the tail inside the 280; link mode also loses the
    URL (23 chars on X). The author_bait 'question' arm carries the @handle in
    its closing question instead of a tail, so it gets the whole 280."""
    if is_web_source(post):
        return 280 - 2                       # original teaching post, no h/t tail
    from ..experiments import arm  # lazy: avoid import cycle
    if arm(arms, "author_bait") == "question":
        return 280 - 2
    fmt = posting_format(cfg)
    if fmt == "link":
        return 280 - (len(f"h/t @{post.author_handle}: ") + 23) - 2
    if fmt == "mention":
        return 280 - (len(f"h/t @{post.author_handle}") + 4)  # tail + separator slack
    return 280
```

In `compose_text` mention branch, replace the tail line:

```python
        # Credit rule: a tail "h/t @handle" OR an in-body @handle (the
        # author_bait 'question' arm) both count. Append the tail only when
        # the handle appears nowhere.
        mention = re.compile(rf"@{re.escape(post.author_handle)}(?!\w)", re.IGNORECASE)
        if not _HT_TAIL.search(text) and not mention.search(text):
            text = f"{text}\n\nh/t @{post.author_handle}"
```

- [ ] **Step 4: `check_commentary` + `qa_commentary` take `arms`**

`safety.py`:
```python
def check_commentary(post: Post, commentary: str, cfg: NS,
                     parts: list[str] | None = None,
                     arms: dict | None = None) -> tuple[bool, str]:
```
and `budget = body_budget(post, cfg, arms)`.

`qa.py`:
```python
def qa_commentary(post: Post, commentary: str, cfg: NS,
                  fail_open: bool = True, arms: dict | None = None) -> tuple[bool, str]:
    if not cfg.get("ranking.qa_gate", True):
        return True, ""
    from ..experiments import arm  # lazy
    note = ""
    if arm(arms, "author_bait") == "question":
        note = ("\n\nNOTE: this draft MAY end with ONE question addressed to the "
                "source author. That alone is not META. Still reject it if the "
                "lesson is missing or unclear.")
    return _qa_call(QA_SYSTEM, (
        f"Source post (already vetted):\n\"\"\"\n{post.text[:600]}\n\"\"\"\n\n"
        f"Draft to check:\n\"\"\"\n{commentary}\n\"\"\"{note}"), cfg, fail_open, "draft")
```

- [ ] **Step 5: `_user_prompt` + generators in `generate.py`**

```python
def _user_prompt(post: Post, cfg: NS = None, allow_thread: bool = False,
                 arms: dict | None = None) -> str:
    from ..experiments import arm  # lazy: avoid cycle
    question_arm = arm(arms, "author_bait") == "question" and not is_web_source(post)
    extra = ""
    if cfg is not None:
        from ..publish.publisher import body_budget, part_budget  # lazy: avoid cycle
        hook_budget = body_budget(post, cfg, arms)
        target = min(int(cfg.get("llm.max_commentary_chars", 240)), hook_budget)
        what = "in total" if question_arm else "before the h/t tail"
        extra = (f"\nHARD LIMIT for this post: {hook_budget} characters {what} — "
                 f"aim for {target}. If in doubt, cut a sentence, never the explanation.")
        if allow_thread:
            extra += THREAD_INSTRUCTIONS.format(
                n_parts=int(cfg.get("posting.max_thread_parts", 3)),
                hook_budget=hook_budget,
                part_budget=part_budget(cfg),
                handle=post.author_handle)
    if is_web_source(post):
        return (f"Source article ({post.author_name}):\n"
                f'"""\n{post.text}\n"""\n\n'
                f"Write the post now as a COMPLETE teaching post: one point, told as "
                f"a tiny story, ending with a clear what-to-do line on its OWN final "
                f"line. Teach it in your own words (the source is a brief, not "
                f"something to compress further). Do NOT add an h/t or @mention.{extra}")
    if question_arm:
        ending = (f"End with ONE short, specific question to @{post.author_handle} "
                  f"about a concrete claim in their post. The question is the last "
                  f"line and REPLACES the h/t tail (do not write 'h/t').")
    else:
        ending = f"End with: h/t @{post.author_handle}"
    return (f"Source post by @{post.author_handle} ({post.author_name}):\n"
            f'"""\n{post.text}\n"""\n\n'
            f"Write the post now. {ending}{extra}")
```

`OpenAICompatGenerator`:
```python
    def generate(self, post: Post, allow_thread: bool = False, arms=None) -> Draft:
        return self._call(post, allow_thread=allow_thread, messages=[
            {"role": "system", "content": self.system},
            {"role": "user", "content": _user_prompt(post, self.cfg, allow_thread, arms)},
        ])

    def revise(self, post: Post, previous: str, feedback: str, arms=None) -> Draft:
        return self._call(post, messages=[
            {"role": "system", "content": self.system},
            {"role": "user", "content": _user_prompt(post, self.cfg, arms=arms)},
            {"role": "assistant", "content": previous},
            {"role": "user", "content": (
                f"Editor rejected that draft: {feedback}\n"
                "Rewrite it as ONE single post fixing ONLY that problem. Keep every "
                "other rule (voice, one explained point, the ending you were asked "
                "for, no fabrication). Return only the post text.")},
        ])
```
`AnthropicGenerator.generate(self, post, allow_thread=False, arms=None)` → pass `arms` to `_user_prompt` the same way.

- [ ] **Step 6: Orchestrator wiring**

In the draft loop (`make_drafts`), replace the generate call:

```python
            from .experiments import assign_arms  # lazy: keeps module import light
            arms = assign_arms(self.cfg, self.repo, post)
            draft = self.generator.generate(post, allow_thread=allow_thread, arms=arms)
            draft.arms = arms
```
(also set `draft.arms = arms` on the SKIP-sentinel path's blocked draft, after `draft.safety_passed = False`, so blocked rows keep their assignment for debugging; they don't count for alternation).

In `_vet_commentary`:
```python
            ok, notes = check_commentary(post, draft.commentary, self.cfg,
                                         parts=draft.parts, arms=draft.arms)
            if ok:
                qa_ok, qa_issue = qa_commentary(post, draft.full_text, self.cfg,
                                                arms=draft.arms)
```
and the revise call: `draft = revise(post, draft.full_text, self._revision_feedback(post, notes), arms=draft.arms)` followed by `draft.arms = arms_before` where `arms_before = draft.arms` is captured before the call (the generator returns a fresh Draft). The trim path: `smart_trim(draft.commentary, body_budget(post, self.cfg, draft.arms))` and the re-check passes `arms=draft.arms`.

In `_publish_due` re-vet:
```python
            ok, revet_notes = check_commentary(post, draft.commentary, self.cfg,
                                               parts=draft.parts, arms=draft.arms)
            if ok:
                ok, revet_notes = qa_commentary(post, draft.full_text, self.cfg,
                                                fail_open=False, arms=draft.arms)
```

In `_publish` / `_log_features`:
```python
        try:
            self._log_features(draft, post, our_id, arms=dict(draft.arms or {}))
```
```python
    def _log_features(self, draft: Draft, post: Post, our_id: str,
                      arms: dict | None = None) -> None:
```
and add `"arms": arms or {},` to the dict passed to `log_features`.

- [ ] **Step 7: Run tests**

Run: `.venv/bin/pytest tests/test_author_bait.py tests/test_publish_due.py tests/test_outcomes.py tests/test_safety.py -v`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/xbot/commentary/generate.py src/xbot/commentary/safety.py src/xbot/commentary/qa.py src/xbot/publish/publisher.py src/xbot/orchestrator.py tests/test_author_bait.py
git commit -m "author_bait experiment: question arm, in-body credit, arm-aware budget + QA"
```

---

### Task 4: Image card arm (renderer, upload with text fallback, dry-run PNG, actual-arm recording)

**Files:**
- Create: `src/xbot/publish/card.py`
- Modify: `src/xbot/publish/api_publisher.py` (`publish`, new `_card_media_id`, `_upload_png`)
- Modify: `src/xbot/publish/dryrun.py` (`publish`)
- Modify: `src/xbot/orchestrator.py` (`_publish`: record the arm actually posted)
- Modify: `pyproject.toml` (`media = ["Pillow>=10.1"]`), `.github/workflows/publish.yml:32` (`pip install -e ".[x,llm,turso,media]"`)
- Modify: `config.yaml` (`posting.card_handle`)
- Test: `tests/test_image_card.py`

**Interfaces:**
- Produces: `card.card_lines(commentary) -> tuple[str, list[str]]`, `card.render_card(commentary, handle) -> bytes` (PNG; raises `ImportError` without Pillow), `card.W, card.H = 1200, 675`.
- `Publisher.publish` result gains `"media": bool` (True only when an image was actually attached).
- `ApiPublisher._upload_png(session, png: bytes) -> str` (media id or raises).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_image_card.py
"""image_card experiment: self-rendered PNG attached to the main post, with a
plain-text fallback that is RECORDED as the text arm (spec §2)."""
import io

import pytest

from xbot.config import NS
from xbot.models import Draft, Metrics, Post, Score, utcnow
from xbot.orchestrator import Orchestrator
from xbot.publish.card import H, W, card_lines, render_card
from xbot.storage.sqlite_repo import SqliteRepository

pytest.importorskip("PIL")

BODY = ("A founder ran one ad on two platforms.\n\n"
        "He divided what he spent by the trials it brought in.\n\n"
        "Measure this before you spend more.\n\nh/t @adriamatz")


def test_card_lines_drop_tail_and_handle_lines():
    hook, rest = card_lines(BODY)
    assert hook == "A founder ran one ad on two platforms."
    assert rest == ["He divided what he spent by the trials it brought in.",
                    "Measure this before you spend more."]
    hook, rest = card_lines("Hook.\n\nDid it hold, @adriamatz?")
    assert rest == []


def _png_size(png: bytes):
    from PIL import Image
    return Image.open(io.BytesIO(png)).size


def test_render_card_is_1200x675_png():
    png = render_card(BODY, "polar")
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert _png_size(png) == (W, H)


def test_render_card_survives_empty_and_huge_text():
    assert _png_size(render_card("", "polar")) == (W, H)
    assert _png_size(render_card("word " * 400, "polar")) == (W, H)


CFG = NS({"posting": {"format": "mention", "per_day": 3, "per_run": 1,
                      "card_handle": "polar"},
          "llm": {"max_commentary_chars": 262}, "safety": {"exclude": []},
          "mode": {"autonomous": True}, "ranking": {"qa_gate": False},
          "experiments": {"image_card": {"enabled": True, "arms": ["text", "card"]}}})


class _Resp:
    def __init__(self, code, data):
        self.status_code, self._data, self.text = code, data, str(data)

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")


class _Session:
    """Fake OAuth1Session: records payloads; upload can be told to fail."""
    def __init__(self, upload_ok=True, media_post_ok=True):
        self.upload_ok, self.media_post_ok, self.calls = upload_ok, media_post_ok, []

    def post(self, url, json=None, files=None, data=None, timeout=None):
        self.calls.append({"url": url, "json": json, "files": files})
        if url.endswith("/media/upload"):
            return _Resp(201, {"data": {"id": "m1"}}) if self.upload_ok \
                else _Resp(500, {"error": "upload down"})
        if json and "media" in json and not self.media_post_ok:
            return _Resp(400, {"error": "bad media"})
        return _Resp(201, {"data": {"id": "t1"}})


def _api_publisher(session):
    from xbot.publish.api_publisher import ApiPublisher
    pub = object.__new__(ApiPublisher)
    pub.cfg = CFG
    pub._session = lambda: session
    return pub


def _post():
    return Post(tweet_id="1", author_handle="adriamatz", author_name="A",
                text="one ad two platforms", created_at=utcnow(),
                author_follower_count=10, metrics=Metrics())


def test_api_publisher_attaches_media_in_card_arm():
    s = _Session()
    res = _api_publisher(s).publish(Draft(tweet_id="1", commentary=BODY, model="t",
                                          arms={"image_card": "card"}), _post())
    assert res["media"] is True and res["id"] == "t1"
    tweet_call = [c for c in s.calls if c["url"].endswith("/tweets")][0]
    assert tweet_call["json"]["media"] == {"media_ids": ["m1"]}


def test_api_publisher_falls_back_to_text_when_upload_fails():
    s = _Session(upload_ok=False)
    res = _api_publisher(s).publish(Draft(tweet_id="1", commentary=BODY, model="t",
                                          arms={"image_card": "card"}), _post())
    assert res["media"] is False and res["id"] == "t1"
    tweet_call = [c for c in s.calls if c["url"].endswith("/tweets")][0]
    assert "media" not in tweet_call["json"]


def test_api_publisher_retries_without_media_when_post_rejects_it():
    s = _Session(media_post_ok=False)
    res = _api_publisher(s).publish(Draft(tweet_id="1", commentary=BODY, model="t",
                                          arms={"image_card": "card"}), _post())
    assert res["media"] is False and res["id"] == "t1"
    tweet_calls = [c for c in s.calls if c["url"].endswith("/tweets")]
    assert len(tweet_calls) == 2 and "media" not in tweet_calls[1]["json"]


def test_text_arm_never_uploads():
    s = _Session()
    _api_publisher(s).publish(Draft(tweet_id="1", commentary=BODY, model="t",
                                    arms={"image_card": "text"}), _post())
    assert not [c for c in s.calls if c["url"].endswith("/media/upload")]


class _Pub:
    def __init__(self, media):
        self.media = media

    def publish(self, draft, post):
        return {"ok": True, "id": "our_1", "media": self.media}


def test_features_record_the_arm_actually_posted():
    repo = SqliteRepository(":memory:"); repo.init_schema()
    p = _post(); repo.upsert_post(p)
    repo.save_score(Score(tweet_id="1", quote_score=0.8, judged=True))
    repo.add_draft(Draft(tweet_id="1", commentary=BODY, model="t", safety_passed=True,
                         arms={"image_card": "card"}))
    o = object.__new__(Orchestrator)
    o.cfg, o.repo, o.publisher, o.judge_reasons = CFG, repo, _Pub(media=False), {}
    assert o.publish_due()["status"] == "posted"
    row = repo.conn.execute("SELECT arms FROM post_features").fetchone()
    assert row["arms"] == '{"image_card": "text"}'


def test_dry_run_writes_png_for_card_arm(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from xbot.publish.dryrun import DryRunPublisher
    res = DryRunPublisher(CFG).publish(Draft(tweet_id="1", commentary=BODY, model="t",
                                             arms={"image_card": "card"}), _post())
    assert res["media"] is True
    assert list((tmp_path / "data" / "cards").glob("*.png"))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pip install "Pillow>=10.1"` (the venv needs it for the tests; CI gets it via the new extra), then `.venv/bin/pytest tests/test_image_card.py -v`
Expected: FAIL — `ModuleNotFoundError: xbot.publish.card`.

- [ ] **Step 3: Create `src/xbot/publish/card.py`**

```python
"""Image card for the image_card experiment (spec 2026-10-08 §2).

Draws the post's OWN text (hook large, the other lines below, our handle in
the footer) as a 1200x675 PNG. No source media, no generated imagery. Pillow
is an optional extra (`pip install -e ".[media]"`), imported lazily: without
it render_card raises ImportError and the publisher posts plain text.
"""
from __future__ import annotations

W, H = 1200, 675
_BG, _FG, _DIM = (16, 20, 24), (244, 246, 243), (154, 165, 173)
_PAD = 72


def card_lines(commentary: str) -> tuple[str, list[str]]:
    """(hook, other lines) from a commentary body. Drops the h/t tail and any
    line that addresses a handle (the author_bait question stays text-only)."""
    from .publisher import strip_ht_tail
    lines = [ln.strip() for ln in strip_ht_tail(commentary or "").splitlines()
             if ln.strip()]
    if not lines:
        return "", []
    return lines[0], [ln for ln in lines[1:] if "@" not in ln]


def _wrap(draw, text: str, font, max_w: int) -> list[str]:
    out, cur = [], ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        if draw.textlength(trial, font=font) <= max_w or not cur:
            cur = trial
        else:
            out.append(cur)
            cur = word
    if cur:
        out.append(cur)
    return out


def render_card(commentary: str, handle: str) -> bytes:
    """PNG bytes of the card. Raises ImportError when Pillow is missing."""
    import io
    from PIL import Image, ImageDraw, ImageFont  # lazy: optional extra

    hook, rest = card_lines(commentary)
    img = Image.new("RGB", (W, H), _BG)
    d = ImageDraw.Draw(img)
    big = ImageFont.load_default(size=56)
    small = ImageFont.load_default(size=34)
    foot = ImageFont.load_default(size=26)
    y, max_w, floor = _PAD, W - 2 * _PAD, H - 110
    for ln in _wrap(d, hook, big, max_w)[:3]:
        d.text((_PAD, y), ln, font=big, fill=_FG)
        y += 68
    y += 20
    for para in rest:
        for ln in _wrap(d, para, small, max_w):
            if y > floor:
                break
            d.text((_PAD, y), ln, font=small, fill=_FG)
            y += 44
        y += 16
    if handle:
        d.text((_PAD, H - 60), f"@{handle.lstrip('@')}", font=foot, fill=_DIM)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
```

- [ ] **Step 4: `ApiPublisher`: upload + attach + fallback**

Add near `API_BASE`:
```python
MEDIA_UPLOAD = "https://api.x.com/2/media/upload"
```

Add methods to `ApiPublisher`:

```python
    def _upload_png(self, session, png: bytes) -> str:
        """Simple (non-chunked) v2 media upload. Returns the media id."""
        resp = session.post(MEDIA_UPLOAD,
                            files={"media": ("card.png", png, "image/png")},
                            data={"media_category": "tweet_image",
                                  "media_type": "image/png"},
                            timeout=60)
        _raise_if_account_error(resp)
        resp.raise_for_status()
        data = resp.json().get("data", {}) or {}
        return str(data.get("id") or data.get("media_id_string") or "")

    def _card_media_id(self, session, draft: Draft) -> str:
        """Render + upload the card; '' on ANY non-account failure so the post
        still goes out as text (spec §2: never block publishing on media)."""
        from .card import render_card  # lazy
        try:
            png = render_card(draft.commentary,
                              str(self.cfg.get("posting.card_handle", "") if self.cfg else ""))
            return self._upload_png(session, png)
        except AccountError:
            raise
        except Exception as e:
            print(f"  [publish] card skipped ({type(e).__name__}: {str(e)[:100]}) — posting text")
            return ""
```

In `publish`, replace the `else: main_id = self._post(session, {"text": text}).get("id", "")` branch:

```python
        else:
            from ..experiments import arm  # lazy
            media_posted = False
            payload = {"text": text}
            if arm(draft.arms, "image_card") == "card":
                media_id = self._card_media_id(session, draft)
                if media_id:
                    payload["media"] = {"media_ids": [media_id]}
            try:
                main_id = self._post(session, payload).get("id", "")
                media_posted = "media" in payload
            except AccountError:
                raise
            except Exception as e:
                if "media" not in payload:
                    raise
                print(f"  [publish] media post rejected ({type(e).__name__}: "
                      f"{str(e)[:100]}) — retrying as text")
                main_id = self._post(session, {"text": text}).get("id", "")
```
Initialize `media_posted = False` before the `if fmt == "link":` so the link branch has it too, and add `"media": media_posted` to the final return dict.

- [ ] **Step 5: `DryRunPublisher.publish`**

After computing `text, fmt`, add:

```python
        from ..experiments import arm  # lazy
        media = False
        if arm(draft.arms, "image_card") == "card":
            try:
                from pathlib import Path
                from .card import render_card
                out = Path("data/cards"); out.mkdir(parents=True, exist_ok=True)
                path = out / f"{fake_id}.png"
                path.write_bytes(render_card(
                    draft.commentary, str(self.cfg.get("posting.card_handle", "") if self.cfg else "")))
                print(f"  card: {path}")
                media = True
            except Exception as e:
                print(f"  card skipped ({type(e).__name__}: {str(e)[:80]}) — text arm")
```
and add `"media": media` to the returned dict.

- [ ] **Step 6: Record the arm actually posted (`orchestrator._publish`)**

```python
    def _publish(self, draft_id: int, draft: Draft, post: Post) -> dict:
        result = self.publisher.publish(draft, post)
        our_id = result.get("id", "")
        ...
        arms = dict(draft.arms or {})
        if "image_card" in arms:  # record what went out, not what was planned
            arms["image_card"] = "card" if result.get("media") else "text"
        try:
            self._log_features(draft, post, our_id, arms=arms)
```

- [ ] **Step 7: Packaging + config**

`pyproject.toml` optional-dependencies: add `media = ["Pillow>=10.1"]   # image_card experiment renderer (lazy import)`.
`.github/workflows/publish.yml` line 32: `- run: pip install -e ".[x,llm,turso,media]"`.
`config.yaml` under `posting:`: `card_handle: ""   # OUR handle for the image-card footer (no @). Empty = no footer.`

- [ ] **Step 8: Run tests**

Run: `.venv/bin/pytest tests/test_image_card.py tests/test_publish_due.py -v`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add src/xbot/publish/card.py src/xbot/publish/api_publisher.py src/xbot/publish/dryrun.py src/xbot/orchestrator.py pyproject.toml .github/workflows/publish.yml config.yaml tests/test_image_card.py
git commit -m "image_card experiment: self-rendered PNG with text fallback"
```

---

### Task 5: Volume levers (threshold delta, per_run, informed fabricated-number revision)

**Files:**
- Modify: `src/xbot/experiments.py` (add `threshold_delta`, `per_run`, `max_vet_attempts`)
- Modify: `src/xbot/select/rules.py:16-19`
- Modify: `src/xbot/orchestrator.py` (`_publish_due` budget line, `_vet_commentary` loop, `_revision_feedback`)
- Test: `tests/test_volume.py`

**Interfaces:**
- Produces: `experiments.threshold_delta(cfg) -> float` (0.05 when on, else 0), `experiments.per_run(cfg) -> int`, `experiments.max_vet_attempts(cfg) -> int` (3 when on, else 2).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_volume.py
"""volume period test (spec §4): thresholds -0.05, per_run 2, one extra
fabricated-number revision that names the allowed numbers."""
from xbot.config import NS
from xbot.models import Draft, Metrics, Post, Score, utcnow
from xbot.orchestrator import Orchestrator
from xbot.select.rules import evaluate
from xbot.storage.sqlite_repo import SqliteRepository


def _cfg(on):
    return NS({"thresholds": {"topic_fit_min": 0.45, "quote_worthy_min": 0.35},
               "posting": {"per_day": 3, "per_run": 1, "format": "mention"},
               "safety": {"exclude": []}, "llm": {"max_commentary_chars": 262},
               "mode": {"autonomous": True}, "ranking": {"qa_gate": False},
               "experiments": {"volume": {"enabled": on, "period": True}}})


def _repo():
    r = SqliteRepository(":memory:"); r.init_schema(); return r


def _post(tid="1"):
    return Post(tweet_id=tid, author_handle=f"u{tid}", author_name="U",
                text=f"he spent 30 a day and got 4 times more trials {tid}",
                created_at=utcnow(), author_follower_count=100, metrics=Metrics())


def test_thresholds_unchanged_when_off_and_lowered_when_on():
    repo = _repo()
    s = Score(tweet_id="1", topic_fit=0.42, quote_worthy=0.32)
    ok, why = evaluate(_post(), s, _cfg(False), repo)
    assert not ok and why.startswith("low_topic_fit")
    ok, why = evaluate(_post(), s, _cfg(True), repo)
    assert ok, why


def _orch(cfg, repo, gen=None):
    o = object.__new__(Orchestrator)
    o.cfg, o.repo, o.judge_reasons = cfg, repo, {}
    o.generator, o.prescreen, o.judge = gen, None, None
    class _Pub:
        def publish(self, d, p): return {"ok": True, "id": f"our_{p.tweet_id}"}
    o.publisher = _Pub()
    return o


def _queue(repo, tid):
    repo.upsert_post(_post(tid))
    repo.save_score(Score(tweet_id=tid, quote_score=0.8, judged=True))
    repo.add_draft(Draft(tweet_id=tid, commentary="a sharp growth take worth stealing",
                         model="t", safety_passed=True))


def test_per_run_is_two_when_on():
    repo = _repo(); _queue(repo, "1"); _queue(repo, "2")
    assert _orch(_cfg(False), repo).publish_due()["count"] == 1
    repo = _repo(); _queue(repo, "1"); _queue(repo, "2")
    assert _orch(_cfg(True), repo).publish_due()["count"] == 2


class _Gen:
    """First draft fabricates '10'; first revision still does; second fixes it."""
    def __init__(self):
        self.feedback = []

    def generate(self, post, allow_thread=False, arms=None):
        return Draft(tweet_id=post.tweet_id, model="t",
                     commentary="He spent 30 a day and got 10 times more trials.")

    def revise(self, post, previous, feedback, arms=None):
        self.feedback.append(feedback)
        n = len(self.feedback)
        text = ("He spent 30 a day and got 10 times more trials." if n == 1
                else "He spent 30 a day and got 4 times more trials.")
        return Draft(tweet_id=post.tweet_id, model="t", commentary=text)


def test_second_revision_only_when_volume_on_and_feedback_names_numbers():
    gen = _Gen()
    o = _orch(_cfg(True), _repo(), gen)
    draft, ok, notes = o._vet_commentary(_post(), gen.generate(_post()))
    assert ok, notes
    assert "30" in gen.feedback[0] and "4" in gen.feedback[0]   # allowed numbers listed
    assert len(gen.feedback) == 2
    gen = _Gen()
    o = _orch(_cfg(False), _repo(), gen)
    _, ok, notes = o._vet_commentary(_post(), gen.generate(_post()))
    assert not ok and notes.startswith("fabricated_number:10")
    assert len(gen.feedback) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_volume.py -v`
Expected: FAIL on all three (thresholds not lowered; count == 1; only one revision).

- [ ] **Step 3: Levers in `experiments.py`**

Append:

```python
# ---- volume period test (spec §4): three levers, graded as one ----

def threshold_delta(cfg) -> float:
    """Subtracted from thresholds.topic_fit_min / quote_worthy_min while on."""
    return float(cfg.get("experiments.volume.threshold_delta", 0.05)) if volume_on(cfg) else 0.0


def per_run(cfg) -> int:
    """Drafts a publish window may post: posting.per_run, or the volume override."""
    if volume_on(cfg):
        return int(cfg.get("experiments.volume.per_run", 2))
    return int(cfg.get("posting.per_run", 1))


def max_vet_attempts(cfg) -> int:
    """Generate + revisions the vet loop may spend on one draft (2 today)."""
    return 3 if volume_on(cfg) else 2
```

- [ ] **Step 4: `select/rules.py`**

```python
from ..experiments import threshold_delta
...
    delta = threshold_delta(cfg)
    if score.topic_fit < cfg.get("thresholds.topic_fit_min", 0.55) - delta:
        return False, f"low_topic_fit:{score.topic_fit:.2f}"
    if score.quote_worthy < cfg.get("thresholds.quote_worthy_min", 0.55) - delta:
        return False, f"low_quote_worthy:{score.quote_worthy:.2f}"
```

- [ ] **Step 5: Orchestrator**

`_publish_due`: replace `budget = min(remaining, self.cfg.get("posting.per_run", 1))` with
```python
        from .experiments import per_run  # lazy
        budget = min(remaining, per_run(self.cfg))
```

`_vet_commentary`: replace `for attempt in (1, 2):` / `if attempt == 2: break` with
```python
        from .experiments import max_vet_attempts  # lazy
        attempts = max_vet_attempts(self.cfg)
        for attempt in range(1, attempts + 1):
            ...
            if attempt == attempts:
                break
```
(the body of the loop is unchanged apart from the `arms` plumbing from Task 3).

`_revision_feedback`, fabricated branch:
```python
        if notes.startswith("fabricated_number"):
            from .commentary.safety import _DIGITS
            allowed = sorted(set(_DIGITS.findall(post.text or "")), key=int)
            bad = notes.split(":", 1)[-1]
            listed = ", ".join(allowed) if allowed else "(none — the source has no numbers)"
            return (f"You used the number {bad}, which is not in the source post. "
                    f"The ONLY numbers you may write as digits are: {listed}. Remove "
                    f"every other digit (say it in words without a figure, or drop it).")
```

- [ ] **Step 6: Run tests**

Run: `.venv/bin/pytest tests/test_volume.py tests/test_publish_due.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/xbot/experiments.py src/xbot/select/rules.py src/xbot/orchestrator.py tests/test_volume.py
git commit -m "volume experiment: threshold delta, per_run 2, informed number revision"
```

---

### Task 6: Grading — report math, `xbot experiments`, briefing section, strategist rule

**Files:**
- Modify: `src/xbot/experiments.py` (report + verdict)
- Modify: `src/xbot/storage/sqlite_repo.py` (`experiment_rows`)
- Modify: `src/xbot/briefing.py` (arms in per-post tag + "## Experiments" section)
- Modify: `src/xbot/cli.py` (`cmd_experiments`, parser)
- Modify: `agent/prompts/strategist.md` (grading rule)
- Test: `tests/test_experiment_report.py`

**Interfaces:**
- Produces: `repo.experiment_rows(within_days=35) -> list[dict]` with keys `our_tweet_id, posted_at, arms (dict), views, likes, reposts, replies, quotes` (outcome keys None when no 24h row).
- Produces: `experiments.summarize(rows, cfg, today: date) -> list[dict]`, `experiments.verdict(control, other, days_running) -> str`, `experiments.render(report) -> str`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_experiment_report.py
"""Grading (spec §5): per-arm stats, the winner rule, period comparison."""
from datetime import date, timedelta

from xbot.config import NS
from xbot.experiments import render, summarize, verdict
from xbot.models import Metrics
from xbot.storage.sqlite_repo import SqliteRepository


def test_verdict_rule():
    c = {"n": 10, "median_views": 10.0}
    assert verdict(c, {"n": 10, "median_views": 13.0}, 5) == "winner: b"
    assert verdict(c, {"n": 10, "median_views": 12.0}, 5) == "no difference"
    assert verdict(c, {"n": 10, "median_views": 6.0}, 5) == "winner: control"
    assert verdict(c, {"n": 3, "median_views": 30.0}, 5) == "continue"
    assert verdict(c, {"n": 3, "median_views": 30.0}, 21) == "winner: b"
    assert verdict({"n": 0, "median_views": 0.0}, {"n": 0, "median_views": 0.0}, 30) == "no data"


def _rows():
    base = date(2026, 10, 9)
    out = []
    for i in range(8):
        arm = "card" if i % 2 else "text"
        out.append({"our_tweet_id": f"o{i}", "posted_at": f"{base + timedelta(days=i)}T18:00:00",
                    "arms": {"image_card": arm}, "views": 20 if arm == "card" else 10,
                    "likes": 1, "reposts": 0, "replies": 0, "quotes": 0})
    # before the experiment started: no arms, lower views
    for i in range(1, 8):
        out.append({"our_tweet_id": f"p{i}", "posted_at": f"{base - timedelta(days=i)}T18:00:00",
                    "arms": {}, "views": 5, "likes": 0, "reposts": 0, "replies": 0, "quotes": 0})
    return out


CFG = NS({"experiments": {
    "image_card": {"enabled": True, "arms": ["text", "card"], "started": "2026-10-09"},
    "author_bait": {"enabled": False, "arms": ["tail", "question"], "started": "2026-10-09"},
    "volume": {"enabled": True, "period": True, "started": "2026-10-09"}}})


def test_summarize_per_post_and_period():
    rep = summarize(_rows(), CFG, today=date(2026, 10, 17))
    img = next(r for r in rep if r["name"] == "image_card")
    assert img["arms"]["text"]["n"] == 4 and img["arms"]["card"]["n"] == 4
    assert img["arms"]["card"]["median_views"] == 20
    assert img["verdict"] == "continue"            # n < 10 and < 21 days
    assert img["days"] == 8
    vol = next(r for r in rep if r["name"] == "volume")
    assert vol["period"] is True
    assert vol["on"]["posts_per_day"] == 1.0 and vol["before"]["posts_per_day"] == 0.5
    assert vol["on"]["median_views"] == 15 and vol["before"]["median_views"] == 5
    assert "author_bait" not in [r["name"] for r in rep]
    text = render(rep)
    assert "image_card" in text and "card" in text and "volume" in text


def test_repo_experiment_rows_joins_features_and_24h_outcomes():
    repo = SqliteRepository(":memory:"); repo.init_schema()
    repo.log_posted("src1", "our_1", "alice", "src text", "our text")
    repo.log_features({"our_tweet_id": "our_1", "arms": {"image_card": "card"}})
    repo.log_outcome("our_1", "1h", Metrics(views=3))
    repo.log_outcome("our_1", "24h", Metrics(views=9, likes=1))
    repo.log_posted("src2", "our_2", "bob", "src text 2", "our text 2")
    rows = repo.experiment_rows(within_days=35)
    by = {r["our_tweet_id"]: r for r in rows}
    assert by["our_1"]["arms"] == {"image_card": "card"} and by["our_1"]["views"] == 9
    assert by["our_2"]["arms"] == {} and by["our_2"]["views"] is None
```

(`repo.log_posted` signature: `log_posted(source_tweet_id, our_tweet_id, author_handle, source_text, commentary)` per `sqlite_repo.py:422` — confirm the parameter order there before running.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_experiment_report.py -v`
Expected: FAIL — `ImportError: cannot import name 'summarize'`.

- [ ] **Step 3: `repo.experiment_rows`**

Add to `SqliteRepository` after `log_features`:

```python
    def experiment_rows(self, within_days: int = 35) -> list[dict]:
        """Every post we published in the window, with its arms (from
        post_features) and its 24h outcome (None when not captured yet).
        The grading substrate for `xbot experiments` and the briefing."""
        cutoff = (utcnow() - timedelta(days=within_days)).isoformat()
        rows = self.conn.execute(
            """SELECT pl.our_tweet_id, pl.posted_at, f.arms,
                      o.views, o.likes, o.reposts, o.replies, o.quotes
               FROM posted_log pl
               LEFT JOIN post_features f ON f.our_tweet_id = pl.our_tweet_id
               LEFT JOIN post_outcomes o ON o.our_tweet_id = pl.our_tweet_id
                    AND o.milestone = '24h'
               WHERE pl.posted_at >= ? AND pl.our_tweet_id != ''
                 AND pl.our_tweet_id IS NOT NULL
               ORDER BY pl.posted_at ASC""", (cutoff,)).fetchall()
        out = []
        for r in rows:
            try:
                arms = json.loads(r["arms"]) if r["arms"] else {}
            except (ValueError, TypeError):
                arms = {}
            out.append({"our_tweet_id": r["our_tweet_id"], "posted_at": r["posted_at"],
                        "arms": arms if isinstance(arms, dict) else {},
                        "views": r["views"], "likes": r["likes"], "reposts": r["reposts"],
                        "replies": r["replies"], "quotes": r["quotes"]})
        return out
```
(`timedelta` is already imported in this module — check the imports at the top; add `from datetime import timedelta` if not.)

- [ ] **Step 4: Report math in `experiments.py`**

Append:

```python
# ---- grading (spec §5) ----

MIN_N_PER_ARM = 10
MAX_DAYS = 21
MIN_LIFT = 0.30


def _median(xs: list[float]) -> float:
    xs = sorted(xs)
    if not xs:
        return 0.0
    m = len(xs) // 2
    return float(xs[m]) if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2.0


def _stats(rows: list[dict]) -> dict:
    """Stats over posts that HAVE a 24h outcome; n counts only those."""
    done = [r for r in rows if r.get("views") is not None]
    views = [float(r["views"] or 0) for r in done]
    eng = [float((r.get("likes") or 0) + (r.get("reposts") or 0)
                 + (r.get("replies") or 0) + (r.get("quotes") or 0)) for r in done]
    return {"n": len(done), "posted": len(rows),
            "median_views": _median(views),
            "mean_views": round(sum(views) / len(views), 1) if views else 0.0,
            "mean_eng": round(sum(eng) / len(eng), 2) if eng else 0.0}


def verdict(control: dict, other: dict, days_running: int) -> str:
    """Spec rule: decide at >=10 posts per arm OR >=21 days; a |lift| >= 30%
    in median 24h views names a winner, else 'no difference'."""
    if control["n"] == 0 and other["n"] == 0:
        return "no data"
    enough = (control["n"] >= MIN_N_PER_ARM and other["n"] >= MIN_N_PER_ARM) \
        or days_running >= MAX_DAYS
    if not enough:
        return "continue"
    base = max(control["median_views"], 1.0)
    lift = (other["median_views"] - control["median_views"]) / base
    if lift >= MIN_LIFT:
        return "winner: b"
    if lift <= -MIN_LIFT:
        return "winner: control"
    return "no difference"


def _day(iso: str):
    from datetime import date
    return date.fromisoformat(str(iso)[:10])


def summarize(rows: list[dict], cfg, today=None) -> list[dict]:
    """One dict per ENABLED test. Per-post tests: per-arm stats + verdict.
    Period tests: the on-period vs the 14 days before `started`."""
    from datetime import date, timedelta
    today = today or date.today()
    report = []
    for name in PER_POST:
        if not enabled(cfg, name):
            continue
        arms = arms_of(cfg, name)
        start = started(cfg, name)
        days = (today - _day(start)).days if start else 0
        by_arm = {a: _stats([r for r in rows if r["arms"].get(name) == a]) for a in arms}
        v = verdict(by_arm[arms[0]], by_arm[arms[1]], days) if len(arms) > 1 else "n/a"
        if v == "winner: b":
            v = f"winner: {arms[1]}"
        elif v == "winner: control":
            v = f"winner: {arms[0]}"
        report.append({"name": name, "period": False, "started": start,
                       "days": days, "arms": by_arm, "verdict": v})
    if enabled(cfg, "volume"):
        start = started(cfg, "volume")
        s = _day(start) if start else today
        before_from = s - timedelta(days=14)
        on = [r for r in rows if _day(r["posted_at"]) >= s]
        before = [r for r in rows if before_from <= _day(r["posted_at"]) < s]
        days_on = max((today - s).days, 1)
        report.append({
            "name": "volume", "period": True, "started": start, "days": days_on,
            "on": {**_stats(on), "posts_per_day": round(len(on) / days_on, 2)},
            "before": {**_stats(before), "posts_per_day": round(len(before) / 14, 2)},
        })
    return report


def render(report: list[dict]) -> str:
    """Markdown table(s) for the CLI and the briefing pack."""
    if not report:
        return "(no experiments enabled)"
    lines = []
    for t in report:
        if t["period"]:
            lines += [f"### {t['name']} (period, started {t['started']}, day {t['days']})", "",
                      "| window | posts | posts/day | n w/ 24h | median 24h views | mean eng |",
                      "|---|---|---|---|---|---|"]
            for label in ("before", "on"):
                s = t[label]
                lines.append(f"| {label} | {s['posted']} | {s['posts_per_day']} | {s['n']} "
                             f"| {s['median_views']} | {s['mean_eng']} |")
        else:
            lines += [f"### {t['name']} (started {t['started']}, day {t['days']}) — "
                      f"verdict: {t['verdict']}", "",
                      "| arm | posted | n w/ 24h | median 24h views | mean views | mean eng |",
                      "|---|---|---|---|---|---|"]
            for a, s in t["arms"].items():
                lines.append(f"| {a} | {s['posted']} | {s['n']} | {s['median_views']} "
                             f"| {s['mean_views']} | {s['mean_eng']} |")
        lines.append("")
    return "\n".join(lines).rstrip()
```

- [ ] **Step 5: CLI + briefing + strategist prompt**

`cli.py`: add after `cmd_briefing`:

```python
def cmd_experiments(args):
    """Grade the enabled growth experiments from harvested outcomes (spec
    docs/superpowers/specs/2026-10-08-growth-experiments-design.md §5)."""
    orch = _setup_light(args)
    from .experiments import render, summarize
    report = summarize(orch.repo.experiment_rows(), orch.cfg)
    if args.json:
        import json
        print(json.dumps(report, indent=2, default=str))
    else:
        print(render(report))
    return 0
```
and in `main`:
```python
    p_exp = sub.add_parser("experiments")
    p_exp.add_argument("--json", action="store_true", help="machine-readable report")
    p_exp.set_defaults(func=cmd_experiments)
```
Add `xbot experiments   # grade the growth experiments (per-arm 24h views, verdict)` to the module docstring list.

`briefing.py`: in the per-post `tag`, append `f" arms={f.get('arms') or '{}'}"`; and before the "## Daily run totals" section add:

```python
    from .experiments import render, summarize
    lines += ["## Experiments (spec 2026-10-08; rule: >=10 posts/arm or 21 days, "
              "|median 24h-view lift| >= 30% names a winner)", "",
              render(summarize(repo.experiment_rows(), cfg)), ""]
```

`agent/prompts/strategist.md`: add after the "Session shape" list:

```
Experiments (docs/superpowers/specs/2026-10-08-growth-experiments-design.md):
the briefing's "## Experiments" section grades each enabled test. Apply the
fixed rule and nothing softer: a per-post test is decided only at >=10 posts
per arm OR >=21 days since it started; a winner needs a >=30% lift in MEDIAN
24h views; below that, write "no difference" or "continue". A period test
(volume) compares its on-period to the 14 days before it started on posts/day,
median 24h views and followers/week; revert if median views fall by more than
a third. You PROPOSE the verdict and the config change in the memo; you never
apply it.
```

- [ ] **Step 6: Run tests**

Run: `.venv/bin/pytest tests/test_experiment_report.py tests/test_autonomy_phase12.py -v`
Expected: PASS (the briefing tests, if any, still pass with the new section).

- [ ] **Step 7: Commit**

```bash
git add src/xbot/experiments.py src/xbot/storage/sqlite_repo.py src/xbot/briefing.py src/xbot/cli.py agent/prompts/strategist.md tests/test_experiment_report.py
git commit -m "Experiments grading: xbot experiments, briefing table, strategist rule"
```

---

### Task 7: Config block, docs, offline integration run, full suite

**Files:**
- Modify: `config.yaml` (new `experiments:` block)
- Modify: `CLAUDE.md` (commands list + a "Locked product decisions" bullet + phase status)
- Test: `tests/test_experiments_integration.py`

- [ ] **Step 1: Write the failing integration test**

```python
# tests/test_experiments_integration.py
"""Draft -> publish offline with the REAL config.yaml (experiments flipped on),
the template generator and the dry-run publisher: drafts get arms, a card PNG
is written, features carry the arms actually posted, `experiments` renders.
The full Orchestrator() constructor needs an LLM key for the judge, so the
object is assembled by hand the way tests/test_publish_due.py does."""
import pathlib

import pytest

from xbot.commentary.generate import TemplateCommentaryGenerator
from xbot.config import NS, load_config
from xbot.experiments import render, summarize
from xbot.models import Metrics, Post, Score, utcnow
from xbot.orchestrator import Orchestrator
from xbot.publish.dryrun import DryRunPublisher
from xbot.storage.sqlite_repo import SqliteRepository

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _cfg():
    data = load_config(ROOT / "config.yaml").as_dict()
    data["mode"] = {**data.get("mode", {}), "source": "sample",
                    "publisher": "dry_run", "autonomous": True}
    data["ranking"] = {**data.get("ranking", {}), "qa_gate": False,
                       "draft_prescreen": False}
    data["posting"] = {**data.get("posting", {}), "card_handle": "polar",
                       "max_source_age_hours": 0}
    for name in ("image_card", "author_bait", "volume"):
        data["experiments"][name]["enabled"] = True
    return NS(data)


def _orch(cfg, repo):
    o = object.__new__(Orchestrator)
    o.cfg, o.repo, o.judge_reasons = cfg, repo, {}
    o.source, o.judge, o.prescreen = None, None, None
    o.generator = TemplateCommentaryGenerator(cfg)
    o.publisher = DryRunPublisher(cfg)
    return o


def test_offline_draft_and_publish_with_all_experiments(tmp_path, monkeypatch):
    pytest.importorskip("PIL")
    monkeypatch.chdir(tmp_path)          # data/cards/ lands here, not in the repo
    cfg = _cfg()
    repo = SqliteRepository(":memory:"); repo.init_schema()
    # Four clearly different sources: the publish-time near-duplicate check
    # (similarity 0.82) would collapse look-alike texts into one post.
    sources = [
        "1. post one short video a day\n2. reuse the winner across accounts\n3. track views weekly",
        "1. put the paywall after the aha moment\n2. test two prices\n3. keep the free tier tiny",
        "1. reply to bigger accounts early\n2. ask one real question\n3. never paste a link",
        "1. write the hook first\n2. cut every sentence that explains the hook\n3. ship daily",
    ]
    for i, text in enumerate(sources, 1):
        p = Post(tweet_id=str(i), author_handle=f"maker{i}", author_name="M",
                 text=text, created_at=utcnow(), author_follower_count=5000,
                 metrics=Metrics(likes=20))
        repo.upsert_post(p)
        repo.save_score(Score(tweet_id=str(i), topic_fit=0.9, quote_worthy=0.8,
                              quote_score=0.8, judged=True))
    orch = _orch(cfg, repo)
    created = orch.make_drafts()
    oks = [c for c in created if c["ok"]]
    assert len(oks) >= 2, [c["notes"] for c in created]
    assert {c["draft"].arms["image_card"] for c in oks} == {"text", "card"}
    assert {c["draft"].arms["author_bait"] for c in oks} == {"tail", "question"}
    res = orch.publish_due()
    assert res["status"] == "posted" and res["count"] == 2      # volume: per_run 2
    rows = repo.conn.execute("SELECT arms FROM post_features").fetchall()
    assert len(rows) == 2 and all(r["arms"] for r in rows)
    assert list((tmp_path / "data" / "cards").glob("*.png"))    # one card arm posted
    text = render(summarize(repo.experiment_rows(), cfg))
    assert "image_card" in text and "volume" in text
```

(`make_drafts` tops the queue up to `posting.per_day + 1` = 4 pending drafts; with 4 seeded posts all four draft, arms alternate text/card and tail/question. `publish_due` posts 2 because `volume` sets `per_run` 2. `data/cards/` is created under `tmp_path` because the dry-run publisher writes relative to the CWD.)

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/pytest tests/test_experiments_integration.py -v`
Expected: FAIL — `KeyError: 'experiments'` (no config block yet).

- [ ] **Step 3: `config.yaml` experiments block**

Append after the `posting:` section:

```yaml
# GROWTH EXPERIMENTS (docs/superpowers/specs/2026-10-08-growth-experiments-design.md).
# Per-post tests assign an arm to every draft by strict alternation (the first
# arm = today's behavior = control); `volume` is an on/off period test. Graded
# by `xbot experiments` and the weekly strategist memo (>=10 posts/arm or 21
# days; >=30% lift in median 24h views names a winner). All OFF at merge;
# rollout order: volume + author_bait, then image_card once Pillow is in CI.
experiments:
  image_card:
    enabled: false
    arms: [text, card]           # card = self-rendered 1200x675 PNG of our own text
    started: 2026-10-09
  author_bait:
    enabled: false
    arms: [tail, question]       # question = one closing question to @author, no h/t
    started: 2026-10-09
  volume:
    enabled: false
    period: true
    started: 2026-10-09
    threshold_delta: 0.05        # subtracted from thresholds.topic_fit_min / quote_worthy_min
    per_run: 2                   # overrides posting.per_run while on (per_day still caps)
```

- [ ] **Step 4: CLAUDE.md**

- Commands block: add `| experiments [--json]` to the list.
- Locked product decisions: add
  `- **Voice is PLAIN-LANGUAGE, one point per post** (2026-10-08, owner: "I don't even understand this"). Reader = any founder/creator with no ads background. \`safety.check_readability\` (no bullets/jargon/long lines) is a hard gate; don't weaken it. Spec §6.`
  and
  `- **Experiments are config-driven** (\`experiments:\`), arms alternate per draft, graded by \`xbot experiments\` + the strategist with a fixed rule (10/arm or 21 days, 30% median lift). Experiments never bypass a gate.`
- Phase status: add `- 🧪 Growth experiments merged 2026-10-xx (image card, author bait, volume) — all disabled at merge; see spec for rollout order.`

- [ ] **Step 5: Full suite + offline smoke**

Run: `.venv/bin/pytest -q`
Expected: all pass (139 + new tests; 2 skips).

Run: `.venv/bin/python -c "import xbot.experiments, xbot.publish.card; print('import ok')"`
Expected: `import ok` (no Pillow import at module load).

- [ ] **Step 6: Commit**

```bash
git add config.yaml CLAUDE.md tests/test_experiments_integration.py
git commit -m "Experiments config block (all off), docs, offline integration test"
```

---

## Rollout after merge (owner steps, not code)

1. `config.yaml`: set `experiments.volume.enabled: true` and `experiments.author_bait.enabled: true`; commit; watch two publish windows (`gh run list -R mr008/Polar_ConsumerNews`, then `xbot report` output in the run log).
2. Set `posting.card_handle` to the bot's handle; set `experiments.image_card.enabled: true`; eyeball the first `card` post on X. If X's media upload rejects the simple-upload form, check `docs.x.com/x-api/media/quickstart/media-upload-chunked` and switch `_upload_png` to INIT/APPEND/FINALIZE; the fallback keeps posting text meanwhile.
3. First grading: `xbot experiments` locally (needs Turso env) or the 2026-10-13 strategist memo.
