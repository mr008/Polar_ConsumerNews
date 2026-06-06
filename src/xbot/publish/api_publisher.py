"""Live X API publisher (pay-per-use) — posts a quote tweet via OAuth 1.0a.

  Endpoint: POST /2/tweets
  Auth:     OAuth 1.0a user context (the 4 keys in .env). App must be Read+Write.
  Body:     {"text": <commentary>, "quote_tweet_id": <source tweet id>}
  Cost:     $0.015 per post. A URL in the text → $0.20, so we refuse URLs.
"""
from __future__ import annotations

import os

from ..models import Draft, Post

API_BASE = "https://api.x.com/2"


class ApiPublisher:
    def __init__(self):
        self.ck = os.environ["X_API_KEY"]
        self.cs = os.environ["X_API_SECRET"]
        self.at = os.environ["X_ACCESS_TOKEN"]
        self.ats = os.environ["X_ACCESS_TOKEN_SECRET"]

    def publish(self, draft: Draft, post: Post) -> dict:
        from requests_oauthlib import OAuth1Session  # lazy import

        if "http://" in draft.commentary or "https://" in draft.commentary:
            raise ValueError("commentary contains a URL — refusing (13x cost + voice rule)")

        session = OAuth1Session(self.ck, self.cs, self.at, self.ats)
        resp = session.post(f"{API_BASE}/tweets",
                            json={"text": draft.commentary, "quote_tweet_id": post.tweet_id},
                            timeout=30)
        quoted = True

        # If the author restricts who can quote/reply, fall back to a standalone post.
        if resp.status_code == 403 and "Quoting this post is not allowed" in resp.text:
            resp = session.post(f"{API_BASE}/tweets",
                                json={"text": draft.commentary}, timeout=30)
            quoted = False

        if resp.status_code == 403:
            raise SystemExit(
                "403 from POST /2/tweets — the app behind your Access Token is likely "
                "Read-only. Set THAT app to 'Read and Write', regenerate the Access "
                "Token + Secret, update .env, and retry.\n" + resp.text[:300]
            )
        resp.raise_for_status()
        data = resp.json().get("data", {})
        return {"ok": True, "id": data.get("id", ""), "quoted": quoted}
