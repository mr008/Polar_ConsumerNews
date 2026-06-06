"""Try remaining drafts in QUOTE-ONLY mode (no standalone fallback). Stop at the
first that successfully embeds a quote — verifies the quote-tweet path works."""
from xbot.cli import load_dotenv

load_dotenv()
import os

from requests_oauthlib import OAuth1Session

from xbot.config import db_path, load_config
from xbot.storage import SqliteRepository

cfg = load_config("config.yaml")
repo = SqliteRepository(db_path(cfg))
session = OAuth1Session(os.environ["X_API_KEY"], os.environ["X_API_SECRET"],
                        os.environ["X_ACCESS_TOKEN"], os.environ["X_ACCESS_TOKEN_SECRET"])

for did in [4, 8, 5, 6]:
    row = repo.get_draft(did)
    if not row:
        print(f"#{did}: not found"); continue
    draft, post = row
    if repo.has_posted(post.tweet_id):
        print(f"#{did}: already posted, skip"); continue
    resp = session.post("https://api.x.com/2/tweets",
                        json={"text": draft.commentary, "quote_tweet_id": post.tweet_id},
                        timeout=30)
    if resp.status_code in (200, 201):
        our_id = resp.json().get("data", {}).get("id", "")
        repo.log_posted(post.tweet_id, our_id, post.author_handle, post.text, draft.commentary)
        repo.set_draft_status(did, "posted")
        print(f"#{did}: QUOTE POSTED  @{post.author_handle}")
        print(f"  https://x.com/polarbe12138/status/{our_id}")
        break
    if resp.status_code == 403 and "Quoting this post is not allowed" in resp.text:
        print(f"#{did}: @{post.author_handle} restricts quotes -> skip")
        continue
    print(f"#{did}: error {resp.status_code}: {resp.text[:160]}")
    break
else:
    print("No quote-friendly draft found in this batch.")
