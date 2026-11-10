"""`xbot archive`: a local copy of every post we published next to the original
it came from (full text + picture files). Read-only against production."""
import sqlite3

from xbot.archive import build_archive, extract_article, parse_tweets
from xbot.models import Metrics, Post, utcnow
from xbot.storage.sqlite_repo import SqliteRepository

ARTICLE_HTML = """<html><head><title>Fallback title</title>
<meta property="og:title" content="Ten hooks that work">
<meta property="og:image" content="https://cdn.site.com/cover.jpg">
<script>var tracking = "SCRIPT_JUNK";</script></head>
<body><nav>Home Pricing NAV_JUNK</nav>
<article><h1>Ten hooks that work</h1>
<p>Write ten hooks per product.</p>
<img src="/img/chart.png" alt="a chart">
<img src="data:image/gif;base64,AAAA">
<p>Then test all of them.</p></article>
<footer>FOOTER_JUNK</footer></body></html>"""


def test_extract_article_keeps_the_article_body_and_drops_page_chrome():
    art = extract_article(ARTICLE_HTML, "https://site.com/blog/hooks")
    assert art["title"] == "Ten hooks that work"
    assert "Write ten hooks per product." in art["text"]
    assert "Then test all of them." in art["text"]
    for junk in ("NAV_JUNK", "SCRIPT_JUNK", "FOOTER_JUNK"):
        assert junk not in art["text"]


def test_extract_article_returns_absolute_picture_links_without_inline_data():
    art = extract_article(ARTICLE_HTML, "https://site.com/blog/hooks")
    assert art["images"] == ["https://cdn.site.com/cover.jpg",
                             "https://site.com/img/chart.png"]


def test_extract_article_falls_back_to_the_page_body_without_an_article_tag():
    art = extract_article("<html><body><div><p>Only paragraph.</p></div></body></html>",
                          "https://site.com/x")
    assert art["text"] == "Only paragraph."
    assert art["title"] == ""


CLUTTERED_HTML = """<html><head><meta property="og:title" content="How to make AI ads">
</head><body>
<div class="site-container"><p>PROMO_BANNER register before 31 December.</p>
<a href="/more">Learn More</a></div>
<div class="crumbs"><a href="/">Academy</a> &gt; <a href="/ai">AI</a> &gt; CRUMB_JUNK</div>
<section><h1>How to make AI ads</h1>
<p class="post-meta">Written By Jane Doe | Published On Oct 25, 2026</p>
<p>Categories: <a href="/c/ai">Artificial Intelligence</a></p></section>
<div class="side-menu"><h4>Table of Contents</h4><ul>
<li><a href="#a">TOC_ENTRY_ONE</a></li><li><a href="#b">TOC_ENTRY_TWO</a></li>
<li><a href="#c">TOC_ENTRY_THREE</a></li></ul><p>Share This</p></div>
<div class="content">
<p>If you run paid social you have seen the messy testimonial beat the studio ad.</p>
<h2>What you need</h2>
<p>You need a script and <a href="/tool">a video tool</a> to turn it into a clip.</p>
<ul><li>An avatar generator to turn scripts into talking heads.</li>
<li>A voice generator for the voiceover.</li></ul>
<p>So start small and test one angle.</p></div>
<div><p>Article Written By</p><p>Jane Doe</p><p>AUTHOR_BIO loves keyword research.</p></div>
<div><h3>Keep Learning</h3><p>RELATED_POST about creators</p></div>
<div class="respond"><h3>Leave a Reply</h3><p>REPLY_JUNK</p></div>
<div><p>Never Miss a Post</p><p>NEWSLETTER_JUNK into your inbox.</p></div>
</body></html>"""


def test_extract_article_starts_at_the_headline_and_drops_what_sits_above_it():
    text = extract_article(CLUTTERED_HTML, "https://site.com/a")["text"]
    assert text.startswith("If you run paid social")
    for junk in ("PROMO_BANNER", "Learn More", "CRUMB_JUNK", "Written By", "Categories:"):
        assert junk not in text


def test_extract_article_drops_the_table_of_contents_but_keeps_real_lists_and_links():
    text = extract_article(CLUTTERED_HTML, "https://site.com/a")["text"]
    for junk in ("Table of Contents", "TOC_ENTRY_ONE", "TOC_ENTRY_THREE", "Share This"):
        assert junk not in text
    assert "What you need" in text
    assert "You need a script and a video tool to turn it into a clip." in text
    assert "An avatar generator to turn scripts into talking heads." in text


def test_extract_article_stops_at_the_author_box_related_posts_and_comments():
    text = extract_article(CLUTTERED_HTML, "https://site.com/a")["text"]
    assert text.endswith("So start small and test one angle.")
    for junk in ("AUTHOR_BIO", "RELATED_POST", "REPLY_JUNK", "NEWSLETTER_JUNK"):
        assert junk not in text


def test_extract_article_keeps_a_page_whose_only_text_sits_above_the_headline():
    html = "<html><body><p>" + "Real words here. " * 20 + "</p><h1>Late title</h1></body></html>"
    assert "Real words here." in extract_article(html, "https://site.com/a")["text"]


def _long_article(extra_top: str = "") -> str:
    """A page long enough for the article library to accept, with a plain-text
    table of contents (no links) that repeats the section headings."""
    sections = ["Why hooks matter", "Writing the script", "Testing the ads"]
    toc = "".join(f"<li>{h}</li>" for h in sections)
    body = "".join(
        f"<h2>{h}</h2>" + "".join(
            f"<p>Paragraph {n} about {h.lower()}: a team that tests ten openings a week "
            f"learns faster than a team that polishes one, because the feed decides.</p>"
            for n in range(4))
        for h in sections)
    return (f"<html><head><title>Hooks guide</title></head><body>{extra_top}"
            f"<main><h1>Hooks guide</h1><div><h4>Table of Contents</h4><ul>{toc}</ul></div>"
            f"<div>{body}</div></main></body></html>")


def _both_engines(monkeypatch, html: str) -> list[str]:
    """The text from the article library (when installed) and from the built-in reader."""
    from xbot import archive
    texts = [extract_article(html, "https://site.com/a")["text"]]
    monkeypatch.setattr(archive, "_library_text", lambda _html: "")
    texts.append(extract_article(html, "https://site.com/a")["text"])
    return texts


def test_extract_article_drops_a_plain_text_table_of_contents(monkeypatch):
    for text in _both_engines(monkeypatch, _long_article()):
        assert "Table of Contents" not in text
        assert text.count("Writing the script") == 1      # the heading, not the menu entry
        assert "Paragraph 3 about testing the ads" in text


def test_extract_article_drops_text_above_the_headline_in_both_engines(monkeypatch):
    top = "<pre><p>SHELL_JUNK uploader</p>Linux box 6.8.0 KERNEL_JUNK</pre>"
    for text in _both_engines(monkeypatch, _long_article(top)):
        assert "SHELL_JUNK" not in text and "KERNEL_JUNK" not in text
        assert "Paragraph 0 about why hooks matter" in text


def test_extract_article_cluttered_page_is_clean_in_the_builtin_reader(monkeypatch):
    from xbot import archive
    monkeypatch.setattr(archive, "_library_text", lambda _html: "")
    text = extract_article(CLUTTERED_HTML, "https://site.com/a")["text"]
    assert text.startswith("If you run paid social")
    assert text.endswith("So start small and test one angle.")


def _page(top: str, bottom: str) -> str:
    para = "".join(f"<p>Real paragraph {n}: testing ten openings a week teaches a team "
                   f"faster than polishing a single one ever will.</p>" for n in range(8))
    return f"<html><body><article><h1>Title</h1>{top}{para}{bottom}</article></body></html>"


def test_extract_article_drops_date_and_author_rows_under_the_headline():
    top = ("<p>September 2, 2025</p><p>By Nim</p><p>Airbridge·September 18, 2026·9 min read</p>"
           "<p>Sections</p><p>By Pushpit, Founder of Faceless.so</p>"
           "<p>Cy GrecoFounder12 min readJuly 20, 2026</p>")
    text = extract_article(_page(top, ""), "https://site.com/a")["text"]
    assert text.startswith("Real paragraph 0")


def test_extract_article_keeps_a_sentence_that_merely_starts_with_by():
    top = "<p>By 2026 most teams had stopped booking creators for every single ad.</p>"
    text = extract_article(_page(top, ""), "https://site.com/a")["text"]
    assert text.startswith("By 2026 most teams")


def test_extract_article_drops_subscribe_prompts_and_dangling_labels_at_the_end():
    bottom = ("<p>Thanks for reading Growth Notes! Subscribe for free to receive new posts.</p>"
              "<h2>Frequently Asked Questions</h2><p>What is a hook?</p><p>Why test ten?</p>"
              "<p>ai ugc ads</p><p>creator-style ads</p><p>8</p>")
    text = extract_article(_page("", bottom), "https://site.com/a")["text"]
    assert text.endswith("than polishing a single one ever will.")
    assert "Subscribe for free" not in text


def test_extract_article_keeps_table_cells_apart():
    table = "<table><tr><th>Benefit</th><th>Impact</th></tr><tr><td>Speed</td><td>16 minutes</td></tr></table>"
    text = extract_article(_page(table, ""), "https://site.com/a")["text"]
    assert "Benefit | Impact" in text and "Speed | 16 minutes" in text


DIRECTORY = ("<div><h3>Top Marketing Agencies</h3><p>2</p><p>The Shelf</p>"
             "<p>A boutique agency for beauty and lifestyle campaigns with strong reporting.</p>"
             "<p>Clients: Pepsi, Hims</p></div>")
PITCH = "<p>Book a 30-minute call and we will tell you what we would do in your situation.</p>"


def test_extract_article_drops_what_another_post_on_the_site_also_shows():
    faq = "<h2>Frequently Asked Questions</h2><p>Does it scale?</p><p>Yes, with a system in place.</p>"
    page = _page("<h2>Key Takeaways</h2>", PITCH + faq + DIRECTORY)
    other = ("<html><body><article><h1>Other post</h1><h2>Key Takeaways</h2><p>Different words "
             "entirely about promo codes.</p>" + PITCH +
             "<h2>Frequently Asked Questions</h2><p>Another question?</p>" + DIRECTORY +
             "</article></body></html>")
    text = extract_article(page, "https://site.com/a", sibling_html=other)["text"]
    assert "Book a 30-minute call" not in text            # the same pitch sits on both posts
    assert "The Shelf" not in text and "Top Marketing Agencies" not in text
    assert "Key Takeaways" in text                        # a short shared heading is not clutter
    assert text.endswith("Yes, with a system in place.")  # this post's own FAQ stays


def test_extract_article_without_a_second_post_keeps_everything():
    text = extract_article(_page("", DIRECTORY), "https://site.com/a")["text"]
    assert "The Shelf" in text


def test_extract_article_keeps_the_faq_the_library_stops_short_of(monkeypatch):
    from xbot import archive
    faq = ("<h2>Frequently Asked Questions</h2><h3>How long should a test run?</h3>"
           "<p>Long enough to be sure of the winner.</p>"
           "<button><span>Can I test two things at once?</span></button>"
           "<p>At least two weeks and two hundred subscriptions per variant.</p>"
           "<button>Copy link</button>"
           "<div><p>Apple dropped the blue background on App Store ads in the new release.</p>"
           "<p>Jane DoeRead</p><p>Meta treated every trial as equal until weighting changed it.</p>"
           "<p>John RoeRead</p><p>Another teaser for a different post on this site entirely.</p></div>")
    page = _page("", faq)
    body_only = "\n".join(f"Real paragraph {n}: testing ten openings a week teaches a team "
                          f"faster than polishing a single one ever will." for n in range(8))
    monkeypatch.setattr(archive, "_library_text", lambda _html: body_only)
    text = extract_article(page, "https://site.com/a")["text"]
    assert text.endswith("At least two weeks and two hundred subscriptions per variant.")
    assert "Can I test two things at once?" in text and "Copy link" not in text


def test_extract_article_shared_lines_do_not_cut_real_content_that_follows():
    end = "<h2>The Testing Framework</h2><p>Create three variations and test them on one audience.</p>"
    cta = ("<h2>Ready to Get Started?</h2><p>Creating viral ads does not require massive budgets or "
           "months of planning anymore.</p><p>The best content still comes down to hooks that stop "
           "the scroll and connect.</p>")
    other = "<html><body><article><h1>Other</h1><p>Other words.</p>" + cta + "</article></body></html>"
    text = extract_article(_page("", cta + end), "https://site.com/a", sibling_html=other)["text"]
    assert text.endswith("Create three variations and test them on one audience.")
    assert "massive budgets" not in text


def test_sibling_url_picks_another_post_in_the_same_section():
    from xbot.archive import sibling_url
    html = ('<a href="/">Home</a><a href="/pricing">Pricing</a><a href="/blog/hooks-that-work-now">self</a>'
            '<a href="https://other.com/blog/some-other-long-post">x</a><a href="/blog/logo-big-one-two.png">i</a>'
            '<a href="/blog/how-to-test-ten-openings">next</a>')
    assert (sibling_url(html, "https://site.com/blog/hooks-that-work-now/")
            == "https://site.com/blog/how-to-test-ten-openings")
    assert sibling_url('<a href="/">Home</a>', "https://site.com/blog/hooks-that-work-now") is None


TWEETS_JSON = {
    "data": [
        {"id": "11", "author_id": "u1", "text": "short cut off…",
         "created_at": "2026-10-01T00:00:00.000Z",
         "note_tweet": {"text": "the full long text of the post"},
         "attachments": {"media_keys": ["m1", "m2"]},
         "referenced_tweets": [{"type": "quoted", "id": "99"}]},
        {"id": "12", "author_id": "u1", "text": "plain post"},
    ],
    "includes": {
        "users": [{"id": "u1", "username": "teacher"}],
        "media": [
            {"media_key": "m1", "type": "photo", "url": "https://pbs.twimg.com/a.jpg",
             "alt_text": "screenshot"},
            {"media_key": "m2", "type": "video",
             "preview_image_url": "https://pbs.twimg.com/v.jpg"},
        ],
    },
    "errors": [{"resource_id": "13", "title": "Not Found Error"}],
}


def test_parse_tweets_prefers_the_long_form_text():
    found, gone = parse_tweets(TWEETS_JSON)
    assert found["11"]["text"] == "the full long text of the post"
    assert found["12"]["text"] == "plain post"
    assert found["11"]["author"] == "teacher"


def test_parse_tweets_maps_pictures_and_video_previews():
    found, _ = parse_tweets(TWEETS_JSON)
    assert found["11"]["media"] == [
        {"kind": "photo", "url": "https://pbs.twimg.com/a.jpg", "alt": "screenshot"},
        {"kind": "video", "url": "https://pbs.twimg.com/v.jpg", "alt": ""},
    ]
    assert found["12"]["media"] == []


def test_parse_tweets_reports_the_quoted_post_and_deleted_ids():
    found, gone = parse_tweets(TWEETS_JSON)
    assert found["11"]["ref_id"] == "99"
    assert found["12"]["ref_id"] == ""
    assert gone == {"13"}


def _one(tweet: dict, media=()) -> dict:
    data = {"data": [{"id": "1", "author_id": "u1", **tweet}],
            "includes": {"users": [{"id": "u1", "username": "teacher"}], "media": list(media)}}
    return parse_tweets(data)[0]["1"]


PHOTO = {"media_key": "m1", "type": "photo", "url": "https://pbs.twimg.com/a.jpg"}


def test_parse_tweets_turns_html_codes_back_into_characters():
    assert _one({"text": "works &amp; improves, 5 &gt; 3"})["text"] == "works & improves, 5 > 3"


def test_parse_tweets_drops_the_trailing_link_that_only_points_at_its_own_picture():
    t = _one({"text": "look at this https://t.co/pic1", "attachments": {"media_keys": ["m1"]}},
             [PHOTO])
    assert t["text"] == "look at this"


def test_parse_tweets_drops_one_trailing_link_each_for_the_picture_and_the_quoted_post():
    t = _one({"text": "so good https://t.co/quote1 https://t.co/pic1",
              "attachments": {"media_keys": ["m1"]},
              "referenced_tweets": [{"type": "quoted", "id": "99"}]}, [PHOTO])
    assert t["text"] == "so good"


def test_parse_tweets_keeps_a_real_link_in_the_post():
    assert _one({"text": "read this https://t.co/article"})["text"] == "read this https://t.co/article"
    t = _one({"text": "read https://t.co/article https://t.co/pic1",
              "attachments": {"media_keys": ["m1"]}}, [PHOTO])
    assert t["text"] == "read https://t.co/article"


def _urls(*pairs):
    return {"urls": [{"url": u, "expanded_url": e, **extra} for u, e, extra in pairs]}


def test_parse_tweets_shows_real_addresses_instead_of_short_links():
    t = _one({"text": "the study: https://t.co/aaa and https://t.co/bbb",
              "entities": _urls(("https://t.co/aaa", "https://site.com/study", {}),
                                ("https://t.co/bbb", "http://short.ly/x",
                                 {"unwound_url": "https://other.com/full"}))})
    assert t["text"] == "the study: https://site.com/study and https://other.com/full"


def test_parse_tweets_removes_links_to_its_own_media_and_quoted_post_exactly():
    t = _one({"text": "watch https://t.co/keep https://t.co/quote https://t.co/vid",
              "attachments": {"media_keys": ["m1"]},
              "referenced_tweets": [{"type": "quoted", "id": "99"}],
              "entities": _urls(
                  ("https://t.co/keep", "https://site.com/a", {}),
                  ("https://t.co/quote", "https://twitter.com/maker/status/99", {}),
                  ("https://t.co/vid", "https://x.com/me/status/1/video/1", {"media_key": "m1"}))},
             [PHOTO])
    assert t["text"] == "watch https://site.com/a"


def test_parse_tweets_reads_a_long_post_with_its_own_link_list():
    t = _one({"text": "cut…", "note_tweet": {
        "text": "long post, tool: https://t.co/tool",
        "entities": _urls(("https://t.co/tool", "https://tool.com/", {}))}})
    assert t["text"] == "long post, tool: https://tool.com/"


def test_parse_tweets_uses_the_x_article_text_and_pictures():
    t = _one({"text": "https://t.co/art",
              "entities": _urls(("https://t.co/art", "http://x.com/i/article/55", {})),
              "article": {"title": "How to build an army", "plain_text": "I run many accounts.\nHere is how.",
                          "cover_media": "m1", "media_entities": ["m9"]}},
             [PHOTO, {"media_key": "m9", "type": "photo", "url": "https://pbs.twimg.com/b.jpg"}])
    assert t["text"] == "How to build an army\n\nI run many accounts.\nHere is how."
    assert [m["url"] for m in t["media"]] == ["https://pbs.twimg.com/a.jpg",
                                             "https://pbs.twimg.com/b.jpg"]


def test_parse_tweets_flags_a_repost():
    assert _one({"text": "RT @a: cut off…",
                 "referenced_tweets": [{"type": "retweeted", "id": "99"}]})["is_repost"] is True
    assert _one({"text": "mine", "referenced_tweets": [{"type": "quoted", "id": "99"}]})["is_repost"] is False


# ------------------------------------------------------------------ end to end
def test_archive_stores_the_reposted_original_instead_of_the_cut_off_stub(tmp_path):
    repo = _repo()
    _seed(repo, "11", "501")
    stub = dict(_tweet("RT @maker: the start of the post…", ref_id="99"), is_repost=True)
    full = dict(_tweet("the start of the post and all the rest of it", [
        {"kind": "photo", "url": "https://pbs.twimg.com/q.jpg", "alt": ""}]), author="maker")
    web = FakeWeb({"https://pbs.twimg.com/q.jpg": (b"Q", "image/jpeg")})

    build_archive(repo, FakeX({"11": stub, "99": full}), tmp_path, http_get=web)

    item = _rows(tmp_path, "SELECT * FROM items")[0]
    assert item["source_text"] == "the start of the post and all the rest of it"
    assert item["source_author"] == "maker"
    assert not item["quoted_text"]
    assert _rows(tmp_path, "SELECT role FROM media")[0]["role"] == "source"


def _repo():
    repo = SqliteRepository(":memory:")
    repo.init_schema()
    return repo


def _seed(repo, source_id, our_id, text="stored text", url=""):
    repo.upsert_post(Post(tweet_id=source_id, author_handle="teacher", author_name="T",
                          author_follower_count=1, text=text,
                          created_at=utcnow().isoformat(),
                          url=url or f"https://x.com/teacher/status/{source_id}"))
    repo.log_posted(source_id, our_id, "teacher", text, f"our take on {source_id}")


class FakeX:
    def __init__(self, found, gone=()):
        self.found, self.gone, self.calls = found, set(gone), []

    def fetch_full(self, ids):
        self.calls.append(list(ids))
        return ({i: self.found[i] for i in ids if i in self.found},
                {i for i in ids if i in self.gone})


class FakeWeb:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def __call__(self, url):
        self.calls.append(url)
        if url not in self.pages:
            raise OSError("HTTP Error 404: Not Found")
        return self.pages[url]


def _tweet(text, media=(), ref_id=""):
    return {"text": text, "author": "teacher", "created_at": "", "ref_id": ref_id,
            "media": list(media)}


def _rows(out, sql):
    conn = sqlite3.connect(out / "archive.db")
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(sql).fetchall()]


def test_posted_all_lists_every_published_post_with_its_source_link():
    repo = _repo()
    _seed(repo, "11", "501")
    row = repo.posted_all()[0]
    assert row["source_tweet_id"] == "11"
    assert row["our_tweet_id"] == "501"
    assert row["source_url"] == "https://x.com/teacher/status/11"
    assert row["commentary"] == "our take on 11"


def test_posted_all_carries_the_highest_view_count_seen():
    repo = _repo()
    _seed(repo, "11", "501")
    repo.log_outcome("501", "1h", Metrics(likes=0, views=5))
    repo.log_outcome("501", "24h", Metrics(likes=2, views=40))
    row = repo.posted_all()[0]
    assert (row["views"], row["likes"]) == (40, 2)


def test_archive_saves_full_tweet_text_and_its_picture_file(tmp_path):
    repo = _repo()
    _seed(repo, "11", "501", text="short cut off…")
    x = FakeX({"11": _tweet("the full long text", [
        {"kind": "photo", "url": "https://pbs.twimg.com/a.jpg", "alt": "shot"}])})
    web = FakeWeb({"https://pbs.twimg.com/a.jpg": (b"JPEGBYTES", "image/jpeg")})

    build_archive(repo, x, tmp_path, http_get=web)

    item = _rows(tmp_path, "SELECT * FROM items")[0]
    assert item["our_text"] == "our take on 11"
    assert item["our_url"] == "https://x.com/i/status/501"
    assert item["source_text"] == "the full long text"
    assert item["status"] == "ok"
    media = _rows(tmp_path, "SELECT * FROM media")[0]
    assert (tmp_path / media["path"]).read_bytes() == b"JPEGBYTES"
    assert media["alt"] == "shot"


def test_archive_saves_the_quoted_post_text_and_pictures(tmp_path):
    repo = _repo()
    _seed(repo, "11", "501")
    x = FakeX({"11": _tweet("look at this", ref_id="99"),
               "99": _tweet("the quoted original", [
                   {"kind": "photo", "url": "https://pbs.twimg.com/q.jpg", "alt": ""}])})
    web = FakeWeb({"https://pbs.twimg.com/q.jpg": (b"Q", "image/jpeg")})

    build_archive(repo, x, tmp_path, http_get=web)

    item = _rows(tmp_path, "SELECT * FROM items")[0]
    assert item["quoted_text"] == "the quoted original"
    assert _rows(tmp_path, "SELECT role FROM media")[0]["role"] == "quoted"


def test_archive_reads_a_second_post_to_strip_site_furniture(tmp_path):
    repo = _repo()
    _seed(repo, "web:abc", "502", url="https://site.com/blog/hooks-that-work-now")
    link = '<p><a href="/blog/how-to-test-ten-openings">Another post</a></p>'
    other = "<html><body><article><h1>Other</h1><p>Other words.</p>" + PITCH + "</article></body></html>"
    web = FakeWeb({
        "https://site.com/blog/hooks-that-work-now": (_page("", PITCH + link).encode(), "text/html"),
        "https://site.com/blog/how-to-test-ten-openings": (other.encode(), "text/html"),
    })
    build_archive(repo, None, tmp_path, http_get=web, log=lambda *_: None)
    text = _rows(tmp_path, "SELECT source_text FROM items")[0]["source_text"]
    assert "Real paragraph 0" in text and "Book a 30-minute call" not in text


def test_archive_still_saves_the_article_when_the_second_post_cannot_be_read(tmp_path):
    repo = _repo()
    _seed(repo, "web:abc", "502", url="https://site.com/blog/hooks-that-work-now")
    link = '<p><a href="/blog/how-to-test-ten-openings">Another post</a></p>'
    web = FakeWeb({"https://site.com/blog/hooks-that-work-now":
                   (_page("", link).encode(), "text/html")})
    build_archive(repo, None, tmp_path, http_get=web, log=lambda *_: None)
    row = _rows(tmp_path, "SELECT status, source_text FROM items")[0]
    assert row["status"] == "ok" and "Real paragraph 0" in row["source_text"]


def test_archive_saves_web_article_text_and_pictures(tmp_path):
    repo = _repo()
    _seed(repo, "web:abc", "502", text="our summary", url="https://site.com/blog/hooks")
    web = FakeWeb({
        "https://site.com/blog/hooks": (ARTICLE_HTML.encode(), "text/html"),
        "https://cdn.site.com/cover.jpg": (b"COVER", "image/jpeg"),
        "https://site.com/img/chart.png": (b"CHART", "image/png"),
    })

    build_archive(repo, FakeX({}), tmp_path, http_get=web)

    item = _rows(tmp_path, "SELECT * FROM items")[0]
    assert item["kind"] == "web"
    assert item["source_title"] == "Ten hooks that work"
    assert "Write ten hooks per product." in item["source_text"]
    assert item["stored_text"] == "our summary"
    paths = [m["path"] for m in _rows(tmp_path, "SELECT path FROM media ORDER BY id")]
    assert [p.rsplit(".", 1)[1] for p in paths] == ["jpg", "png"]
    assert (tmp_path / paths[1]).read_bytes() == b"CHART"


def test_archive_marks_a_deleted_tweet_gone_and_keeps_the_text_we_had(tmp_path):
    repo = _repo()
    _seed(repo, "13", "503", text="what we stored")
    build_archive(repo, FakeX({}, gone=["13"]), tmp_path, http_get=FakeWeb({}))
    item = _rows(tmp_path, "SELECT * FROM items")[0]
    assert item["status"] == "gone"
    assert item["source_text"] == "what we stored"


def test_archive_marks_an_unreachable_article_as_error_and_keeps_the_summary(tmp_path):
    repo = _repo()
    _seed(repo, "web:abc", "502", text="our summary", url="https://site.com/dead")
    build_archive(repo, FakeX({}), tmp_path, http_get=FakeWeb({}))
    item = _rows(tmp_path, "SELECT * FROM items")[0]
    assert item["status"] == "error"
    assert item["source_text"] == "our summary"
    assert "404" in item["status_note"]


def test_archive_rerun_fetches_only_what_is_new(tmp_path):
    repo = _repo()
    _seed(repo, "11", "501")
    x = FakeX({"11": _tweet("full one"), "12": _tweet("full two")})
    build_archive(repo, x, tmp_path, http_get=FakeWeb({}))
    _seed(repo, "12", "504")

    summary = build_archive(repo, x, tmp_path, http_get=FakeWeb({}))

    assert x.calls == [["11"], ["12"]]
    assert summary["fetched"] == 1
    assert len(_rows(tmp_path, "SELECT * FROM items")) == 2


def test_archive_without_x_access_leaves_tweets_pending_with_stored_text(tmp_path):
    repo = _repo()
    _seed(repo, "11", "501", text="what we stored")
    build_archive(repo, None, tmp_path, http_get=FakeWeb({}))
    item = _rows(tmp_path, "SELECT * FROM items")[0]
    assert item["status"] == "pending"
    assert item["source_text"] == "what we stored"


def test_archive_refreshes_view_counts_on_rerun(tmp_path):
    repo = _repo()
    _seed(repo, "11", "501")
    x = FakeX({"11": _tweet("full one")})
    build_archive(repo, x, tmp_path, http_get=FakeWeb({}))
    repo.log_outcome("501", "24h", Metrics(likes=3, views=70))
    build_archive(repo, x, tmp_path, http_get=FakeWeb({}))
    item = _rows(tmp_path, "SELECT * FROM items")[0]
    assert (item["views"], item["likes"]) == (70, 3)
    assert item["source_text"] == "full one"


# ------------------------------------------------------------------ whole videos
VIDEO_JSON = {
    "data": [{"id": "21", "author_id": "u1", "text": "watch",
              "attachments": {"media_keys": ["v1"]}}],
    "includes": {"media": [{
        "media_key": "v1", "type": "video",
        "preview_image_url": "https://pbs.twimg.com/v.jpg",
        "variants": [
            {"content_type": "application/x-mpegURL", "url": "https://video.twimg.com/pl.m3u8"},
            {"content_type": "video/mp4", "bit_rate": 632000, "url": "https://video.twimg.com/lo.mp4"},
            {"content_type": "video/mp4", "bit_rate": 2176000, "url": "https://video.twimg.com/hi.mp4?tag=12"},
        ]}]},
}
PREVIEW = {"kind": "video", "url": "https://pbs.twimg.com/v.jpg", "alt": ""}
VIDEO = {"kind": "video_file", "url": "https://video.twimg.com/hi.mp4?tag=12", "alt": ""}
VIDEO_WEB = {"https://pbs.twimg.com/v.jpg": (b"JPG", "image/jpeg"),
             "https://video.twimg.com/hi.mp4?tag=12": (b"MP4BYTES", "video/mp4")}


def test_parse_tweets_adds_the_best_quality_video_file_next_to_its_preview():
    found, _ = parse_tweets(VIDEO_JSON)
    assert found["21"]["media"] == [PREVIEW, VIDEO]


def test_archive_saves_the_whole_video_file(tmp_path):
    repo = _repo()
    _seed(repo, "21", "601")
    build_archive(repo, FakeX({"21": _tweet("watch", [PREVIEW, VIDEO])}), tmp_path,
                  http_get=FakeWeb(VIDEO_WEB))
    row = _rows(tmp_path, "SELECT * FROM media WHERE kind='video_file'")[0]
    assert row["path"].endswith(".mp4")
    assert (tmp_path / row["path"]).read_bytes() == b"MP4BYTES"


def test_archive_rerun_adds_videos_to_posts_archived_with_only_a_preview(tmp_path):
    repo = _repo()
    _seed(repo, "21", "601")
    old_x = FakeX({"21": _tweet("watch", [PREVIEW])})      # pre-video archive
    build_archive(repo, old_x, tmp_path, http_get=FakeWeb(VIDEO_WEB))
    conn = sqlite3.connect(tmp_path / "archive.db")
    conn.execute("DELETE FROM media WHERE kind='video_file'")
    conn.commit()
    conn.close()

    x = FakeX({"21": _tweet("watch", [PREVIEW, VIDEO])})
    build_archive(repo, x, tmp_path, http_get=FakeWeb(VIDEO_WEB))
    build_archive(repo, x, tmp_path, http_get=FakeWeb(VIDEO_WEB))

    assert x.calls == [["21"]]                              # re-read once, not twice
    row = _rows(tmp_path, "SELECT * FROM media WHERE kind='video_file'")[0]
    assert (tmp_path / row["path"]).read_bytes() == b"MP4BYTES"


def test_archive_rerun_adds_videos_from_quoted_posts(tmp_path):
    repo = _repo()
    _seed(repo, "11", "501")
    x = FakeX({"11": _tweet("look", ref_id="99"),
               "99": _tweet("quoted", [PREVIEW, VIDEO])})
    build_archive(repo, x, tmp_path, http_get=FakeWeb(VIDEO_WEB))
    conn = sqlite3.connect(tmp_path / "archive.db")
    conn.execute("DELETE FROM media WHERE kind='video_file'")
    conn.commit()
    conn.close()
    x.calls.clear()

    build_archive(repo, x, tmp_path, http_get=FakeWeb(VIDEO_WEB))

    assert x.calls == [["99"]]
    row = _rows(tmp_path, "SELECT * FROM media WHERE kind='video_file'")[0]
    assert row["role"] == "quoted" and row["path"].endswith(".mp4")


def test_archive_retries_a_video_download_that_failed_without_another_x_read(tmp_path):
    repo = _repo()
    _seed(repo, "21", "601")
    x = FakeX({"21": _tweet("watch", [PREVIEW, VIDEO])})
    build_archive(repo, x, tmp_path, http_get=FakeWeb({}))  # downloads all fail
    build_archive(repo, x, tmp_path, http_get=FakeWeb(VIDEO_WEB))
    assert x.calls == [["21"]]
    row = _rows(tmp_path, "SELECT * FROM media WHERE kind='video_file'")[0]
    assert (tmp_path / row["path"]).read_bytes() == b"MP4BYTES"


# ------------------------------------------------------------------ website data
import json

from xbot.archive import export_site


def _site(tmp_path):
    return json.loads((tmp_path / "data.json").read_text(encoding="utf-8"))["items"]


def test_site_data_lists_posts_newest_first_with_both_texts(tmp_path):
    repo = _repo()
    _seed(repo, "11", "501")
    _seed(repo, "12", "502")
    x = FakeX({"11": _tweet("full one"), "12": _tweet("full two")})
    build_archive(repo, x, tmp_path, http_get=FakeWeb({}))

    export_site(tmp_path)

    items = _site(tmp_path)
    assert [i["source_text"] for i in items] == ["full two", "full one"]
    assert items[0]["our_text"] == "our take on 12"
    assert items[0]["source_url"] == "https://x.com/teacher/status/12"
    assert items[0]["our_url"] == "https://x.com/i/status/502"


def test_site_data_uses_the_first_saved_picture_as_the_tile_and_flags_video(tmp_path):
    repo = _repo()
    _seed(repo, "21", "601")
    build_archive(repo, FakeX({"21": _tweet("watch", [PREVIEW, VIDEO])}), tmp_path,
                  http_get=FakeWeb(VIDEO_WEB))
    export_site(tmp_path)
    item = _site(tmp_path)[0]
    assert item["tile"].endswith(".jpg")
    assert item["has_video"] is True
    assert [m["kind"] for m in item["media"]] == ["video", "video_file"]
    assert item["media"][1]["path"].endswith(".mp4")


def test_site_data_leaves_out_files_that_failed_to_download(tmp_path):
    repo = _repo()
    _seed(repo, "11", "501")
    x = FakeX({"11": _tweet("t", [{"kind": "photo", "url": "https://pbs.twimg.com/a.jpg",
                                   "alt": ""}])})
    build_archive(repo, x, tmp_path, http_get=FakeWeb({}))
    export_site(tmp_path)
    item = _site(tmp_path)[0]
    assert item["media"] == [] and item["tile"] == "" and item["has_video"] is False


def test_download_that_hits_the_size_cap_is_not_saved_as_a_cut_off_file(tmp_path, monkeypatch):
    import xbot.archive as archive
    monkeypatch.setattr(archive, "MAX_BYTES", 4)
    repo = _repo()
    _seed(repo, "21", "601")
    web = FakeWeb({"https://pbs.twimg.com/v.jpg": (b"JPG", "image/jpeg"),
                   "https://video.twimg.com/hi.mp4?tag=12": (b"MP4B", "video/mp4")})
    build_archive(repo, FakeX({"21": _tweet("watch", [PREVIEW, VIDEO])}), tmp_path,
                  http_get=web)
    rows = {r["kind"]: r["path"] for r in _rows(tmp_path, "SELECT kind, path FROM media")}
    assert rows["video"].endswith(".jpg")
    assert rows["video_file"] == ""


def _video_json(rates):
    return {"data": [{"id": "31", "text": "v", "attachments": {"media_keys": ["v1"]}}],
            "includes": {"media": [{"media_key": "v1", "type": "video",
                                    "preview_image_url": "https://pbs.twimg.com/v.jpg",
                                    "variants": [{"content_type": "video/mp4", "bit_rate": r,
                                                  "url": f"https://video.twimg.com/{r}.mp4"}
                                                 for r in rates]}]}}


def test_parse_tweets_picks_the_best_video_that_is_not_oversized():
    found, _ = parse_tweets(_video_json([632000, 2176000, 10368000, 25128000]))
    assert found["31"]["media"][1]["url"] == "https://video.twimg.com/2176000.mp4"


def test_parse_tweets_takes_the_smallest_video_when_all_are_oversized():
    found, _ = parse_tweets(_video_json([25128000, 10368000]))
    assert found["31"]["media"][1]["url"] == "https://video.twimg.com/10368000.mp4"
