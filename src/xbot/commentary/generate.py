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
    def generate(self, post: Post, allow_thread: bool = False) -> Draft: ...


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
    return f"""You write the commentary for a curator account whose mission is to SHARE SKILLS for making viral consumer-app content (AI UGC, content-driven growth, distribution).

VOICE: {v.style}. Punchy operator energy, NOT a measured curator. Skill-sharing angle: teach the tactic — why it works / the move to steal / the part people miss.

FORMAT: a compact "steal this" breakdown in ONE post (<= {max_chars} characters):
  - HOOK (the first line — it does ~80% of the work; stop the scroll):
      * Lead with the single most surprising SPECIFIC from the source — a hard number, a concrete result, or a counter-intuitive claim ("$100 test made $80k day one", "distribution beats product").
      * NO preamble or throat-clearing ("here's how", "a thread on", "let me explain", "the key to"). Open on the payload.
      * Make it a complete, standalone line — NOT a vague teaser or cliffhanger. The first line must earn the second.
  - 2-4 short bullets (use "•") — the concrete moves to steal
  - a one-line takeaway

PROTAGONIST: the post is about US (the teacher), not the source author. Do NOT open with their @handle. End with a small "h/t @handle" tail only — use their actual handle from the source.

TONE: straight. Report the author's claims neutrally (e.g. "he shares a case study of 14M+ views"). Never vouch, never editorialize doubt.

SOUND HUMAN: write like a real operator typing fast, not like an AI. Do NOT use em dashes (—), en dashes, or " - " as connectors. If one would normally appear, use a comma for a continuing thought or a period to start a new sentence. Skip other AI tells too (the "it's not just X, it's Y" cadence, "delve", over-tidy symmetry).

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
This source is substantial, so you MAY write a SHORT TUTORIAL THREAD instead of one post — but ONLY if you can extract 3+ distinct, concrete steps from the source. Thread format:
  - {n_parts} parts MAX, separated by a line containing exactly: ---
  - Part 1 = the hook post (<= {hook_budget} chars INCLUDING the "h/t @{handle}" tail at its end)
  - Each later part = numbered tutorial steps and/or the takeaway (<= {part_budget} chars each, no h/t tail, no URLs, no hashtags)
If the source does not support 3+ concrete steps, write the normal single post instead."""


def _user_prompt(post: Post, cfg: NS = None, allow_thread: bool = False) -> str:
    extra = ""
    if cfg is not None:
        from ..publish.publisher import body_budget, part_budget  # lazy: avoid cycle
        hook_budget = body_budget(post, cfg)
        # ONE length knob: llm.max_commentary_chars is the target the model aims
        # for (optimal-length strategy), capped at the hard body budget so it can
        # never exceed the 280 ceiling. Keeps the system-prompt target and this
        # per-post instruction consistent (they used to conflict).
        target = min(int(cfg.get("llm.max_commentary_chars", 240)), hook_budget)
        extra = (f"\nHARD LIMIT for this post: {hook_budget} characters before the "
                 f"h/t tail — aim for {target}. If in doubt, cut a bullet.")
        if allow_thread:
            extra += THREAD_INSTRUCTIONS.format(
                n_parts=int(cfg.get("posting.max_thread_parts", 3)),
                hook_budget=hook_budget,
                part_budget=part_budget(cfg),
                handle=post.author_handle)
    if is_web_source(post):
        # Web article: teach the tactic in our own voice. The source is already a
        # dense brief — TEACH from it, don't re-compress it into a bare summary
        # (that trips the QA "reads like a summary / no takeaway" gate). No @handle
        # h/t (we don't tag blogs — a wrong @ would mis-credit); attribution is separate.
        return (f"Source article ({post.author_name}):\n"
                f'"""\n{post.text}\n"""\n\n'
                f"Write the commentary now as a COMPLETE teaching post: a hook, 2-3 "
                f"bullets, and a clear one-line takeaway on its OWN final line — never "
                f"skip the takeaway. Teach the tactic in your own words (the source is "
                f"a brief, not something to compress further). Do NOT add an h/t or "
                f"@mention.{extra}")
    return (f"Source post by @{post.author_handle} ({post.author_name}):\n"
            f'"""\n{post.text}\n"""\n\n'
            f"Write the commentary now. End with: h/t @{post.author_handle}{extra}")


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

    def generate(self, post: Post, allow_thread: bool = False) -> Draft:
        hook = self._hook(post.text)
        bullets = self._bullets(post.text)
        takeaway = self._takeaway(post.text)
        tail = f" h/t @{post.author_handle}" if self.credit == "subtle_tail" else ""
        if bullets:
            body = hook + "\n\n" + "\n".join(f"• {b}" for b in bullets) + "\n\n" + takeaway
        else:
            body = hook + "\n\n" + takeaway
        text = (body + tail).strip()
        while len(text) > self.max_chars and bullets:
            bullets = bullets[:-1]
            body = (hook + "\n\n" + "\n".join(f"• {b}" for b in bullets) + "\n\n" + takeaway
                    if bullets else hook + "\n\n" + takeaway)
            text = (body + tail).strip()
        return Draft(tweet_id=post.tweet_id, commentary=text, model="template")

    @staticmethod
    def _shorten(s: str, n: int = 58) -> str:
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

    def generate(self, post: Post, allow_thread: bool = False) -> Draft:
        return self._call(post, allow_thread=allow_thread, messages=[
            {"role": "system", "content": self.system},
            {"role": "user", "content": _user_prompt(post, self.cfg, allow_thread)},
        ])

    def revise(self, post: Post, previous: str, feedback: str) -> Draft:
        """One editor-feedback rewrite (used by the QA gate / length check).
        A rejected thread retries as a compact SINGLE post — simpler to fix."""
        return self._call(post, messages=[
            {"role": "system", "content": self.system},
            {"role": "user", "content": _user_prompt(post, self.cfg)},
            {"role": "assistant", "content": previous},
            {"role": "user", "content": (
                f"Editor rejected that draft: {feedback}\n"
                "Rewrite it as ONE single post fixing ONLY that problem. Keep every "
                "other rule (voice, format, h/t tail, no fabrication). "
                "Return only the post text.")},
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

    def generate(self, post: Post, allow_thread: bool = False) -> Draft:
        import anthropic  # lazy import
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        msg = client.messages.create(
            model=self.model, max_tokens=900 if allow_thread else 300,
            temperature=self.temperature,
            system=self.system,
            messages=[{"role": "user",
                       "content": _user_prompt(post, self.cfg, allow_thread)}],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        hook, parts = split_parts(text.strip(), self.cfg) if allow_thread else (text.strip(), [])
        return Draft(tweet_id=post.tweet_id, commentary=hook, parts=parts, model=self.model)


def get_generator(cfg: NS) -> CommentaryGenerator:
    provider = cfg.get("llm.provider", "auto")
    model = cfg.get("llm.commentary_model", "")
    order = AUTO_ORDER if provider == "auto" else [provider]
    for prov in order:
        if prov in PROVIDERS and os.environ.get(PROVIDERS[prov]["key_env"]):
            chosen = model if (model and provider != "auto") else DEFAULT_MODEL[prov]
            return OpenAICompatGenerator(cfg, prov, chosen)
    return TemplateCommentaryGenerator(cfg)
