"""Commentary QA gate — a cheap LLM editor pass on every draft.

Catches what the keyword safety filters can't:
  - META commentary (talking ABOUT the source instead of teaching — e.g. the
    real "Drop the full thread and I'll turn it into a sharp breakdown")
  - drafts with no applicable lesson
  - broken format (no hook / no takeaway / reads like a reply)

Fail-OPEN: if no LLM key is available or the call errors, the draft passes —
the deterministic safety gates (keywords, fabrication, length) already ran.
~$0.001 per draft on the judge model (Haiku).
"""
from __future__ import annotations

import json
import os
import re

from ..config import NS
from ..models import Post
from .generate import AUTO_ORDER, PROVIDERS

QA_SYSTEM = """You are the final editor for an X account that shares posts with a compact "steal this" teaching breakdown (hook + bullets + takeaway). A draft may be a short thread (parts separated by blank lines) — judge it as a whole.

REJECT the draft if ANY of these hold:
1) META: it addresses the source author or reader instead of teaching — asks for more content ("drop the full thread"), says it can't make a breakdown, comments on the post itself, or reads like a reply/DM.
2) NO_LESSON: a builder reading it learns no concrete, applicable tactic or insight.
3) FORMAT: missing a hook line or a takeaway, or it's one undifferentiated blob.

Judge the DRAFT only — assume the source post was already vetted. Be permissive about style; reject only real failures.

Return ONLY JSON: {"ok": true|false, "issue": "<reason, <=12 words>"}"""

REPLY_QA_SYSTEM = """You are the final editor for an X account's REPLIES to other people's posts. The account voice is a growth operator: concrete, curious, never sycophantic.

REJECT the reply if ANY of these hold:
1) SYCOPHANT: generic praise or flattery with no substance ("great post", "so true").
2) NO_VALUE: adds nothing concrete — restates the post, vague agreement, filler.
3) OFF_POINT: not actually about what the post says.
4) SOUNDS_LIKE_BOT: template-ish phrasing, hedge-stacking, refers to the author in the third person, or talks about itself/its account.

A good reply does exactly one of: adds a concrete insight, extends the tactic, or asks ONE specific follow-up question. Be permissive about style; reject only real failures.

Return ONLY JSON: {"ok": true|false, "issue": "<reason, <=12 words>"}"""


def _qa_call(system: str, user: str, cfg: NS, fail_open: bool,
             label: str) -> tuple[bool, str]:
    provider = cfg.get("llm.provider", "auto")
    order = AUTO_ORDER if provider == "auto" else [provider]
    chosen = next((p for p in order
                   if p in PROVIDERS and os.environ.get(PROVIDERS[p]["key_env"])), None)
    if chosen is None:
        return True, ""  # offline — deterministic gates already ran

    try:
        from openai import OpenAI  # lazy import
        kwargs = {"api_key": os.environ[PROVIDERS[chosen]["key_env"]]}
        if PROVIDERS[chosen]["base_url"]:
            kwargs["base_url"] = PROVIDERS[chosen]["base_url"]
        client = OpenAI(**kwargs)
        model = cfg.get("ranking.judge_model", cfg.get("llm.commentary_model", ""))
        resp = client.chat.completions.create(
            model=model, temperature=0, max_tokens=100,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
        )
        m = re.search(r"\{.*\}", resp.choices[0].message.content, re.S)
        data = json.loads(m.group(0)) if m else {}
        if bool(data.get("ok", True)):
            return True, ""
        return False, f"qa:{str(data.get('issue', 'rejected'))[:80]}"
    except Exception as e:
        if fail_open:
            print(f"  [qa] {label} gate unavailable ({type(e).__name__}) — passing through")
            return True, ""
        # Fail-CLOSED (publish-time re-vet): an unreviewed draft must not post.
        print(f"  [qa] {label} gate unavailable ({type(e).__name__}) — fail-closed")
        return False, "qa_unavailable"


def qa_commentary(post: Post, commentary: str, cfg: NS,
                  fail_open: bool = True) -> tuple[bool, str]:
    """Returns (ok, issue). Fail-open at draft time (deterministic gates already
    ran); pass fail_open=False at publish time so an unreviewable draft is
    skipped rather than posted."""
    if not cfg.get("ranking.qa_gate", True):
        return True, ""
    return _qa_call(QA_SYSTEM, (
        f"Source post (already vetted):\n\"\"\"\n{post.text[:600]}\n\"\"\"\n\n"
        f"Draft to check:\n\"\"\"\n{commentary}\n\"\"\""), cfg, fail_open, "draft")


def qa_reply(post: Post, reply_text: str, cfg: NS) -> tuple[bool, str]:
    """QA an outgoing reply to someone's post. Fail-open (the deterministic
    check_reply gates already ran; replies are low-stakes vs. broadcast posts)."""
    if not cfg.get("ranking.qa_gate", True):
        return True, ""
    return _qa_call(REPLY_QA_SYSTEM, (
        f"The post being replied to:\n\"\"\"\n{post.text[:600]}\n\"\"\"\n\n"
        f"Our reply to check:\n\"\"\"\n{reply_text}\n\"\"\""), cfg, True, "reply")
