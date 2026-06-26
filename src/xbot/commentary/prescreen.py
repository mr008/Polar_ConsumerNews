"""Cheap pre-draft material check.

~90% of posts that clear eligibility (qw>=0.35, on-topic, not a dup) still get a
SKIP from the commentary generator: truncated retweet stubs, teasers, and flexes
have no teachable method to write about. Today we pay the *expensive* commentary
model (Sonnet) to read each one and say "SKIP". This gate asks a ~24x cheaper
model (Haiku) the same yes/no question FIRST, so Sonnet only ever drafts posts
that actually contain a method.

It mirrors the generator's SKIP criteria (see build_system_prompt's HARD RULES)
so the two agree. It is FAIL-OPEN by design: any error, timeout, or unparseable
answer lets the post through to drafting — a flaky screen must never cost a post.
"""
from __future__ import annotations

import os
from typing import Optional, Protocol

from ..config import NS
from ..models import Post
from .generate import AUTO_ORDER, PROVIDERS

PRESCREEN_SYSTEM = """You are a fast gatekeeper for an account that teaches operators how to make viral consumer-app content (AI UGC, growth, distribution, indie startups). A writer will turn this post into a short "steal this" take — they only need a real POINT to riff on, not a step-by-step tutorial.

You are shown ONE post. Almost everything qualifies — a number, a claim, a tool, an opinion, a one-line insight, a flex with a hint of how, a retweet that carries its own content. A later, smarter step decides if it is actually worth posting; your ONLY job is to throw out posts that contain literally nothing to work with.

Answer NO only in these blatant cases:
- the text is cut off mid-sentence or mid-word with no complete thought (a true fragment, e.g. ends "...and the best part is" or "RT @x: how I …" with nothing after),
- it is a pure pointer with no content of its own ("link in bio", "thread below 👇", "watch this", "DM me") and essentially nothing else,
- it is so short (a few words) there is no claim at all.

Everything else is YES — including retweets that carry a full point, revenue/milestone flexes, acquisition or launch announcements, casual one-liners, and opinions. When in any doubt at all, answer YES. Rejecting a usable post is far worse than passing a weak one.

Reply with EXACTLY one word: YES or NO. No punctuation, no explanation."""


class DraftPrescreen(Protocol):
    def has_material(self, post: Post) -> bool: ...


class LLMPrescreen:
    def __init__(self, cfg: NS, provider: str):
        self.cfg = cfg
        self.provider = provider
        # Defaults to the judge model (Haiku) — cheap, already in use.
        self.model = cfg.get("ranking.prescreen_model",
                             cfg.get("ranking.judge_model", "claude-haiku-4-5-20251001"))

    def has_material(self, post: Post) -> bool:
        text = (post.text or "").strip()
        try:
            answer = self._call(text, post.has_media)
        except Exception as e:
            # FAIL-OPEN: never lose a post to a flaky screen.
            print(f"  [prescreen] unavailable ({type(e).__name__}) — drafting anyway")
            return True
        # Only a decisive "NO" rejects. Anything else (YES, blank, garbage) drafts.
        return not answer.strip().upper().startswith("NO")

    def _call(self, text: str, has_media: bool) -> str:
        from openai import OpenAI  # lazy import
        kwargs = {"api_key": os.environ[PROVIDERS[self.provider]["key_env"]]}
        if PROVIDERS[self.provider]["base_url"]:
            kwargs["base_url"] = PROVIDERS[self.provider]["base_url"]
        client = OpenAI(**kwargs)
        body = text[:600] + (" [attached video/media]" if has_media else "")
        resp = client.chat.completions.create(
            model=self.model, temperature=0, max_tokens=4,
            messages=[{"role": "system", "content": PRESCREEN_SYSTEM},
                      {"role": "user", "content": f"Post:\n{body}"}],
        )
        return resp.choices[0].message.content or ""


def get_prescreen(cfg: NS) -> Optional[DraftPrescreen]:
    """Return a prescreen if enabled AND an LLM provider key is present, else None.
    None means 'no screening' — drafting proceeds as before (fail-open at the
    wiring level too, so the offline/template path is never gated)."""
    if not cfg.get("ranking.draft_prescreen", False):
        return None
    provider = cfg.get("llm.provider", "auto")
    order = AUTO_ORDER if provider == "auto" else [provider]
    for prov in order:
        if prov in PROVIDERS and os.environ.get(PROVIDERS[prov]["key_env"]):
            return LLMPrescreen(cfg, prov)
    return None
