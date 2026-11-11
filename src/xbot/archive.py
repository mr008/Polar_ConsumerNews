"""`xbot archive` — a LOCAL copy of everything we published, each next to the
original it came from: full source text plus its pictures saved as files.

  <out>/archive.db      SQLite: `items` (one row per published post) + `media`
  <out>/media/<id>/...  the picture files; `media.path` is relative to <out>

READ-ONLY against production: it only SELECTs from the shared repo and never
posts. Re-runnable: items already fetched are skipped, so a re-run pays only for
what is new. Tweets cost one X read each (~$0.005, plus one per quoted post);
web articles and all picture downloads are free. Stdlib only.
"""
from __future__ import annotations

import html as html_lib
import json
import re
import sqlite3
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

from .models import WEB_ID_PREFIX, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,          -- posted_log.id in production
    posted_at TEXT, posted_at_pt TEXT,
    our_tweet_id TEXT, our_url TEXT, our_text TEXT,
    kind TEXT,                       -- 'tweet' | 'web'
    source_id TEXT, source_url TEXT, source_author TEXT,
    source_title TEXT, source_created_at TEXT,
    source_text TEXT,                -- the FULL original once fetched
    stored_text TEXT,                -- what production held (cut-off tweet / our summary)
    quoted_id TEXT, quoted_author TEXT, quoted_text TEXT,
    status TEXT,                     -- pending | ok | gone | error
    status_note TEXT, fetched_at TEXT,
    views INTEGER, likes INTEGER, reposts INTEGER, replies INTEGER, quotes INTEGER
);
CREATE TABLE IF NOT EXISTS media (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL,
    role TEXT,                       -- source | quoted | article
    kind TEXT,                       -- photo | image | video / animated_gif (the
                                     -- preview picture) | video_file (the mp4)
    url TEXT, path TEXT, alt TEXT,   -- path '' = download failed
    UNIQUE(item_id, url)
);
"""

MAX_BYTES = 4 * 1024 ** 3             # whole videos are saved too; a download
                                      # that reaches the cap is treated as failed
MAX_ARTICLE_IMAGES = 30
MAX_VIDEO_BITRATE = 2_500_000         # ~720p; 1080p+ files run to gigabytes
_EXT = {"image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png",
        "image/gif": "gif", "image/webp": "webp", "image/avif": "avif",
        "image/svg+xml": "svg", "video/mp4": "mp4"}


# ------------------------------------------------------------------ web pages
_SKIP = {"script", "style", "noscript", "nav", "footer", "form", "aside", "svg",
         "iframe"}
_BLOCK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr",
          "blockquote", "section", "article", "pre", "figcaption"}


class _ArticleParser(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base = base_url
        self.skip = self.article = self.link = self.button = 0
        self.button_text: list[str] = []
        self.in_title = False
        self.title = self.og_title = self.og_image = ""
        self.body: list[str] = []
        self.art: list[str] = []
        self.images: list[tuple[bool, str]] = []   # (inside <article>, url)

    def _emit(self, s: str) -> None:
        self.body.append(s)
        if self.article:
            self.art.append(s)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta":
            prop, content = a.get("property") or a.get("name") or "", a.get("content") or ""
            if prop == "og:title":
                self.og_title = content.strip()
            elif prop == "og:image" and content:
                self.og_image = urljoin(self.base, content.strip())
            return
        if tag == "title":
            self.in_title = True
        if tag in _SKIP or (tag == "header" and not self.article):
            self.skip += 1
            return
        if tag == "article":
            self.article += 1
        if self.skip:
            return
        if tag == "button":                      # kept only when it is a question
            self.button += 1
        if self.button:
            return
        if tag == "a":
            self.link += 1
        if tag == "img":
            self._image(a)
        if tag in _BLOCK:
            self._emit("\n")

    def _image(self, a: dict) -> None:
        src = a.get("src") or a.get("data-src") or a.get("data-lazy-src") or ""
        if (not src or src.startswith("data:")) and a.get("srcset"):
            src = a["srcset"].split(",")[0].split()[0]
        if not src or src.startswith("data:"):
            return
        for dim in ("width", "height"):          # 1x1 tracking pixels
            if (a.get(dim) or "").strip() in ("0", "1", "2"):
                return
        self.images.append((bool(self.article), urljoin(self.base, src.strip())))

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        if tag in _SKIP or (tag == "header" and not self.article):
            self.skip = max(0, self.skip - 1)
            return
        if tag == "button" and self.button and not self.skip:
            self.button -= 1
            if not self.button:
                text = " ".join("".join(self.button_text).split())
                self.button_text = []
                if text.endswith("?"):           # an FAQ accordion's question
                    self._emit("\n" + text + "\n")
            return
        if self.button:
            return
        if tag == "a":
            self.link = max(0, self.link - 1)
        if tag == "h1" and not self.skip:
            self._emit(_H1)
        if tag in ("td", "th") and not self.skip:
            self._emit(" | ")
        if tag in _BLOCK and not self.skip:
            self._emit("\n")
        if tag == "article":
            self.article = max(0, self.article - 1)

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        elif self.skip:
            return
        elif self.button:
            self.button_text.append(data)
        else:
            self._emit(_LINK + data + _LINK_END if self.link else data)


# Markers the parser leaves in the text so _clean can judge each line.
_LINK, _LINK_END, _H1 = "\x01", "\x02", "\x03"
_MARKS = re.compile("[\x01\x02\x03]")
_LINK_RUN = re.compile("\x01([^\x02]*)\x02")

# Lines that are page furniture wherever they appear.
_JUNK_LINE = re.compile(
    r"^(table of contents|contents|on this page:?|in this article:?|share( this)?( article| post)?:?"
    r"|\d+ min(ute)?s?( read)?|copy link|learn more|read more|skip to content"
    r"|listen to this article|toggle|follow|subscribe|sign up|share on \w+)$", re.I)
# Byline rows straight under the headline.
_META_LINE = re.compile(
    r"^(written by|posted by|author|published|updated|last updated|categories|category"
    r"|tags|filed under|posted (in|on))\b", re.I)
_MONTHS = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
# Date / author / reading-time rows straight under the headline.
_BYLINE = re.compile(
    rf"^({_MONTHS} \d{{1,2}},? \d{{4}}|\d{{1,2}} {_MONTHS},? \d{{4}}|\d{{1,4}}[/.-]\d{{1,2}}[/.-]\d{{1,4}}"
    r"|sections|source: .*|.*\d+ min(ute)?s? read.*)$", re.I)
_BY_NAME = re.compile(r"^[Bb]y [A-Z](?:(?![.!?](?: |$)).)*$")
# Newsletter prompts wherever they appear.
_PROMPT = re.compile(r"subscribe for free|thanks for reading\b.*subscribe|unsubscribe in\b", re.I)
# A short line like these, past the middle of the page, is where the article ends.
_TAIL_LINE = re.compile(
    r"^(leave a (reply|comment)|related (posts|articles|reading|stories|content)"
    r"|keep (learning|reading)|you (may|might) also (like|enjoy)|(article )?written by"
    r"|about the author|never miss|reader interactions|subscribe to|join our newsletter"
    r"|more (from|articles|posts|stories)|recommended (for you|reading|articles|posts)"
    r"|read next|up next|\d* ?comments?|share this (article|post)|continue reading)\b", re.I)


def _rows(chunks: list[str]) -> list[tuple[str, bool, float]]:
    """(text, is the headline, share of the text that is a link) per non-empty line."""
    rows = []
    for raw in "".join(chunks).split("\n"):
        text = re.sub(r"[ \t\r\f\v]+", " ", _MARKS.sub("", raw)).strip()
        text = re.sub(r"^(\| ?)+|( ?\|)+$", "", text).strip()      # table row edges
        if text:
            linked = sum(len(m.strip()) for m in _LINK_RUN.findall(raw))
            rows.append((text, _H1 in raw, linked / len(text)))
    return rows


def _split_at_headline(rows: list) -> tuple[list, list]:
    """(rows above and including the <h1>, rows after it). Nothing counts as
    'above' when most of the page's text sits there."""
    cut = next((i + 1 for i, r in enumerate(rows) if r[1]), 0)
    total = sum(len(r[0]) for r in rows)
    if sum(len(r[0]) for r in rows[cut:]) * 2 < total:
        cut = 0
    return rows[:cut], rows[cut:]


def _key(line: str) -> str:
    return re.sub(r"^[-*•·]\s*", "", line).strip().casefold()


def _tidy(lines: list[str]) -> str:
    """Drop page furniture from an article's lines: stray labels, bylines under
    the headline, a table of contents that repeats the headings, and everything
    from the author box / related posts / comments on."""
    total = sum(len(ln) for ln in lines)
    later: dict[str, int] = {}                   # last position of each short line
    for n, ln in enumerate(lines):
        if len(ln) < 120:
            later[_key(ln)] = n
    out: list[str] = []
    seen = 0
    for n, ln in enumerate(lines):
        seen += len(ln)
        bare = re.sub(r"^[-*•·]\s*", "", ln).strip()
        if len(bare) < 60 and seen * 2 > total and _TAIL_LINE.match(bare):
            break
        if not _norm(bare) or _JUNK_LINE.match(bare):
            continue
        if len(bare) < 200 and _PROMPT.search(bare):
            continue
        if len(out) < 6 and len(bare) < 160 and (
                _META_LINE.match(bare) or _BYLINE.match(bare)
                or (len(bare) < 60 and _BY_NAME.match(bare))):
            continue
        if n * 3 < len(lines) and later.get(_key(ln), n) > n:
            continue                             # menu entry repeated by its heading
        out.append(ln)
    while len(out) > 1 and (
            (len(out[-1]) < 60 and out[-1][-1] not in ".!?…”\"')")
            or (len(out[-1]) < 120 and out[-1].endswith("?"))):
        out.pop()                 # dangling label, tag, counter or unanswered question
    return "\n".join(out)


def _library_text(html: str) -> str:
    """Main text via trafilatura (optional `archive` extra); '' when not installed."""
    try:
        import trafilatura
    except ImportError:
        return ""
    try:
        return trafilatura.extract(html, include_comments=False) or ""
    except Exception:
        return ""


def _norm(line: str) -> str:
    return re.sub(r"\W+", "", line).casefold()


def _article_span(lines: list[str], lib: str) -> list[str]:
    """The stretch of `lines` between the first and last line the article
    library also kept: it is good at finding where an article starts and ends,
    while our own reader keeps the lists and captions in between."""
    kept = {_norm(ln) for ln in lib.split("\n")} - {""}
    hits = [i for i, ln in enumerate(lines) if _norm(ln) in kept]
    if not hits:
        return lines
    end = hits[-1] + 1
    faq = next((i for i in range(end, min(end + 3, len(lines)))
                if _FAQ_LINE.match(lines[i])), None)
    if faq is not None:                          # it stopped short of the FAQ:
        last_q = None                            # keep through the last question's answer
        for i in range(faq + 1, len(lines)):
            if lines[i].endswith("?"):
                last_q = i
            elif i - (last_q or faq) > 4:
                break
        if last_q is not None:
            end = min(last_q + 2, len(lines))
    span = lines[hits[0]:end]
    return span if sum(map(len, span)) * 4 >= sum(map(len, lines)) else lines


_FAQ_LINE = re.compile(r"^(frequently asked questions|faqs?|common questions)\b", re.I)
_NOT_A_POST = re.compile(r"\.(png|jpe?g|gif|webp|svg|css|js|xml|pdf|zip|mp4)$", re.I)


def sibling_url(html: str, url: str) -> str | None:
    """Another post in the same section of the site (same folder, a slug of
    several words), or None. Whatever both pages show is site furniture."""
    here = urlparse(url)
    path = here.path.rstrip("/")
    folder = path.rsplit("/", 1)[0] + "/"
    for href in re.findall(r'<a\b[^>]*?href="([^"#?]+)', html):
        full = urljoin(url, href)
        there = urlparse(full)
        other = there.path.rstrip("/")
        if (there.netloc == here.netloc and other != path and other.startswith(folder)
                and other.count("/") == path.count("/")
                and other.rsplit("/", 1)[-1].count("-") >= 3 and not _NOT_A_POST.search(other)):
            return full
    return None


def _drop_shared(lines: list[str], sibling_html: str) -> list[str]:
    """Remove what another post on the same site also shows: any long line, and
    everything from a block of three shared lines in the second half when most of
    what follows is shared too (a directory, a footer). Short shared lines alone
    are just common headings."""
    p = _ArticleParser("")
    p.feed(sibling_html)
    p.close()
    other = {_norm(t) for t, _, _ in _rows(p.body)} - {""}
    shared = [_norm(ln) in other for ln in lines]
    total, seen = sum(map(len, lines)), 0
    for i, ln in enumerate(lines):
        seen += len(ln)
        if (seen * 2 > total and len(shared) - i >= 3 and all(shared[i:i + 3])
                and sum(shared[i:]) * 10 >= len(shared[i:]) * 7):
            lines, shared = lines[:i], shared[:i]
            break
    return [ln for ln, same in zip(lines, shared) if not (same and len(ln) >= 50)]


def extract_article(html: str, base_url: str, sibling_html: str | None = None) -> dict:
    """{title, text, images} from a page. Text is the <article> element (or the
    whole body) after the headline, minus link-only rows and page furniture; the
    article library, when installed, trims it to where the article starts and ends,
    and `sibling_html` (another post from the same site) exposes repeated furniture."""
    p = _ArticleParser(base_url)
    p.feed(html)
    p.close()
    body, art = _rows(p.body), _rows(p.art)
    body_len, art_len = (sum(len(r[0]) for r in rows) for rows in (body, art))
    use_article = bool(art) and (art_len >= 500 or art_len * 2 >= body_len)
    above, after = _split_at_headline(art if use_article else body)
    lines = [t for t, _, link_share in after if link_share < 0.75]
    lines = _article_span(lines, _library_text(html))
    if sibling_html:
        lines = _drop_shared(lines, sibling_html)
    text = _tidy(lines)

    urls = [p.og_image] + [u for inside, u in p.images if inside or not use_article]
    images: list[str] = []
    for u in urls:
        if u and u not in images:
            images.append(u)
    return {"title": p.og_title or p.title.strip(), "text": text, "images": images}


# ------------------------------------------------------------------ X payloads
_TRAILING_LINK = re.compile(r"\s*https://t\.co/\w+\s*$")


def clean_tweet_text(text: str, attached: int = 0) -> str:
    """Post text as a person reads it: HTML codes decoded (X sends `&amp;`), and
    up to `attached` trailing t.co links removed — X appends one for the post's
    own pictures/video and one for a quoted post. Other links stay."""
    text = html_lib.unescape(text or "")
    for _ in range(attached):
        text, n = _TRAILING_LINK.subn("", text)
        if not n:
            break
    return text.strip()


_OWN_MEDIA = re.compile(r"/status/\d+/(photo|video)/\d+$")


def _expand_links(text: str, urls: list[dict], quoted_id: str = "") -> str:
    """Swap each t.co link for the address X says it points to, and drop the
    ones that only point at the post's own media or at the post it quotes."""
    for u in urls:
        short = u.get("url") or ""
        target = u.get("unwound_url") or u.get("expanded_url") or ""
        if not short or not target:
            continue
        own = (u.get("media_key") or _OWN_MEDIA.search(target)
               or (quoted_id and target.rstrip("/").endswith(f"/status/{quoted_id}")))
        text = text.replace(short, "" if own else target)
    return html_lib.unescape(re.sub(r"[ \t]+(\n|$)", r"\1", text)).strip()


def _tweet_text(t: dict, has_media: bool, quoted_id: str) -> str:
    article = t.get("article") or {}
    if article.get("plain_text"):                # an X article: the post is only a link
        return "\n\n".join(x for x in (article.get("title", "").strip(),
                                        article["plain_text"].strip()) if x)
    note = t.get("note_tweet") or {}
    body, entities = (note, note.get("entities")) if note.get("text") else (t, t.get("entities"))
    urls = (entities or {}).get("urls")
    if urls:
        return _expand_links(body.get("text", ""), urls, quoted_id)
    if note.get("text"):
        return clean_tweet_text(note["text"])
    return clean_tweet_text(t.get("text", ""), attached=has_media + bool(quoted_id))


def parse_tweets(data: dict) -> tuple[dict[str, dict], set[str]]:
    """Normalize a GET /2/tweets response to ({id: {text, author, created_at,
    media, ref_id, is_repost}}, gone_ids). Long posts carry their real text in
    `note_tweet`, X articles in `article`; `text` is the ~280-char cut. Links
    are shown as their real addresses (see _expand_links). A video/GIF yields its preview
    picture plus a `video_file` entry for the best mp4 up to ~720p."""
    inc = data.get("includes") or {}
    users = {u["id"]: u.get("username", "") for u in inc.get("users", [])}
    media = {m["media_key"]: m for m in inc.get("media", [])}
    found: dict[str, dict] = {}
    for t in data.get("data") or []:
        items = []
        article = t.get("article") or {}
        keys = ((t.get("attachments") or {}).get("media_keys", [])
                + [k for k in [article.get("cover_media")] if k]
                + list(article.get("media_entities") or []))
        for key in dict.fromkeys(keys):
            m = media.get(key) or {}
            url = m.get("url") or m.get("preview_image_url") or ""
            if url:
                items.append({"kind": m.get("type", "photo"), "url": url,
                              "alt": m.get("alt_text") or ""})
            mp4 = [v for v in m.get("variants") or []
                   if v.get("content_type") == "video/mp4" and v.get("url")]
            if mp4:     # the whole video: best quality up to ~720p, else smallest
                rate = lambda v: v.get("bit_rate") or 0
                fits = [v for v in mp4 if rate(v) <= MAX_VIDEO_BITRATE]
                best = max(fits, key=rate) if fits else min(mp4, key=rate)
                items.append({"kind": "video_file", "url": best["url"],
                              "alt": m.get("alt_text") or ""})
        kinds = {r.get("type"): r.get("id", "") for r in t.get("referenced_tweets") or []}
        ref = kinds.get("retweeted") or kinds.get("quoted") or ""
        found[t["id"]] = {
            "text": _tweet_text(t, bool(items), kinds.get("quoted") or ""),
            "is_repost": bool(kinds.get("retweeted")),
            "author": users.get(t.get("author_id"), ""),
            "created_at": t.get("created_at", ""),
            "media": items, "ref_id": ref,
        }
    gone = {str(e["resource_id"]) for e in data.get("errors") or []
            if e.get("resource_id")}
    return found, gone - set(found)


# ------------------------------------------------------------------ downloads
def http_get(url: str) -> tuple[bytes, str]:
    """(body, content-type). Generic client name; nothing identifying is sent."""
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; archive/1.0)", "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read(MAX_BYTES), resp.headers.get_content_type()


def _ext(url: str, ctype: str) -> str:
    if ctype in _EXT:
        return _EXT[ctype]
    suffix = Path(urlparse(url).path).suffix.lstrip(".").lower()
    return suffix if suffix in set(_EXT.values()) | {"jpeg"} else ""


# ------------------------------------------------------------------ the build
def _open(out: Path) -> sqlite3.Connection:
    out.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(out / "archive.db")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def _sync_items(conn, rows) -> None:
    """Mirror posted_log into `items`. New rows start `pending` with the text
    production held; existing rows only get their metrics/our-post refreshed."""
    for r in rows:
        sid = r["source_tweet_id"] or ""
        our = r["our_tweet_id"] or ""
        conn.execute(
            """INSERT OR IGNORE INTO items
                   (id, kind, source_id, source_url, source_author, source_text,
                    stored_text, status)
               VALUES (?,?,?,?,?,?,?, 'pending')""",
            (r["id"], "web" if sid.startswith(WEB_ID_PREFIX) else "tweet", sid,
             r["source_url"] or "", r["author_handle"] or "", r["source_text"] or "",
             r["source_text"] or ""))
        conn.execute(
            """UPDATE items SET posted_at=?, posted_at_pt=?, our_tweet_id=?, our_url=?,
                   our_text=?, views=?, likes=?, reposts=?, replies=?, quotes=?
               WHERE id=?""",
            (r["posted_at"], r["posted_at_pt"], our,
             f"https://x.com/i/status/{our}" if our else "", r["commentary"] or "",
             r["views"], r["likes"], r["reposts"], r["replies"], r["quotes"], r["id"]))
    conn.commit()


def _save_media(conn, out: Path, get, item_id: int, role: str, media: list[dict]) -> int:
    saved = 0
    for m in media:
        if conn.execute("SELECT 1 FROM media WHERE item_id=? AND url=? AND path!=''",
                        (item_id, m["url"])).fetchone():
            continue
        path = ""
        try:
            body, ctype = get(m["url"])
            ext = _ext(m["url"], ctype)
            if ext and body and len(body) < MAX_BYTES:   # at the cap = cut off
                n = conn.execute("SELECT COUNT(*) FROM media WHERE item_id=?",
                                 (item_id,)).fetchone()[0] + 1
                rel = Path("media") / str(item_id) / f"{role}-{n}.{ext}"
                (out / rel).parent.mkdir(parents=True, exist_ok=True)
                (out / rel).write_bytes(body)
                path, saved = rel.as_posix(), saved + 1
        except Exception:
            pass                                    # row kept with path '' = failed
        conn.execute("INSERT OR REPLACE INTO media (item_id, role, kind, url, path, alt) "
                     "VALUES (?,?,?,?,?,?)",
                     (item_id, role, m.get("kind", "image"), m["url"], path, m.get("alt", "")))
    conn.commit()
    return saved


def _chunks(seq: list, n: int = 100):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _archive_tweets(conn, out, get, source, todo, stats, log) -> None:
    now = utcnow().isoformat()
    by_id: dict[str, list] = {}
    for it in todo:
        by_id.setdefault(it["source_id"], []).append(it["id"])
    refs: dict[str, list[tuple[int, bool]]] = {}    # quoted/reposted id -> (item, repost?)
    for chunk in _chunks(list(by_id)):
        found, gone = source.fetch_full(chunk)
        stats["x_reads"] += len(found)
        for sid in chunk:
            for item_id in by_id[sid]:
                if sid in found:
                    t = found[sid]
                    conn.execute(
                        """UPDATE items SET source_text=?, source_author=?,
                               source_created_at=?, quoted_id=?, status='ok',
                               status_note='', fetched_at=? WHERE id=?""",
                        (t["text"], t["author"], t["created_at"], t["ref_id"], now, item_id))
                    stats["media"] += _save_media(conn, out, get, item_id, "source", t["media"])
                    stats["fetched"] += 1
                    if t["ref_id"]:
                        refs.setdefault(t["ref_id"], []).append(
                            (item_id, bool(t.get("is_repost"))))
                else:
                    state = "gone" if sid in gone else "error"
                    conn.execute("UPDATE items SET status=?, status_note=?, fetched_at=? "
                                 "WHERE id=?",
                                 (state, "deleted or no longer visible on X"
                                  if state == "gone" else "X did not return this post",
                                  now, item_id))
                    stats[state] += 1
        conn.commit()
    for chunk in _chunks(list(refs)):
        found, _ = source.fetch_full(chunk)
        stats["x_reads"] += len(found)
        for rid, t in found.items():
            for item_id, is_repost in refs[rid]:
                if is_repost:       # a repost is only a cut-off stub: keep the original
                    conn.execute("UPDATE items SET source_author=?, source_text=? WHERE id=?",
                                 (t["author"], t["text"], item_id))
                else:
                    conn.execute("UPDATE items SET quoted_author=?, quoted_text=? WHERE id=?",
                                 (t["author"], t["text"], item_id))
                stats["media"] += _save_media(conn, out, get, item_id,
                                              "source" if is_repost else "quoted", t["media"])
        conn.commit()


_NEEDS_VIDEO = """
SELECT DISTINCT i.id, m.role,
       CASE m.role WHEN 'quoted' THEN i.quoted_id ELSE i.source_id END AS tid
FROM media m JOIN items i ON i.id = m.item_id
WHERE m.kind IN ('video', 'animated_gif') AND m.role IN ('source', 'quoted')
  AND NOT EXISTS (SELECT 1 FROM media v WHERE v.item_id = m.item_id
                  AND v.role = m.role AND v.kind = 'video_file')
"""


def _backfill_videos(conn, out, get, source, stats) -> None:
    """Posts archived before whole videos were saved hold only the preview
    picture: re-read those tweets ONCE for the mp4 link. A post X returns no
    mp4 for gets an empty marker row so it is never paid for again."""
    want: dict[str, list] = {}
    for r in conn.execute(_NEEDS_VIDEO).fetchall():
        if r["tid"]:
            want.setdefault(r["tid"], []).append((r["id"], r["role"]))
    for chunk in _chunks(list(want)):
        found, _ = source.fetch_full(chunk)
        stats["x_reads"] += len(found)
        for tid in chunk:
            videos = [m for m in (found.get(tid) or {}).get("media", [])
                      if m["kind"] == "video_file"]
            for item_id, role in want[tid]:
                if videos:
                    stats["media"] += _save_media(conn, out, get, item_id, role, videos)
                else:
                    conn.execute(
                        "INSERT OR REPLACE INTO media (item_id, role, kind, url, path, alt) "
                        "VALUES (?,?, 'video_file', ?, '', 'no video file available')",
                        (item_id, role, f"unavailable:{role}"))
        conn.commit()


def _retry_videos(conn, out, get, stats) -> None:
    """Free: download again any video whose link we hold but whose file failed."""
    for r in conn.execute("SELECT item_id, role, url, alt FROM media WHERE "
                          "kind='video_file' AND path='' AND url LIKE 'http%'").fetchall():
        stats["media"] += _save_media(conn, out, get, r["item_id"], r["role"],
                                      [{"kind": "video_file", "url": r["url"],
                                        "alt": r["alt"] or ""}])


def _sibling_html(get, html: str, url: str) -> str | None:
    """A second post from the same site, to tell furniture from article. Free,
    and optional: any failure just means no comparison."""
    other = sibling_url(html, url)
    if not other:
        return None
    try:
        return get(other)[0].decode("utf-8", errors="replace")
    except Exception:
        return None


def _read_article(get, url: str) -> dict:
    body, _ = get(url)
    html = body.decode("utf-8", errors="replace")
    art = extract_article(html, url, sibling_html=_sibling_html(get, html, url))
    if not art["text"]:
        raise ValueError("page returned no readable text")
    return art


def _saved_article(repo, it: dict) -> dict | None:
    """The article from the page web-content kept when the bot first read it —
    the fallback when the site has since deleted or locked the page."""
    from .ingest.web_articles import load_page
    html = load_page(repo, it["source_id"])
    art = extract_article(html, it["source_url"]) if html else None
    return art if art and art["text"] else None


def _archive_web(conn, out, get, todo, stats, log, repo=None) -> None:
    for it in todo:
        now = utcnow().isoformat()
        try:
            try:
                art = _read_article(get, it["source_url"])
            except Exception:
                art = _saved_article(repo, it) if repo is not None else None
                if art is None:
                    raise
                log(f"  [archive] {it['source_url']} — site unavailable; used the "
                    "page saved when we read it")
        except Exception as e:
            conn.execute("UPDATE items SET status='error', status_note=?, fetched_at=? "
                         "WHERE id=?", (str(e)[:300], now, it["id"]))
            conn.commit()
            stats["error"] += 1
            log(f"  [archive] {it['source_url']} — {str(e)[:80]}")
            continue
        conn.execute("UPDATE items SET source_text=?, source_title=?, status='ok', "
                     "status_note='', fetched_at=? WHERE id=?",
                     (art["text"], art["title"], now, it["id"]))
        media = [{"kind": "image", "url": u} for u in art["images"][:MAX_ARTICLE_IMAGES]]
        stats["media"] += _save_media(conn, out, get, it["id"], "article", media)
        stats["fetched"] += 1


def build_archive(repo, source, out_dir, http_get=http_get, limit: int | None = None,
                  log=print) -> dict:
    """Sync production's published posts into <out_dir>/archive.db and fetch the
    originals (and whole videos) still missing. `source=None` skips the (paid) X reads: tweets stay
    `pending` with the text production held, and fill in on a later run."""
    out = Path(out_dir)
    conn = _open(out)
    try:
        _sync_items(conn, repo.posted_all())
        todo = [dict(r) for r in conn.execute(
            "SELECT id, kind, source_id, source_url FROM items "
            "WHERE status IN ('pending', 'error') ORDER BY id DESC").fetchall()]
        if limit:
            todo = todo[:limit]
        stats = {"fetched": 0, "gone": 0, "error": 0, "media": 0, "x_reads": 0,
                 "x_error": ""}
        tweets = [t for t in todo if t["kind"] == "tweet"]
        if source is not None and tweets:
            try:
                _archive_tweets(conn, out, http_get, source, tweets, stats, log)
            except Exception as e:                  # e.g. 402: keep going with web
                stats["x_error"] = str(e)[:300]
                log(f"  [archive] X read failed — tweets left pending: {stats['x_error']}")
        _retry_videos(conn, out, http_get, stats)
        if source is not None and not stats["x_error"]:
            try:
                _backfill_videos(conn, out, http_get, source, stats)
            except Exception as e:
                stats["x_error"] = str(e)[:300]
                log(f"  [archive] X read failed — videos not added: {stats['x_error']}")
        _archive_web(conn, out, http_get, [t for t in todo if t["kind"] == "web"],
                     stats, log, repo=repo)
        counts = {r["status"]: r["n"] for r in conn.execute(
            "SELECT status, COUNT(*) n FROM items GROUP BY status").fetchall()}
        stats.update(total=sum(counts.values()), by_status=counts,
                     db=str(out / "archive.db"))
        return stats
    finally:
        conn.close()


# ------------------------------------------------------------------ website
_SITE_FIELDS = ("id", "kind", "posted_at", "our_url", "our_text", "source_url",
                "source_author", "source_title", "source_text", "quoted_author",
                "quoted_text", "status", "views", "likes")


def export_site(out_dir) -> dict:
    """Write <out_dir>/data.json for the static site: every archived post, newest
    first, with only the media files that actually exist. `tile` is the first
    picture (the grid thumbnail); `has_video` flags a saved video file."""
    out = Path(out_dir)
    conn = _open(out)
    try:
        media: dict[int, list[dict]] = {}
        for m in conn.execute("SELECT item_id, role, kind, path, alt FROM media "
                              "WHERE path != '' ORDER BY id").fetchall():
            media.setdefault(m["item_id"], []).append(
                {"role": m["role"], "kind": m["kind"], "path": m["path"],
                 "alt": m["alt"] or ""})
        items = []
        for r in conn.execute("SELECT * FROM items ORDER BY posted_at DESC, id DESC").fetchall():
            ms = media.get(r["id"], [])
            item = {k: r[k] for k in _SITE_FIELDS}
            item["media"] = ms
            item["tile"] = next((m["path"] for m in ms if m["kind"] != "video_file"), "")
            item["has_video"] = any(m["kind"] == "video_file" for m in ms)
            items.append(item)
    finally:
        conn.close()
    payload = {"generated_at": utcnow().isoformat(), "items": items}
    (out / "data.json").write_text(json.dumps(payload, ensure_ascii=False),
                                   encoding="utf-8")
    return {"items": len(items), "path": str(out / "data.json")}
