"""Open-web discovery: find prominent teacher ACCOUNTS via a web-search API,
without paying X read costs for the search itself.

The weekly job runs a few curated queries (e.g. "best AI UGC creators to follow"),
scrapes X/Twitter @handles out of the result URLs + snippets (the listicles,
"who to follow" threads, and profile/status links that web search surfaces well),
and hands the deduped handle list back to the orchestrator. New handles are then
vetted by reading a few of each account's recent posts via the X API (capped) and
scored by the existing teaching judge, so only consistent teachers reach the List.

Search is FREE (Brave free tier: 2,000 queries/mo). Only the vetting reads are
billed, and those are hard-capped in config. Provider-pluggable: only Brave is
implemented now; Google CSE / SerpAPI could slot in behind `search_provider`.
"""
from __future__ import annotations

import re

# X handles: 1-15 chars, letters/digits/underscore. Reserved path segments that
# look like handles in a URL but aren't accounts (x.com/search, /i/..., /hashtag).
_HANDLE_RE = r"[A-Za-z0-9_]{1,15}"
_RESERVED = {
    "i", "intent", "home", "search", "hashtag", "explore", "notifications",
    "messages", "settings", "compose", "share", "login", "signup", "tos",
    "privacy", "about", "status", "statuses", "help", "download", "jobs",
    "followers", "following", "lists", "list", "communities", "moments",
    "topics", "bookmarks", "verified", "premium", "media", "photo", "video",
    "live", "spaces", "broadcasts", "who_to_follow", "account", "twitterapi",
    "en", "es", "fr", "de", "www",
}

# Handles inside x.com / twitter.com / nitter URLs (profile OR /status/ links —
# the author handle is the first path segment either way).
_URL_HANDLE_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:x|twitter|nitter|fixupx|vxtwitter)\.com/(" + _HANDLE_RE + r")\b",
    re.IGNORECASE,
)
# Bare @mentions in titles/snippets ("follow @levelsio and @gregisenberg").
_MENTION_RE = re.compile(r"(?<![A-Za-z0-9_])@(" + _HANDLE_RE + r")\b")
_TAG_RE = re.compile(r"<[^>]+>")


def extract_handles(results: list[dict]) -> list[str]:
    """Pull candidate X @handles from search results (url + title + description),
    lowercased, deduped, order-preserving. Drops reserved path segments and
    anything that isn't a valid 1-15 char handle. Purely lexical — the teaching
    judge is what actually vets quality downstream."""
    seen: dict[str, None] = {}
    for r in results or []:
        url = r.get("url", "") or ""
        text = _TAG_RE.sub(" ", f"{r.get('title', '')} {r.get('description', '')}")
        for m in _URL_HANDLE_RE.findall(url) + _URL_HANDLE_RE.findall(text):
            _add(seen, m)
        for m in _MENTION_RE.findall(text):
            _add(seen, m)
    return list(seen)


def _add(seen: dict, handle: str) -> None:
    h = handle.lower().lstrip("@")
    if h and h not in _RESERVED and re.fullmatch(_HANDLE_RE, h):
        seen.setdefault(h, None)


def search_handles(queries: list[str], api_key: str, *, provider: str = "brave",
                   per_query: int = 20, timeout: float = 20.0) -> list[str]:
    """Run each query through the web-search provider and return deduped candidate
    handles across all queries. Never raises on a single bad query — logs and moves
    on, so one flaky call can't sink the weekly job."""
    if not api_key or not queries:
        return []
    fetch = _PROVIDERS.get(provider)
    if fetch is None:
        print(f"  [web-discovery] unknown provider '{provider}' — skipped")
        return []
    seen: dict[str, None] = {}
    for q in queries:
        try:
            results = fetch(q, api_key, per_query, timeout)
        except Exception as e:  # network / rate-limit / bad JSON — skip this query
            print(f"  [web-discovery] query failed ({type(e).__name__}): {q[:60]}")
            continue
        for h in extract_handles(results):
            seen.setdefault(h, None)
    return list(seen)


def _brave_search(query: str, api_key: str, count: int, timeout: float) -> list[dict]:
    """Brave Web Search API -> [{url, title, description}]. count is capped at 20
    (Brave's per-call max). Free tier: 2,000 queries/mo, 1 query/sec."""
    import httpx  # lazy — keeps the dry-run path dependency-light

    resp = httpx.get(
        "https://api.search.brave.com/res/v1/web/search",
        params={"q": query, "count": max(1, min(20, count))},
        headers={"Accept": "application/json", "X-Subscription-Token": api_key},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json().get("web", {}).get("results", []) or []


_PROVIDERS = {"brave": _brave_search}
