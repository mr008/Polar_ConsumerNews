"""Online CONTENT source: web search finds tactical blog posts, a cheap model
summarizes each into a teaching brief, and we emit Post-like candidates that flow
through the normal score -> draft -> publish pipeline. Published as an ORIGINAL
post (no quote, no @handle h/t) — the tactic is taught in the bot's own voice.

Cost: the Brave search is FREE (free tier); the only spend is one small summarize
call per NEW article (Haiku, ~$0.003). No X reads. Runs on its own light cadence.
"""
from __future__ import annotations

import hashlib
import os
import re
from urllib.parse import urlparse

from ..models import WEB_ID_PREFIX, Post, utcnow
from . import web_discovery as wd  # reuse the Brave client + tag stripping

# Domains that are not tactical blog articles: video/social, marketplaces, and
# listicle/influencer-directory farms. X links are handled by account discovery.
_SKIP_DOMAINS = (
    "youtube.com", "youtu.be", "tiktok.com", "instagram.com", "pinterest.",
    "reddit.com", "facebook.com", "linkedin.com", "x.com", "twitter.com",
    "feedspot.com", "collabstr.com", "joinbrands.com", "producthunt.com",
    "apps.apple.com", "play.google.com", "amazon.",
)


def _skip(url: str) -> bool:
    u = url.lower()
    return not u.startswith("http") or any(d in u for d in _SKIP_DOMAINS)


def _blurb(r: dict) -> str:
    """Brave's description + extra_snippets, tags stripped — the fallback source
    text when the full article can't be fetched."""
    parts = [r.get("description", "")] + (r.get("extra_snippets") or [])
    return re.sub(r"\s+", " ", wd._TAG_RE.sub(" ", " ".join(p for p in parts if p))).strip()


def web_id(url: str) -> str:
    """Deterministic candidate id for a URL (idempotent upserts + cheap dedup)."""
    return WEB_ID_PREFIX + hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


def find_article_candidates(queries, key, *, provider="brave", per_query=15):
    """Run the content queries and return deduped [(url, title, blurb)] for results
    that look like tactical blog articles (junk domains dropped)."""
    if not key or not queries:
        return []
    fetch = wd._PROVIDERS.get(provider)
    if fetch is None:
        print(f"  [web-content] unknown provider '{provider}' — skipped")
        return []
    out: dict[str, dict] = {}
    for q in queries:
        try:
            results = fetch(q, key, per_query, 20.0)
        except Exception as e:
            print(f"  [web-content] query failed ({type(e).__name__}): {q[:60]}")
            continue
        for r in results:
            url = (r.get("url") or "").strip()
            if not url or _skip(url) or url in out:
                continue
            title = re.sub(r"\s+", " ", wd._TAG_RE.sub("", r.get("title", ""))).strip()
            out[url] = {"title": title, "blurb": _blurb(r)}
    return [(u, v["title"], v["blurb"]) for u, v in out.items()]


def _fetch_text(url: str, timeout: float = 15.0) -> str:
    """Best-effort full-article text (httpx + crude HTML strip). Returns "" on any
    failure — the summarizer then falls back to the search blurb."""
    try:
        import httpx  # lazy: only the live web-content path needs it

        resp = httpx.get(url, timeout=timeout, follow_redirects=True,
                         headers={"User-Agent": "Mozilla/5.0 (compatible; xbot/1.0)"})
        resp.raise_for_status()
        html = resp.text
    except Exception:
        return ""
    html = re.sub(r"(?is)<(script|style|noscript|nav|footer|header|form)[^>]*>.*?</\1>",
                  " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", text).strip()


_SUMMARY_SYSTEM = (
    "You extract a compact TACTICAL TEACHING BRIEF from a web article about making "
    "viral consumer-app content (AI UGC, content-driven growth, distribution). "
    "Output 2-5 plain-prose sentences capturing the concrete tactics, formulas, "
    "numbers, and steps a practitioner could STEAL. Preserve exact numbers. No "
    "markdown, no preamble. If the article is NOT a tactical how-to (a tool "
    "listicle or ad, a marketplace, a news blurb, or too thin to teach anything), "
    "output exactly: SKIP"
)


def summarize_article(title: str, blurb: str, url: str, cfg,
                      min_chars: int = 400) -> str | None:
    """Fetch (or fall back to the blurb) and summarize into a teaching brief.
    Returns None when the model says SKIP or no LLM key is available."""
    from ..commentary.generate import PROVIDERS, openai_chat  # lazy: avoid cycle

    body = _fetch_text(url)
    source = body if len(body) >= min_chars else blurb
    if len(source) < 80:                       # nothing worth summarizing
        return None
    provider = _pick_provider(cfg)
    if provider is None:
        return None
    from openai import OpenAI
    kwargs = {"api_key": os.environ[PROVIDERS[provider]["key_env"]]}
    if PROVIDERS[provider]["base_url"]:
        kwargs["base_url"] = PROVIDERS[provider]["base_url"]
    client = OpenAI(**kwargs)
    model = cfg.get("webcontent.summarize_model",
                    cfg.get("ranking.judge_model", "claude-haiku-4-5-20251001"))
    try:
        resp = openai_chat(
            client, model=model, max_tokens=400, temperature=0,
            messages=[{"role": "system", "content": _SUMMARY_SYSTEM},
                      {"role": "user",
                       "content": f"Title: {title}\n\nArticle:\n{source[:6000]}"}])
        text = (resp.choices[0].message.content or "").strip()
    except Exception as e:
        print(f"  [web-content] summarize failed ({type(e).__name__}): {url[:60]}")
        return None
    if not text or text.upper().startswith("SKIP"):
        return None
    return text


def _pick_provider(cfg):
    from ..commentary.generate import AUTO_ORDER, PROVIDERS
    provider = cfg.get("llm.provider", "auto")
    order = AUTO_ORDER if provider == "auto" else [provider]
    for prov in order:
        if prov in PROVIDERS and os.environ.get(PROVIDERS[prov]["key_env"]):
            return prov
    return None


def to_web_post(url: str, brief: str) -> Post:
    """Build a Post-like candidate. author_handle = domain (distinct per blog, for
    cooldown/dedup) but never tagged — is_web_source suppresses the @h/t."""
    domain = urlparse(url).netloc.replace("www.", "") or "web"
    return Post(tweet_id=web_id(url), author_handle=domain, author_name=domain,
                text=brief, created_at=utcnow(), url=url)
