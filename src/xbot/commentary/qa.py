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

QA_SYSTEM = """You are the final editor for an X account that quote-tweets posts with a compact "steal this" teaching breakdown (hook + bullets + takeaway).

REJECT the draft if ANY of these hold:
1) META: it addresses the source author or reader instead of teaching — asks for more content ("drop the full thread"), says it can't make a breakdown, comments on the post itself, or reads like a reply/DM.
2) NO_LESSON: a builder reading it learns no concrete, applicable tactic or insight.
3) FORMAT: missing a hook line or a takeaway, or it's one undifferentiated blob.

Judge the DRAFT only — assume the source post was already vetted. Be permissive about style; reject only real failures.

Return ONLY JSON: {"ok": true|false, "issue": "<reason, <=12 words>"}"""


def qa_commentary(post: Post, commentary: str, cfg: NS) -> tuple[bool, str]:
    """Returns (ok, issue). Fail-open on any infrastructure problem."""
    if not cfg.get("ranking.qa_gate", True):
        return True, ""
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
            messages=[
                {"role": "system", "content": QA_SYSTEM},
                {"role": "user", "content": (
                    f"Source post (already vetted):\n\"\"\"\n{post.text[:600]}\n\"\"\"\n\n"
                    f"Draft to check:\n\"\"\"\n{commentary}\n\"\"\"")},
            ],
        )
        m = re.search(r"\{.*\}", resp.choices[0].message.content, re.S)
        data = json.loads(m.group(0)) if m else {}
        if bool(data.get("ok", True)):
            return True, ""
        return False, f"qa:{str(data.get('issue', 'rejected'))[:80]}"
    except Exception as e:
        print(f"  [qa] gate unavailable ({type(e).__name__}) — passing draft through")
        return True, ""
