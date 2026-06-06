"""Verify OAuth 1.0a user-context auth: who am I + can I read my home timeline."""
from xbot.cli import load_dotenv

load_dotenv()
import os

from requests_oauthlib import OAuth1Session

s = OAuth1Session(
    os.environ["X_API_KEY"], os.environ["X_API_SECRET"],
    os.environ["X_ACCESS_TOKEN"], os.environ["X_ACCESS_TOKEN_SECRET"],
)

me = s.get("https://api.x.com/2/users/me")
print("GET /2/users/me ->", me.status_code)
print(me.text[:300])

if me.status_code == 200:
    uid = me.json()["data"]["id"]
    r = s.get(
        f"https://api.x.com/2/users/{uid}/timelines/reverse_chronological",
        params={
            "max_results": 5,
            "tweet.fields": "public_metrics,created_at",
            "expansions": "author_id",
            "user.fields": "username,public_metrics",
        },
    )
    print("\nGET home timeline ->", r.status_code)
    data = r.json()
    users = {u["id"]: u for u in data.get("includes", {}).get("users", [])}
    rows = data.get("data", [])
    print(f"got {len(rows)} posts from your feed:")
    for t in rows:
        au = users.get(t.get("author_id"), {})
        pm = t.get("public_metrics", {})
        text = " ".join(t["text"].split())[:70]
        print(f"  @{au.get('username','?'):<16} likes={pm.get('like_count',0):<5} {text}")
    if not rows:
        print(data)
