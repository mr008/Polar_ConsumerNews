"""Light normalization shared by all sources."""
from __future__ import annotations

import re

from ..models import Post

_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def normalize(post: Post) -> Post:
    """Fill derived flags and collapse to a canonical id."""
    if not post.has_link:
        post.has_link = bool(_URL_RE.search(post.text or ""))
    # Retweets/quotes amplify an original; collapse to it when we know it.
    if (post.is_retweet or post.is_quote) and post.canonical_id == post.tweet_id:
        # In live data the original id comes from the API expansion; sample data
        # may set canonical_id explicitly. Leave as-is otherwise.
        pass
    return post
