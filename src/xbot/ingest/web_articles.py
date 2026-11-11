"""Online CONTENT source: web search finds tactical blog posts, a cheap model
summarizes each into a teaching brief, and we emit Post-like candidates that flow
through the normal score -> draft -> publish pipeline. Published as an ORIGINAL
post (no quote, no @handle h/t) — the tactic is taught in the bot's own voice.

Cost: the Brave search is FREE (free tier); the only spend is one small summarize
call per NEW article (Haiku, ~$0.003). No X reads. Runs on its own light cadence.
"""
from __future__ import annotations

import base64
import hashlib
import os
import re
import time
import zlib
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


def find_article_candidates(queries, key, *, provider="brave", per_query=15,
                            freshness="", pause=0.0):
    """Run the content queries and return deduped [(url, title, blurb)] for results
    that look like tactical blog articles (junk domains dropped). `freshness`
    limits results by age (see _brave_search); `pause` sleeps between queries to
    stay under the provider's rate limit."""
    if not key or not queries:
        return []
    fetch = wd._PROVIDERS.get(provider)
    if fetch is None:
        print(f"  [web-content] unknown provider '{provider}' — skipped")
        return []
    extra = {"freshness": freshness} if freshness else {}
    out: dict[str, dict] = {}
    for i, q in enumerate(queries):
        if i and pause:
            time.sleep(pause)
        try:
            results = fetch(q, key, per_query, 20.0, **extra)
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


def _fetch_html(url: str, timeout: float = 15.0) -> str:
    """The raw page, or "" on any failure."""
    try:
        import httpx  # lazy: only the live web-content path needs it

        resp = httpx.get(url, timeout=timeout, follow_redirects=True,
                         headers={"User-Agent": "Mozilla/5.0 (compatible; xbot/1.0)"})
        resp.raise_for_status()
        return resp.text
    except Exception:
        return ""


_pages: dict[str, str] = {}      # url -> raw page from this run's reads (see page_read)


def _fetch_text(url: str, timeout: float = 15.0) -> str:
    """Best-effort full-article text (httpx + crude HTML strip). Returns "" on any
    failure — the summarizer then falls back to the search blurb. The raw page is
    held for page_read so an accepted article can be kept for the archive."""
    html = _fetch_html(url, timeout)
    if html:
        _pages[url] = html
    html = re.sub(r"(?is)<(script|style|noscript|nav|footer|header|form)[^>]*>.*?</\1>",
                  " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", text).strip()


def page_read(url: str) -> str:
    """The raw page _fetch_text read for `url` this run ("" if none); handed over
    once."""
    return _pages.pop(url, "")


# The page as it was when we read it, kept in the shared state table so `xbot
# archive` still has the original if the site deletes or locks it before the
# archive gets there. Value = "<YYYY-MM-DD>\n<base64 of zlib>"; the date lets
# collect_web prune old ones (Repository.delete_state_before).
PAGE_KEY = "web_page:"
MAX_PAGE_CHARS = 2_000_000


def save_page(repo, wid: str, html: str, day: str | None = None) -> None:
    packed = base64.b64encode(zlib.compress(html[:MAX_PAGE_CHARS].encode("utf-8"), 9))
    repo.set_state(PAGE_KEY + wid,
                   f"{day or utcnow().date().isoformat()}\n{packed.decode('ascii')}")


def load_page(repo, wid: str) -> str:
    """The saved page for a web candidate id, or "" (none saved, or unreadable)."""
    try:
        _, _, packed = repo.get_state(PAGE_KEY + wid, "").partition("\n")
        return zlib.decompress(base64.b64decode(packed)).decode("utf-8") if packed else ""
    except Exception:
        return ""


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
    Returns "" when the article has nothing to teach (too thin, or the model says
    SKIP) — the caller remembers it and never pays to summarize it again — and
    None when it couldn't be judged right now (no LLM key, LLM error, empty
    reply), so the next run retries it."""
    from ..commentary.generate import PROVIDERS, openai_chat  # lazy: avoid cycle

    body = _fetch_text(url)
    source = body if len(body) >= min_chars else blurb
    if len(source) < 80:                       # nothing worth summarizing
        return ""
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
    if not text:
        return None
    if text.upper().startswith("SKIP"):
        return ""
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
