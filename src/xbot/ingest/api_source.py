"""Live X API source (pay-per-use) — reads your home timeline (or a curated List)
via OAuth 1.0a user context (the 4 keys in .env). Requires `pip install -e ".[x]"`.

  Endpoint: GET /2/users/:id/timelines/reverse_chronological  (home, default)
            GET /2/lists/:id/tweets                            (when list_id set)
  Auth:     OAuth 1.0a (API key/secret + access token/secret) — long-lived.
  Cost:     ~$0.005 per post RETURNED (not per unique post). `max_posts_per_day`
            caps spend per run.

DEDUP differs by source. Home supports `since_id` SERVER-SIDE, so the API returns
only genuinely-new posts and you pay only for those. The List Tweets endpoint has
NO `since_id` — it returns a full page every call — so we recreate the dedup
CLIENT-SIDE: fetch newest-first and STOP as soon as a tweet we've already stored
appears (snowflake ids are monotonic). With a small page size this keeps List
spend ≈ new-posts-per-run + at most one partial page of overlap.
"""
from __future__ import annotations

import os

from ..models import Metrics, Post, utcnow
from .normalize import normalize

API_BASE = "https://api.x.com/2"


_FIELDS = {
    "tweet.fields": "created_at,public_metrics,lang,referenced_tweets,entities,attachments,reply_settings",
    "expansions": "author_id",
    "user.fields": "public_metrics,username,name",
}


class ApiSourceAdapter:
    def __init__(self, max_posts_per_day: int = 120, list_id: str = "",
                 list_page_size: int = 25):
        self.ck = os.environ["X_API_KEY"]
        self.cs = os.environ["X_API_SECRET"]
        self.at = os.environ["X_ACCESS_TOKEN"]
        self.ats = os.environ["X_ACCESS_TOKEN_SECRET"]
        self.uid = os.environ["X_USER_ID"]
        self.max = max_posts_per_day
        self.list_id = (list_id or "").strip()
        self.list_page_size = list_page_size

    def _session(self):
        from requests_oauthlib import OAuth1Session  # lazy import
        return OAuth1Session(self.ck, self.cs, self.at, self.ats)

    def fetch_timeline(self, limit: int = 120, since_id: str | None = None) -> list[Post]:
        limit = min(limit, self.max)
        if self.list_id:
            return self._fetch_list(limit, since_id)
        return self._fetch_home(limit, since_id)

    def _fetch_home(self, limit: int, since_id: str | None) -> list[Post]:
        session = self._session()
        url = f"{API_BASE}/users/{self.uid}/timelines/reverse_chronological"
        params = {"max_results": min(100, max(5, limit)), **_FIELDS}
        # READ-DEDUP (server-side): X bills per post RETURNED, so without since_id
        # every run re-buys posts already paid for. With it, `limit` is a safety
        # ceiling instead of guaranteed spend.
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

    def _fetch_list(self, limit: int, since_id: str | None) -> list[Post]:
        """Read a curated List. No server-side since_id, so we page newest-first
        and STOP at the first already-seen tweet (client-side dedup). Page size is
        small so the unavoidable overlap (the tail of the last page) stays tiny."""
        session = self._session()
        url = f"{API_BASE}/lists/{self.list_id}/tweets"
        page = min(100, max(5, self.list_page_size))
        params = {"max_results": page, **_FIELDS}
        try:
            since = int(since_id) if since_id else None
        except (TypeError, ValueError):
            since = None
        posts: list[Post] = []
        token, pages = None, 0
        stop = False
        while not stop and len(posts) < limit and pages < 10:
            if token:
                params["pagination_token"] = token
            else:
                params.pop("pagination_token", None)
            resp = session.get(url, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            users = {u["id"]: u for u in data.get("includes", {}).get("users", [])}
            for t in data.get("data", []):
                # Already-seen boundary: ids are monotonic, list is newest-first,
                # so the first id <= since_id means everything after is old. Stop.
                if since is not None:
                    try:
                        if int(t["id"]) <= since:
                            stop = True
                            break
                    except (KeyError, ValueError):
                        pass
                posts.append(self._to_post(t, users))
                if len(posts) >= limit:
                    break
            token = data.get("meta", {}).get("next_token")
            pages += 1
            if not token:
                break
        return posts[:limit]

    def fetch_discovery(self, target_authors: int = 50, per_author_cap: int = 2,
                        max_reads: int = 250, since_id: str | None = None
                        ) -> tuple[list[Post], int]:
        """Author-FAIR home-feed sample for discovery (so accounts you don't yet
        read on the List can be scored + promoted). Pages newest-first but keeps at
        most `per_author_cap` posts per author, so one flooder can't dominate — the
        "window" is `target_authors` DISTINCT accounts, not a raw post count. Stops
        at target_authors covered OR max_reads posts read. Returns (kept, n_read):
        n_read is the BILLED count (every post a page returns, kept or not), so the
        breaker tracks true spend. The cap shrinks what we JUDGE, not what we pay."""
        session = self._session()
        url = f"{API_BASE}/users/{self.uid}/timelines/reverse_chronological"
        params = {"max_results": 100, **_FIELDS}
        if since_id:
            params["since_id"] = since_id
        kept: list[Post] = []
        per_author: dict[str, int] = {}
        read, token, pages = 0, None, 0
        while pages < 15 and read < max_reads and len(per_author) < target_authors:
            if token:
                params["pagination_token"] = token
            else:
                params.pop("pagination_token", None)
            resp = session.get(url, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            users = {u["id"]: u for u in data.get("includes", {}).get("users", [])}
            batch = data.get("data", [])
            read += len(batch)                  # billed for the whole page
            for t in batch:
                post = self._to_post(t, users)
                h = post.author_handle.lower()
                if per_author.get(h, 0) < per_author_cap:   # keep-cap is per author
                    kept.append(post)
                    per_author[h] = per_author.get(h, 0) + 1
            token = data.get("meta", {}).get("next_token")
            pages += 1
            if not token:
                break
        return kept, read

    # ---------------- List administration (auto-update + setup) ---------------
    def resolve_user_ids(self, handles: list[str]) -> dict[str, str]:
        """Map @handles -> numeric user ids (up to 100/call). Cheap owned read."""
        out: dict[str, str] = {}
        session = self._session()
        clean = [h.lstrip("@") for h in handles if h.strip()]
        for i in range(0, len(clean), 100):
            chunk = clean[i:i + 100]
            resp = session.get(f"{API_BASE}/users/by",
                               params={"usernames": ",".join(chunk)}, timeout=30)
            resp.raise_for_status()
            for u in resp.json().get("data", []):
                out[u["username"].lower()] = u["id"]
        return out

    def create_list(self, name: str, private: bool = True) -> str:
        resp = self._session().post(f"{API_BASE}/lists",
                                    json={"name": name[:25], "private": private},
                                    timeout=30)
        resp.raise_for_status()
        return resp.json()["data"]["id"]

    def list_members(self, list_id: str) -> list[dict]:
        """Current members as [{id, handle}] — the auto-updater diffs by handle."""
        session = self._session()
        out: list[dict] = []
        token = None
        while True:
            params = {"max_results": 100, "user.fields": "username"}
            if token:
                params["pagination_token"] = token
            resp = session.get(f"{API_BASE}/lists/{list_id}/members",
                               params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            out.extend({"id": u["id"], "handle": u.get("username", "")}
                       for u in data.get("data", []))
            token = data.get("meta", {}).get("next_token")
            if not token:
                return out

    def add_list_member(self, list_id: str, user_id: str) -> bool:
        resp = self._session().post(f"{API_BASE}/lists/{list_id}/members",
                                    json={"user_id": user_id}, timeout=30)
        resp.raise_for_status()
        return bool(resp.json().get("data", {}).get("is_member"))

    def remove_list_member(self, list_id: str, user_id: str) -> bool:
        resp = self._session().delete(f"{API_BASE}/lists/{list_id}/members/{user_id}",
                                      timeout=30)
        resp.raise_for_status()
        return not resp.json().get("data", {}).get("is_member", True)

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

    def fetch_me(self) -> dict:
        """Own-account public metrics (~$0.001 owned read) — the follower trend
        is THE success metric for the growth work; everything else is a proxy."""
        from requests_oauthlib import OAuth1Session  # lazy import

        session = OAuth1Session(self.ck, self.cs, self.at, self.ats)
        resp = session.get(f"{API_BASE}/users/me",
                           params={"user.fields": "public_metrics,username"},
                           timeout=30)
        resp.raise_for_status()
        data = resp.json().get("data", {})
        pm = data.get("public_metrics", {})
        return {
            "handle": data.get("username", ""),
            "followers": pm.get("followers_count", 0),
            "following": pm.get("following_count", 0),
            "tweets": pm.get("tweet_count", 0),
        }

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
            reply_settings=t.get("reply_settings"),
            metrics=Metrics(
                likes=pm.get("like_count", 0),
                reposts=pm.get("retweet_count", 0),
                replies=pm.get("reply_count", 0),
                quotes=pm.get("quote_count", 0),
                views=pm.get("impression_count", 0),
            ),
        ))
