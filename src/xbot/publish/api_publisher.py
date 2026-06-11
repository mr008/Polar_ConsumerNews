"""Live X API publisher (pay-per-use) — posts via OAuth 1.0a.

  Endpoint: POST /2/tweets
  Auth:     OAuth 1.0a user context (the 4 keys in .env). App must be Read+Write.
  Cost:     $0.015 per post. A URL in the text → $0.20, so the main post and all
            thread parts refuse URLs; only the attribution self-reply carries one.

publish() posts the whole item: main post, then thread parts chained as
self-replies, then the optional attribution reply (the hidden source link).
Partial-failure rule: if a chained part fails, stop the chain and report —
never delete the already-posted hook.
"""
from __future__ import annotations

import os

from ..models import Draft, Post
from .publisher import (URL_RE, attribution_text, compose_text,
                        wants_attribution_reply)

API_BASE = "https://api.x.com/2"


class ApiPublisher:
    def __init__(self, cfg=None):
        self.cfg = cfg
        self.ck = os.environ["X_API_KEY"]
        self.cs = os.environ["X_API_SECRET"]
        self.at = os.environ["X_ACCESS_TOKEN"]
        self.ats = os.environ["X_ACCESS_TOKEN_SECRET"]

    def _session(self):
        from requests_oauthlib import OAuth1Session  # lazy import
        return OAuth1Session(self.ck, self.cs, self.at, self.ats)

    def _post(self, session, payload: dict) -> dict:
        resp = session.post(f"{API_BASE}/tweets", json=payload, timeout=30)
        if resp.status_code == 403:
            # RuntimeError (not SystemExit) so the orchestrator can skip to the
            # next-best draft instead of the whole run dying.
            raise RuntimeError(
                "403 from POST /2/tweets — check the app's Read+Write permission "
                "and regenerate the Access Token if needed.\n" + resp.text[:300]
            )
        resp.raise_for_status()
        return resp.json().get("data", {})

    def reply(self, text: str, in_reply_to_tweet_id: str) -> dict:
        data = self._post(self._session(), {
            "text": text,
            "reply": {"in_reply_to_tweet_id": in_reply_to_tweet_id},
        })
        return {"ok": True, "id": data.get("id", "")}

    def publish(self, draft: Draft, post: Post) -> dict:
        session = self._session()
        text, fmt = compose_text(draft, post, self.cfg)

        if fmt == "quote":
            return self._publish_quote(session, draft, post)

        if fmt == "mention" and URL_RE.search(text):
            raise ValueError("main post contains a URL — refusing (buried + 13x cost)")

        # Main post (mention: clean text; link: legacy URL-appended standalone).
        if fmt == "link":
            main_id = self._publish_link(session, draft, text)
            if not main_id:
                return {"ok": False, "id": "", "mode": "link"}
        else:
            main_id = self._post(session, {"text": text}).get("id", "")

        # Thread parts + attribution reply, chained under the main post. A part
        # failure stops the chain but never invalidates the already-posted hook.
        thread_ids, prev_id = [], main_id
        chain = [p for p in draft.parts if p.strip()]
        if wants_attribution_reply(self.cfg) and post.url:
            chain.append(attribution_text(post))
        for part in chain:
            try:
                prev_id = self.reply(part, prev_id).get("id", "") or prev_id
                thread_ids.append(prev_id)
            except Exception as e:
                print(f"  [publish] thread part failed ({type(e).__name__}: "
                      f"{str(e)[:120]}) — hook stays up, chain stopped")
                break

        return {"ok": True, "id": main_id, "mode": fmt, "thread_ids": thread_ids}

    def _publish_link(self, session, draft: Draft, text: str) -> str:
        """Legacy link mode. X sometimes rejects tweet-URLs like a quote — fall
        back to the bare commentary rather than failing the window."""
        resp = session.post(f"{API_BASE}/tweets", json={"text": text}, timeout=30)
        if resp.status_code in (200, 201):
            return resp.json().get("data", {}).get("id", "")
        body = resp.text[:200]
        resp = session.post(f"{API_BASE}/tweets",
                            json={"text": draft.commentary}, timeout=30)
        if resp.status_code in (200, 201):
            print(f"  [publish] link rejected ({body}); posted WITHOUT the link")
            return resp.json().get("data", {}).get("id", "")
        raise RuntimeError(f"link post failed: {body}\nbare post failed: {resp.text[:200]}")

    def _publish_quote(self, session, draft: Draft, post: Post) -> dict:
        if URL_RE.search(draft.commentary):
            raise ValueError("commentary contains a URL — refusing (13x cost + voice rule)")
        # For retweets the timeline id is the RT wrapper (never quotable) — quote the original.
        quote_id = post.canonical_id if post.is_retweet else post.tweet_id
        resp = session.post(f"{API_BASE}/tweets",
                            json={"text": draft.commentary, "quote_tweet_id": quote_id},
                            timeout=30)
        quoted = True
        # If the author restricts who can quote/reply, fall back to a standalone post.
        if resp.status_code == 403 and "Quoting this post is not allowed" in resp.text:
            resp = session.post(f"{API_BASE}/tweets",
                                json={"text": draft.commentary}, timeout=30)
            quoted = False
        if resp.status_code == 403:
            raise RuntimeError(
                "403 from POST /2/tweets — the app behind your Access Token is likely "
                "Read-only. Set THAT app to 'Read and Write', regenerate the Access "
                "Token + Secret, update .env, and retry.\n" + resp.text[:300]
            )
        resp.raise_for_status()
        data = resp.json().get("data", {})
        return {"ok": True, "id": data.get("id", ""), "mode": "quote", "quoted": quoted}
