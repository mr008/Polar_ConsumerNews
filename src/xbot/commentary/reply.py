"""Reply generation for the auto-reply engine.

A reply is the account's voice in someone else's thread — the growth lever for
small accounts (a reply is weighted ~27x a like; an author reply-back ~150x).
The bar: add something real or ask something real. Anything that smells like a
bot ("great post!") is worse than silence; the gates in safety.check_reply +
qa.qa_reply enforce that.
"""
from __future__ import annotations

import os

from ..config import NS
from ..models import Post
from .generate import AUTO_ORDER, DEFAULT_MODEL, PROVIDERS


def build_reply_system_prompt(cfg: NS) -> str:
    max_chars = int(cfg.get("replies.max_reply_chars", 240))
    return f"""You write SHORT REPLIES from a growth-operator account focused on viral consumer-app content (AI UGC, content-driven growth, distribution). You are replying to a post from an account we follow and respect.

The reply must do EXACTLY ONE of:
1) Add one concrete insight or extension to their point (a sharper framing, the step people miss, the constraint that makes it work).
2) Name a relevant pattern from public examples ("the accounts doing X all seem to do Y") — observations, not invented war stories.
3) Ask ONE specific follow-up question about their tactic or result. PREFER this when the post describes a process or an outcome — a question that shows you actually read it and that they will want to answer.

HARD RULES (never break):
  - <= {max_chars} characters; aim for 150-200. One thought, not a thread.
  - NO links. NO hashtags. NO @mentions. At most one emoji.
  - NEVER invent numbers, results, or personal experiences. Use only numbers that appear in their post.
  - NEVER open with praise ("great post", "love this", "so true") — banned outright.
  - Never refer to yourself as an account/bot/curator, never mention replying or engagement.
  - Write like a person mid-conversation: lowercase-casual is fine, no corporate tone.
  - If there is nothing genuine to add or ask, output exactly: SKIP: <reason in <=8 words>

Return ONLY the reply text (or the SKIP line) — no preamble, no quotes around it."""


class ReplyGenerator:
    """One class for every provider — Anthropic included via its OpenAI-compat
    endpoint (same pattern as the QA gate)."""

    def __init__(self, cfg: NS, provider: str, model: str):
        self.cfg = cfg
        self.provider = provider
        self.model = model
        self.system = build_reply_system_prompt(cfg)

    def _user_prompt(self, post: Post) -> str:
        return (f"Post by @{post.author_handle} ({post.author_name}, "
                f"{post.author_follower_count:,} followers):\n"
                f'"""\n{post.text}\n"""\n\n'
                f"Write the reply now.")

    def generate(self, post: Post) -> tuple[str, str]:
        """Returns (reply_text, model_label). reply_text may be a SKIP line."""
        return self._call([
            {"role": "system", "content": self.system},
            {"role": "user", "content": self._user_prompt(post)},
        ], post)

    def revise(self, post: Post, previous: str, feedback: str) -> tuple[str, str]:
        return self._call([
            {"role": "system", "content": self.system},
            {"role": "user", "content": self._user_prompt(post)},
            {"role": "assistant", "content": previous},
            {"role": "user", "content": (
                f"Editor rejected that reply: {feedback}\n"
                "Rewrite it fixing ONLY that problem, keeping every other rule. "
                "Return only the reply text.")},
        ], post)

    def _call(self, messages: list[dict], post: Post) -> tuple[str, str]:
        from openai import OpenAI  # lazy import
        kwargs = {"api_key": os.environ[PROVIDERS[self.provider]["key_env"]]}
        if PROVIDERS[self.provider]["base_url"]:
            kwargs["base_url"] = PROVIDERS[self.provider]["base_url"]
        client = OpenAI(**kwargs)
        resp = client.chat.completions.create(
            model=self.model, temperature=float(self.cfg.get("llm.temperature", 0.7)),
            max_tokens=200, messages=messages,
        )
        return (resp.choices[0].message.content.strip(),
                f"{self.provider}:{self.model}")


def get_reply_generator(cfg: NS):
    """Resolve provider/model like get_generator. Returns None when no key is
    available — the reply step then no-ops (replies are never template-generated;
    a canned reply is exactly the bot-smell we filter against)."""
    provider = cfg.get("llm.provider", "auto")
    order = AUTO_ORDER if provider == "auto" else [provider]
    for prov in order:
        if prov in PROVIDERS and os.environ.get(PROVIDERS[prov]["key_env"]):
            model = cfg.get("replies.model", "") or DEFAULT_MODEL[prov]
            return ReplyGenerator(cfg, prov, model)
    return None
