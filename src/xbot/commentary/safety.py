"""Safety + quality filters. Fail-closed. Runs on the source post AND the
generated commentary.

These are deliberately conservative keyword/heuristic gates for Phase 0; the
live version adds a zero-shot LLM classifier for the nuanced categories. The
anti-fabrication number check below is real and stays in production.
"""
from __future__ import annotations

import re

from ..config import NS
from ..models import Post

# app-revenue / "$X/mo" growth talk is IN-SCOPE → investment keywords are specific.
EXCLUDE_KEYWORDS: dict[str, list[str]] = {
    "politics": ["election", "republican", "democrat", "president", "senator",
                 "congress", "immigration", "left wing", "right wing", "political party"],
    "ragebait": ["retweet if you agree", "ratio", "triggered", "rage", "outrage", "destroying the country"],
    "nsfw": ["nsfw", "porn", "nude", "onlyfans", "explicit sex"],
    "harassment": ["idiot", "clown", "pathetic", "scumbag", "expose this person"],
    "personal_drama": ["beef with", "calling out", "drama", "canceled", "cancelled", "subtweet"],
    "medical_advice": ["supplement", "dosage", "migraine", "ibuprofen", "prescription",
                       "diagnose", "cure your", "mg daily", "mg of"],
    "legal_advice": ["lawsuit", "sue them", "legal advice", "attorney", "you should sue"],
    "investment_advice": ["buy this stock", "invest in this", "crypto", "altcoin", "$btc",
                          "to the moon", "ticker", "portfolio allocation"],
}

PROFANITY = ["fuck", "shit", "bitch", "asshole", "cunt"]

_DIGITS = re.compile(r"\d+")


def _hits(text: str, words: list[str]) -> bool:
    # Word-boundary match for single words (so "rage" doesn't fire on "leverage");
    # phrases match as substrings.
    for w in words:
        if " " in w:
            if w in text:
                return True
        elif re.search(rf"\b{re.escape(w)}\b", text):
            return True
    return False


def classify_source(post: Post, cfg: NS) -> tuple[bool, str]:
    """Gate the SOURCE post before we spend an LLM call on it."""
    text = (post.text or "").lower()
    for cat in cfg.get("safety.exclude", []):
        if cat in EXCLUDE_KEYWORDS and _hits(text, EXCLUDE_KEYWORDS[cat]):
            return False, f"excluded:{cat}"
    if _hits(text, PROFANITY):
        return False, "profanity"
    return True, ""


def check_commentary(post: Post, commentary: str, cfg: NS) -> tuple[bool, str]:
    """Gate the generated COMMENTARY. Includes the anti-fabrication number rule."""
    text = commentary.lower()

    for cat in cfg.get("safety.exclude", []):
        if cat in EXCLUDE_KEYWORDS and _hits(text, EXCLUDE_KEYWORDS[cat]):
            return False, f"commentary_excluded:{cat}"
    if _hits(text, PROFANITY):
        return False, "commentary_profanity"

    # Anti-fabrication: every number in the commentary must appear in the source.
    src_digits = set(_DIGITS.findall(post.text or ""))
    for d in _DIGITS.findall(commentary):
        if d not in src_digits:
            return False, f"fabricated_number:{d}"

    # Length: measure what will ACTUALLY be posted. In link mode the attribution
    # line ("h/t @handle: <url>") eats ~45 chars of the 280, so a 270-char
    # commentary would be silently truncated mid-sentence at post time.
    from ..publish.publisher import body_budget, strip_ht_tail  # lazy: avoid import cycle
    budget = body_budget(post, cfg)
    body = strip_ht_tail(commentary)
    if len(body) > budget:
        return False, f"too_long:{len(body)}>{budget}"

    return True, "ok"
