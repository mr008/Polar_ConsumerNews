"""Commentary generators.

- TemplateCommentaryGenerator: deterministic, offline. Compact "steal this"
  breakdown so the dry-run works with no API keys (a placeholder voice).
- OpenAICompatGenerator: one class for every OpenAI-compatible provider
  (Groq, xAI/Grok, Gemini's compat endpoint, OpenAI) — just a base_url + key swap.
- AnthropicGenerator: Claude.

get_generator(cfg) resolves the provider from config (or auto-detects by which
key is present) and falls back to the template generator if no key exists.
"""
from __future__ import annotations

import os
import re
from typing import Protocol

from ..config import NS
from ..models import Draft, Post, is_web_source

STEP_RE = re.compile(r"^\s*\d+[\.\)]\s*(.+)$")

# OpenAI-compatible providers: base_url + which env var holds the key.
PROVIDERS = {
    "groq": {"base_url": "https://api.groq.com/openai/v1", "key_env": "GROQ_API_KEY"},
    "xai": {"base_url": "https://api.x.ai/v1", "key_env": "XAI_API_KEY"},
    "anthropic": {"base_url": "https://api.anthropic.com/v1/", "key_env": "ANTHROPIC_API_KEY"},
    "gemini": {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
               "key_env": "GEMINI_API_KEY"},
    "openai": {"base_url": None, "key_env": "OPENAI_API_KEY"},
}
DEFAULT_MODEL = {
    "groq": "llama-3.3-70b-versatile",
    "xai": "grok-4",
    "anthropic": "claude-sonnet-5",
    "gemini": "gemini-2.0-flash",
    "openai": "gpt-4o-mini",
}
AUTO_ORDER = ["anthropic", "groq", "xai", "gemini", "openai"]

# The Sonnet-5 / Opus-4.7+ / Fable-5 generation REJECTS sampling params
# (`temperature` 400s: "deprecated for this model"). Drop it for those, and
# retry-without on any future model that does the same.
_NO_SAMPLING = ("sonnet-5", "opus-4-7", "opus-4-8", "fable-5", "mythos-5")


def _omit_temperature(model: str) -> bool:
    m = (model or "").lower()
    return any(tag in m for tag in _NO_SAMPLING)


def openai_chat(client, *, model: str, messages: list, max_tokens: int,
                temperature=None):
    """chat.completions.create that adapts to models which reject `temperature`.
    Drops it for known new-gen models; for anything not yet listed, retries
    without it on a 'temperature'-related 400 so a new model never breaks us."""
    kwargs = dict(model=model, max_tokens=max_tokens, messages=messages)
    if temperature is not None and not _omit_temperature(model):
        kwargs["temperature"] = temperature
    try:
        return client.chat.completions.create(**kwargs)
    except Exception as e:
        if "temperature" in kwargs and "temperature" in str(e).lower():
            kwargs.pop("temperature")
            return client.chat.completions.create(**kwargs)
        raise


class CommentaryGenerator(Protocol):
    def generate(self, post: Post, allow_thread: bool = False, arms=None) -> Draft: ...


# ----------------------------- system prompt -----------------------------

# SHARED VOICE SPEC (AUTONOMY.md): agent/voice.md is the single source of truth
# for the public voice — the pipeline route (this file) and the Curator route
# both read it, so a Strategist voice improvement lands in one place and the
# fallback route can never go stale. The embedded prompt below is the
# byte-identical fallback for when the file isn't present (installed package
# run outside the repo); test_outcomes golden-tests the equality.
VOICE_SPEC_PATH = "agent/voice.md"


def _voice_spec_text() -> str | None:
    try:
        from pathlib import Path
        p = Path(VOICE_SPEC_PATH)
        if p.exists():
            return p.read_text(encoding="utf-8")
    except Exception:
        pass
    return None


def build_system_prompt(cfg: NS) -> str:
    v = cfg.voice
    max_chars = cfg.get("llm.max_commentary_chars", 240)
    spec = _voice_spec_text()
    if spec:
        return (spec.replace("<<STYLE>>", str(v.style))
                    .replace("<<MAX_CHARS>>", str(max_chars))).strip()
    return f"""You write the posts for a curator account whose mission is to SHARE SKILLS for growing consumer apps, backed by real numbers (AI UGC, creator ops, content + paid distribution, paywalls and monetization).

READER: any founder or creator, with NO ads or growth background. If they would have to already know the trick to follow the post, rewrite it.

VOICE: {v.style}. Operator energy WITH A POINT OF VIEW: teach the tactic AND say what you think of it (why it works, where it breaks, the part people miss). Opinions are about the TACTIC, never invented facts; stay neutral on whether the author's numbers are true (see TONE).

FORMAT: teach ONE point per post, explained fully, in <= {max_chars} characters (before the h/t tail):
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

Return ONLY the post text (or the SKIP line) — no preamble, no quotes around it."""


THREAD_INSTRUCTIONS = """
This source is substantial, so you MAY write a SHORT THREAD instead of one post, ONLY if the one point you picked cannot be explained in a single post. Thread format:
  - {n_parts} parts MAX, separated by a line containing exactly: ---
  - Part 1 = the hook post (<= {hook_budget} chars INCLUDING {ending})
  - Each later part = the rest of the SAME point, explained plainly (<= {part_budget} chars each, no h/t tail, no URLs, no hashtags, no bullets)
If one post is enough, write the normal single post instead."""


def _user_prompt(post: Post, cfg: NS = None, allow_thread: bool = False,
                 arms: dict | None = None) -> str:
    from ..experiments import arm  # lazy: avoid cycle
    question_arm = arm(arms, "author_bait") == "question" and not is_web_source(post)
    extra = ""
    if cfg is not None:
        from ..publish.publisher import body_budget, part_budget  # lazy: avoid cycle
        hook_budget = body_budget(post, cfg, arms)
        # ONE length knob: llm.max_commentary_chars is the target the model aims
        # for (optimal-length strategy), capped at the hard body budget so it can
        # never exceed the 280 ceiling. Keeps the system-prompt target and this
        # per-post instruction consistent (they used to conflict).
        target = min(int(cfg.get("llm.max_commentary_chars", 240)), hook_budget)
        what = "in total" if question_arm else "before the h/t tail"
        extra = (f"\nHARD LIMIT for this post: {hook_budget} characters {what} — "
                 f"aim for {target}. If in doubt, cut a sentence, never the explanation.")
        if allow_thread:
            extra += THREAD_INSTRUCTIONS.format(
                n_parts=int(cfg.get("posting.max_thread_parts", 3)),
                hook_budget=hook_budget,
                part_budget=part_budget(cfg),
                ending=(f"the closing question to @{post.author_handle} (no h/t tail)"
                        if question_arm else
                        f'the "h/t @{post.author_handle}" tail at its end'))
    if is_web_source(post):
        # Web article: teach the tactic in our own voice. The source is already a
        # dense brief — TEACH from it, don't re-compress it into a bare summary
        # (that trips the QA "reads like a summary / no takeaway" gate). No @handle
        # h/t (we don't tag blogs — a wrong @ would mis-credit); attribution is separate.
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


def split_parts(text: str, cfg: NS) -> tuple[str, list[str]]:
    """Split LLM output on '---' separator lines into (hook, thread parts)."""
    chunks = [c.strip() for c in re.split(r"\n\s*---\s*\n", text.strip()) if c.strip()]
    if not chunks:
        return text.strip(), []
    max_parts = int(cfg.get("posting.max_thread_parts", 3)) - 1 if cfg else 2
    return chunks[0], chunks[1: 1 + max_parts]


# ----------------------------- template (offline) -----------------------------

class TemplateCommentaryGenerator:
    def __init__(self, cfg: NS):
        self.cfg = cfg
        self.max_chars = cfg.get("llm.max_commentary_chars", 240)
        self.credit = cfg.get("voice.credit_style", "subtle_tail")

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

    @staticmethod
    def _shorten(s: str, n: int = 80) -> str:
        s = " ".join(s.split())
        return s if len(s) <= n else s[: n - 1].rstrip(" ,.") + "…"

    def _bullets(self, text: str) -> list[str]:
        steps = [self._shorten(m.group(1)) for line in text.splitlines()
                 if (m := STEP_RE.match(line))]
        return steps[:4]

    @staticmethod
    def _hook(text: str) -> str:
        t = text.lower()
        if any(k in t for k in ("ugc", "character", "video", "views")):
            return "The content engine quietly printing views right now:"
        if any(k in t for k in ("retention", "onboarding", "aha", "install")):
            return "The retention lever most apps ignore:"
        if any(k in t for k in ("hook", "launch", "cta", "first 3")):
            return "Steal this structure for your next launch:"
        if any(k in t for k in ("reuse", "distribution", "repost", "same ")):
            return "The marketing move most people overthink:"
        return "Worth saving for your next launch:"

    @staticmethod
    def _takeaway(text: str) -> str:
        t = text.lower()
        if any(k in t for k in ("distribution", "reuse", "repost")):
            return "Distribution > production. Run your winner back."
        if any(k in t for k in ("ugc", "character", "accounts", "views")):
            return "One asset, many accounts. Volume is the whole game."
        if "retention" in t or "aha" in t or "onboarding" in t:
            return "Ship people to the aha moment faster."
        if "hook" in t or "launch" in t:
            return "Hook first. Everything else second."
        return "Simple — and most people still skip it."


# ----------------------------- LLM (live) -----------------------------

class OpenAICompatGenerator:
    """Works with any OpenAI-compatible provider (Groq, xAI, Gemini, OpenAI)."""

    def __init__(self, cfg: NS, provider: str, model: str):
        self.cfg = cfg
        self.provider = provider
        self.model = model
        self.base_url = PROVIDERS[provider]["base_url"]
        self.key_env = PROVIDERS[provider]["key_env"]
        self.temperature = cfg.get("llm.temperature", 0.7)
        self.system = build_system_prompt(cfg)

    def generate(self, post: Post, allow_thread: bool = False, arms=None) -> Draft:
        return self._call(post, allow_thread=allow_thread, messages=[
            {"role": "system", "content": self.system},
            {"role": "user", "content": _user_prompt(post, self.cfg, allow_thread, arms)},
        ])

    def revise(self, post: Post, previous: str, feedback: str, arms=None) -> Draft:
        """One editor-feedback rewrite (used by the QA gate / length check).
        A rejected thread retries as a compact SINGLE post — simpler to fix."""
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

    def _call(self, post: Post, messages: list[dict], allow_thread: bool = False) -> Draft:
        from openai import OpenAI  # lazy import
        kwargs = {"api_key": os.environ[self.key_env]}
        if self.base_url:
            kwargs["base_url"] = self.base_url
        client = OpenAI(**kwargs)
        resp = openai_chat(client, model=self.model, temperature=self.temperature,
                           max_tokens=900 if allow_thread else 320, messages=messages)
        raw = resp.choices[0].message.content.strip()
        hook, parts = split_parts(raw, self.cfg) if allow_thread else (raw, [])
        return Draft(tweet_id=post.tweet_id, commentary=hook, parts=parts,
                     model=f"{self.provider}:{self.model}")


class AnthropicGenerator:
    def __init__(self, cfg: NS, model: str):
        self.cfg = cfg
        self.model = model
        self.temperature = cfg.get("llm.temperature", 0.7)
        self.system = build_system_prompt(cfg)

    def generate(self, post: Post, allow_thread: bool = False, arms=None) -> Draft:
        import anthropic  # lazy import
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        msg = client.messages.create(
            model=self.model, max_tokens=900 if allow_thread else 300,
            temperature=self.temperature,
            system=self.system,
            messages=[{"role": "user",
                       "content": _user_prompt(post, self.cfg, allow_thread, arms)}],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        hook, parts = split_parts(text.strip(), self.cfg) if allow_thread else (text.strip(), [])
        return Draft(tweet_id=post.tweet_id, commentary=hook, parts=parts, model=self.model)


class LLMUnavailable(RuntimeError):
    """No LLM key while the bot is LIVE. get_generator would silently fall back
    to the offline template, and the QA gate treats "no key" as offline and
    passes — so template text could publish. Stop loudly instead."""


def llm_key_available(cfg: NS) -> bool:
    provider = cfg.get("llm.provider", "auto")
    order = AUTO_ORDER if provider == "auto" else [provider]
    return any(p in PROVIDERS and os.environ.get(PROVIDERS[p]["key_env"]) for p in order)


def require_llm_if_live(cfg: NS) -> None:
    """Raise LLMUnavailable when mode.publisher is api and no LLM key resolves.
    Offline sample/dry_run runs keep the template fallback."""
    if cfg.get("mode.publisher", "") == "api" and not llm_key_available(cfg):
        provider = cfg.get("llm.provider", "auto")
        env = (PROVIDERS[provider]["key_env"] if provider in PROVIDERS
               else "an LLM API key")
        raise LLMUnavailable(
            f"{env} is missing but mode.publisher is 'api' (LIVE). Refusing to "
            f"draft/publish with the offline template generator. Restore the key "
            f"(GitHub: `gh secret set {env}`; local: .env) and re-run.")


def get_generator(cfg: NS) -> CommentaryGenerator:
    provider = cfg.get("llm.provider", "auto")
    model = cfg.get("llm.commentary_model", "")
    order = AUTO_ORDER if provider == "auto" else [provider]
    for prov in order:
        if prov in PROVIDERS and os.environ.get(PROVIDERS[prov]["key_env"]):
            chosen = model if (model and provider != "auto") else DEFAULT_MODEL[prov]
            return OpenAICompatGenerator(cfg, prov, chosen)
    return TemplateCommentaryGenerator(cfg)
