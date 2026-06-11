"""Safety + quality filters. Fail-closed. Runs on the source post AND the
generated commentary/replies.

These are deliberately conservative keyword/heuristic gates; the LLM QA gate
(qa.py) layers nuance on top. The anti-fabrication number check and the
refusal-text check below are deterministic and run on every draft AND again at
publish time (a refusal slipped to the live account on 2026-06-10 because the
only gates that day were keywords/numbers/length).
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

# Deterministic refusal/meta detection: when the generator has nothing to teach
# it tends to talk ABOUT the source or TO the operator instead of writing
# commentary. These phrases never appear in legitimate breakdowns (the voice
# rules forbid addressing the author/reader), so substring match is safe.
# Calibrated against the three real refusals that reached/neared the live
# account (prod drafts #6 and #10, local draft #14, 2026-06-09/10).
REFUSAL_MARKERS = [
    "this post doesn't", "this post does not", "the source post", "the source is",
    "doesn't contain enough", "does not contain enough", "i can't write",
    "i cannot write", "i can't make", "i cannot make", "drop the full thread",
    "share a source post", "no completed insight", "nothing skill-teachable",
    "skill-share breakdown", "compliant breakdown", "without fabricating",
    "happy to write", "to get a good output", "real material to work with",
    "as an ai", "i'm unable to", "i am unable to",
]

# Generic-praise replies scream "bot" and add nothing. Exact/prefix match on the
# stripped reply (a real reply that merely *contains* "love this" mid-sentence
# is fine).
GENERIC_PRAISE = [
    "great post", "so true", "this!", "love this", "well said", "100%", "facts",
    "this is gold", "amazing post", "totally agree", "couldn't agree more",
    "great breakdown", "great thread", "underrated post", "this is the way",
]

_DIGITS = re.compile(r"\d+")
# Digits that aren't claims: numbered-list markers ("1." / "2)"), digits inside
# @handles ("h/t @gregpr07" blocked two real drafts as "fabricated"), and digits
# inside URLs (the dedicated URL gates report those properly).
_ENUM_MARKER = re.compile(r"^\s*\d+[\.\)]\s", re.M)
_HANDLE_TOKEN = re.compile(r"@\w+")
_URL_SPAN = re.compile(r"https?://\S+|t\.co/\S+|\bwww\.\S+", re.IGNORECASE)
URL_IN_TEXT = re.compile(r"https?://|t\.co/|\bwww\.", re.IGNORECASE)
HASHTAG = re.compile(r"(?:^|\s)#\w+")
MENTION = re.compile(r"(?:^|\s)@\w+")


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


def looks_like_refusal(text: str) -> str:
    """Return the matched refusal marker, or '' if the text reads like real
    commentary. SKIP-sentinel outputs are also caught here as a backstop."""
    t = text.lower()
    if t.startswith("skip:") or t.startswith("skip —"):
        return "skip_sentinel"
    for marker in REFUSAL_MARKERS:
        if marker in t:
            return marker
    return ""


def classify_source(post: Post, cfg: NS) -> tuple[bool, str]:
    """Gate the SOURCE post before we spend an LLM call on it."""
    text = (post.text or "").lower()
    for cat in cfg.get("safety.exclude", []):
        if cat in EXCLUDE_KEYWORDS and _hits(text, EXCLUDE_KEYWORDS[cat]):
            return False, f"excluded:{cat}"
    if _hits(text, PROFANITY):
        return False, "profanity"
    return True, ""


def _check_text_common(post: Post, text: str, cfg: NS, src_digits: set) -> tuple[bool, str]:
    """Checks shared by the commentary hook, thread parts, and replies."""
    low = text.lower()
    for cat in cfg.get("safety.exclude", []):
        if cat in EXCLUDE_KEYWORDS and _hits(low, EXCLUDE_KEYWORDS[cat]):
            return False, f"commentary_excluded:{cat}"
    if _hits(low, PROFANITY):
        return False, "commentary_profanity"
    marker = looks_like_refusal(text)
    if marker:
        return False, f"refusal_meta:{marker[:40]}"
    # Anti-fabrication: every CLAIM number must appear in the source (list
    # markers and @handle digits exempt — they aren't claims).
    cleaned = _URL_SPAN.sub(" ", _HANDLE_TOKEN.sub(" ", _ENUM_MARKER.sub("", text)))
    for d in _DIGITS.findall(cleaned):
        if d not in src_digits:
            return False, f"fabricated_number:{d}"
    return True, "ok"


def check_commentary(post: Post, commentary: str, cfg: NS,
                     parts: list[str] | None = None) -> tuple[bool, str]:
    """Gate the generated COMMENTARY (hook + any thread parts)."""
    src_digits = set(_DIGITS.findall(post.text or ""))

    ok, note = _check_text_common(post, commentary, cfg, src_digits)
    if not ok:
        return False, note

    # Length: measure what will ACTUALLY be posted in the active format (the
    # h/t tail / attribution line eat into the 280).
    from ..publish.publisher import (body_budget, part_budget, posting_format,
                                     strip_ht_tail)  # lazy: avoid import cycle
    budget = body_budget(post, cfg)
    body = strip_ht_tail(commentary)
    if len(body) > budget:
        return False, f"too_long:{len(body)}>{budget}"
    # The main post must never carry a URL outside legacy link mode.
    if posting_format(cfg) != "link" and URL_IN_TEXT.search(commentary):
        return False, "url_in_commentary"

    max_parts = int(cfg.get("posting.max_thread_parts", 3)) - 1
    for i, part in enumerate(parts or [], 1):
        if i > max_parts:
            return False, f"too_many_parts:{len(parts)}>{max_parts}"
        ok, note = _check_text_common(post, part, cfg, src_digits)
        if not ok:
            return False, f"part{i}_{note}"
        if URL_IN_TEXT.search(part):
            return False, f"part{i}_url"
        if len(part) > part_budget(cfg):
            return False, f"part{i}_too_long:{len(part)}>{part_budget(cfg)}"
    return True, "ok"


def check_reply(post: Post, text: str, cfg: NS) -> tuple[bool, str]:
    """Gate a generated REPLY to someone else's post. Stricter than commentary:
    no URLs, no hashtags, no @mentions, no generic praise, tight length."""
    text = (text or "").strip()
    src_digits = set(_DIGITS.findall(post.text or ""))

    ok, note = _check_text_common(post, text, cfg, src_digits)
    if not ok:
        return False, f"reply_{note}"

    if URL_IN_TEXT.search(text):
        return False, "reply_url"
    if HASHTAG.search(text):
        return False, "reply_hashtag"
    if MENTION.search(text):
        return False, "reply_mention"

    low = text.lower().strip(" .!🔥👏💯")
    for praise in GENERIC_PRAISE:
        if low == praise or (low.startswith(praise) and len(text) < 40):
            return False, f"reply_generic_praise:{praise}"

    max_chars = int(cfg.get("replies.max_reply_chars", 240))
    if len(text) > max_chars:
        return False, f"reply_too_long:{len(text)}>{max_chars}"
    if len(text) < 30:
        return False, f"reply_too_short:{len(text)}"
    return True, "ok"
