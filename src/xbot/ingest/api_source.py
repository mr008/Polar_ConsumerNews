"""Live X API source (pay-per-use) — reads your home timeline via OAuth 1.0a
user context (the 4 keys in .env). Requires `pip install -e ".[x]"`.

  Endpoint: GET /2/users/:id/timelines/reverse_chronological
  Auth:     OAuth 1.0a (API key/secret + access token/secret) — long-lived.
  Cost:     ~$0.005 per post RETURNED (not per unique post). `since_id` skips
            everything already read; `max_posts_per_day` caps spend per run.
"""
from __future__ import annotations

import os

from ..models import Metrics, Post, utcnow
from .normalize import normalize

API_BASE = "https://api.x.com/2"


class ApiSourceAdapter:
    def __init__(self, max_posts_per_day: int = 120):
        self.ck = os.environ["X_API_KEY"]
        self.cs = os.environ["X_API_SECRET"]
        self.at = os.environ["X_ACCESS_TOKEN"]
        self.ats = os.environ["X_ACCESS_TOKEN_SECRET"]
        self.uid = os.environ["X_USER_ID"]
        self.max = max_posts_per_day

    def fetch_timeline(self, limit: int = 120, since_id: str | None = None) -> list[Post]:
        from requests_oauthlib import OAuth1Session  # lazy import

        limit = min(limit, self.max)
        session = OAuth1Session(self.ck, self.cs, self.at, self.ats)
        url = f"{API_BASE}/users/{self.uid}/timelines/reverse_chronological"
        params = {
            "max_results": min(100, max(5, limit)),
            "tweet.fields": "created_at,public_metrics,lang,referenced_tweets,entities,attachments",
            "expansions": "author_id",
            "user.fields": "public_metrics,username,name",
        }
        # READ-DEDUP: X bills per post RETURNED, so without since_id every run
        # re-buys posts the previous run already paid for. With it, `limit`
        # becomes a safety ceiling instead of a guaranteed spend.
        if since_id:
            params["since_id"] = since_id
        posts: list[Post] = []
        token, pages = None, 0
        while len(posts) < limit and pages < 10:
            if token:
                params["pagination_token"] = token
            else:
                params.pop("pagination_token", None)
            resp = session.get(url, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            users = {u["id"]: u for u in data.get("includes", {}).get("users", [])}
            for t in data.get("data", []):
                posts.append(self._to_post(t, users))
                if len(posts) >= limit:
                    break
            token = data.get("meta", {}).get("next_token")
            pages += 1
            if not token:
                break
        return posts[:limit]

    def fetch_metrics(self, ids: list[str]) -> dict[str, Metrics]:
        """Re-poll public metrics for known tweets so ranking sees live engagement
        (since_id means timeline reads never refresh them). PAID: ~$0.005/post —
        callers keep the id list small (queue + top candidates)."""
        if not ids:
            return {}
        from requests_oauthlib import OAuth1Session  # lazy import

        session = OAuth1Session(self.ck, self.cs, self.at, self.ats)
        resp = session.get(f"{API_BASE}/tweets",
                           params={"ids": ",".join(ids[:100]),
                                   "tweet.fields": "public_metrics"},
                           timeout=30)
        resp.raise_for_status()
        now = utcnow()
        out: dict[str, Metrics] = {}
        for t in resp.json().get("data", []):
            pm = t.get("public_metrics", {})
            out[t["id"]] = Metrics(
                likes=pm.get("like_count", 0), reposts=pm.get("retweet_count", 0),
                replies=pm.get("reply_count", 0), quotes=pm.get("quote_count", 0),
                views=pm.get("impression_count", 0), captured_at=now,
            )
        return out

    @staticmethod
    def _to_post(t: dict, users: dict) -> Post:
        author = users.get(t.get("author_id"), {})
        pm = t.get("public_metrics", {})
        refs = t.get("referenced_tweets", []) or []
        ref_types = {r.get("type"): r.get("id") for r in refs}
        canonical = ref_types.get("retweeted") or ref_types.get("quoted") or t["id"]
        handle = author.get("username", "")
        return normalize(Post(
            tweet_id=t["id"],
            author_handle=handle,
            author_name=author.get("name", handle),
            author_follower_count=author.get("public_metrics", {}).get("followers_count", 0),
            text=t.get("text", ""),
            created_at=t.get("created_at", utcnow().isoformat()),
            url=f"https://x.com/{handle}/status/{t['id']}",
            lang=t.get("lang", "en"),
            is_reply="replied_to" in ref_types,
            is_retweet="retweeted" in ref_types,
            is_quote="quoted" in ref_types,
            has_media="attachments" in t,
            has_link=bool(t.get("entities", {}).get("urls")),
            canonical_id=canonical,
            metrics=Metrics(
                likes=pm.get("like_count", 0),
                reposts=pm.get("retweet_count", 0),
                replies=pm.get("reply_count", 0),
                quotes=pm.get("quote_count", 0),
                views=pm.get("impression_count", 0),
            ),
        ))
