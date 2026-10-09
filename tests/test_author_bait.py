# tests/test_author_bait.py
"""author_bait experiment: 'question' arm ends with one question to the author
instead of the h/t tail (spec §3)."""
from xbot.commentary import generate as g
from xbot.commentary.safety import check_commentary
from xbot.config import NS
from xbot.models import Draft, Metrics, Post, Score, utcnow
from xbot.orchestrator import Orchestrator
from xbot.publish.publisher import body_budget, compose_text
from xbot.storage.sqlite_repo import SqliteRepository

CFG = NS({"posting": {"format": "mention", "per_day": 3, "per_run": 1},
          "llm": {"max_commentary_chars": 262},
          "safety": {"exclude": []},
          "voice": {"style": "growth_first"},
          "mode": {"autonomous": True},
          "ranking": {"teaching_weight": 0.65, "qa_gate": False},
          "scoring_weights": {"likes": 0.05, "reposts": 0.15, "velocity": 0.10,
                              "eng_per_follower": 0.25, "echo": 0.25,
                              "recency": 0.10, "topic_fit": 0.10},
          "recency_tau_hours": 8,
          "experiments": {"author_bait": {"enabled": True,
                                          "arms": ["tail", "question"]}}})
Q = {"author_bait": "question"}
T = {"author_bait": "tail"}


def _post(tid="1", handle="adriamatz"):
    # distinct text + handle per tid so the publish-time near-duplicate and
    # author-cooldown checks don't collapse two test posts into one
    return Post(tweet_id=tid, author_handle=handle if tid == "1" else f"{handle}{tid}",
                author_name="A",
                text=("same ad on 2 platforms, one cost 4x more per trial "
                      + {"1": "creative hooks retention onboarding",
                         "2": "pricing paywall funnel cohort"}.get(tid, f"topic{tid}")),
                created_at=utcnow(), author_follower_count=1000,
                metrics=Metrics(likes=3))


def test_user_prompt_asks_for_question_in_question_arm():
    p = g._user_prompt(_post(), CFG, arms=Q)
    assert "ONE short, specific question to @adriamatz" in p
    assert "End with: h/t" not in p
    p = g._user_prompt(_post(), CFG, arms=T)
    assert "End with: h/t @adriamatz" in p


def test_question_arm_gets_the_bigger_budget():
    assert body_budget(_post(), CFG, arms=Q) == 278          # prompt time: assigned arm
    assert body_budget(_post(), CFG, arms=T) == 280 - (len("h/t @adriamatz") + 4)
    assert body_budget(_post(), CFG) == body_budget(_post(), CFG, arms=T)
    # with the text, only a question that actually carries the @handle gets 278
    assert body_budget(_post(), CFG, arms=Q,
                       commentary="One ad.\n\nDid it hold, @adriamatz?") == 278
    assert body_budget(_post(), CFG, arms=Q, commentary="One ad.\n\nDid it hold?") \
        == body_budget(_post(), CFG, arms=T)


def test_effective_author_bait_comes_from_the_text():
    from xbot.publish.publisher import effective_author_bait
    assert effective_author_bait("One ad.\n\nDid it hold, @adriamatz?", "adriamatz", Q) \
        == "question"
    assert effective_author_bait("One ad.\n\nDid it hold?", "adriamatz", Q) == "tail"
    assert effective_author_bait("One ad.\n\nh/t @adriamatz", "adriamatz", Q) == "tail"
    assert effective_author_bait("Did it hold, @adriamatz?", "adriamatz", T) == "tail"
    assert effective_author_bait("Did it hold, @adriamatz?", "adriamatz", {}) == "tail"


def test_compose_keeps_question_and_adds_no_tail():
    d = Draft(tweet_id="1", commentary="One ad, 2 platforms.\n\nDid the gap hold, @adriamatz?",
              model="t", arms=Q)
    text, _ = compose_text(d, _post(), CFG)
    assert text.endswith("Did the gap hold, @adriamatz?")
    assert "h/t" not in text


def test_compose_still_appends_tail_when_handle_missing():
    d = Draft(tweet_id="1", commentary="One ad, 2 platforms.", model="t", arms=T)
    text, _ = compose_text(d, _post(), CFG)
    assert text.endswith("h/t @adriamatz")


def test_compose_credit_check_is_regex_safe():
    post = _post(handle="a_b.c")
    d = Draft(tweet_id="1", commentary="One ad.\n\nTrue, @a_b.c?", model="t", arms=Q)
    text, _ = compose_text(d, post, CFG)
    assert "h/t" not in text
    d = Draft(tweet_id="1", commentary="One ad.\n\nTrue, @a_bxc?", model="t", arms=Q)
    text, _ = compose_text(d, post, CFG)
    assert text.endswith("h/t @a_b.c")       # '.' must not act as a wildcard


def test_check_commentary_uses_arm_budget():
    body = "x" * 245 + "\n\nDid it hold, @adriamatz?"
    assert 262 < len(body) <= 278
    ok, why = check_commentary(_post(), body, CFG, arms=Q)
    assert ok, why
    ok, why = check_commentary(_post(), body, CFG, arms=T)
    assert not ok and why.startswith("too_long")


def test_check_commentary_gives_handleless_question_the_tail_budget():
    # The closing question lacks the @handle, so compose_text will append the
    # h/t tail: the draft must fit the tail budget, not 278.
    body = "x" * 255 + "\n\nDid it hold?"
    ok, why = check_commentary(_post(), body, CFG, arms=Q)
    assert not ok and why == f"too_long:{len(body)}>262", why


class _Gen:
    """Records the arms it was asked to write for."""
    def __init__(self):
        self.calls = []

    def generate(self, post, allow_thread=False, arms=None):
        self.calls.append(dict(arms or {}))
        end = f"Did it hold, @{post.author_handle}?" if (arms or {}).get("author_bait") == "question" \
            else f"h/t @{post.author_handle}"
        return Draft(tweet_id=post.tweet_id, model="fake",
                     commentary=f"One ad, 2 platforms. One cost 4x more per trial.\n\n{end}")


class _Pub:
    def __init__(self):
        self.texts = []

    def publish(self, draft, post):
        self.texts.append(draft.commentary)
        return {"ok": True, "id": f"our_{post.tweet_id}"}


class _Judge:
    """All posts below are pre-judged; nothing should reach the LLM judge."""
    def score_batch(self, posts):
        return {}


def _orch(repo):
    o = object.__new__(Orchestrator)
    o.cfg, o.repo = CFG, repo
    o.generator, o.prescreen, o.judge = _Gen(), None, _Judge()
    o.publisher, o.judge_reasons = _Pub(), {}
    return o


def test_draft_and_publish_carry_arms_to_features():
    repo = SqliteRepository(":memory:"); repo.init_schema()
    o = _orch(repo)
    for tid in ("1", "2"):
        p = _post(tid); repo.upsert_post(p)
        repo.save_score(Score(tweet_id=tid, topic_fit=0.9, quote_worthy=0.8,
                              quote_score=0.8, judged=True))
    created = o.make_drafts(limit=2)
    assert [c["ok"] for c in created] == [True, True]
    assert [c["draft"].arms["author_bait"] for c in created] == ["tail", "question"]
    assert o.publish_due()["status"] == "posted"
    assert o.publish_due()["status"] == "posted"
    rows = repo.conn.execute(
        "SELECT arms FROM post_features ORDER BY our_tweet_id").fetchall()
    assert sorted(r["arms"] for r in rows) == sorted(
        ['{"author_bait": "tail"}', '{"author_bait": "question"}'])


def test_credit_check_ignores_longer_handles():
    # '@adriamatz' must not be satisfied by '@adriamatzz' / '@adriamatz2'
    for other in ("@adriamatzz", "@adriamatz2"):
        d = Draft(tweet_id="1", commentary=f"One ad.\n\nTrue, {other}?", model="t", arms=Q)
        text, _ = compose_text(d, _post(), CFG)
        assert text.endswith("h/t @adriamatz")


def test_trim_that_cuts_the_question_relabels_to_tail():
    body = ("One ad ran on 2 platforms.\n\n"
            "One cost 4x more per trial.\n\n"
            "The cheap one used a plain founder voice and one clear ask.\n\n"
            "The pricey one used a polished studio look and many asks.\n\n"
            "People trust the plain voice more, so it wins.\n\n"
            "Steal that and test the plain voice first before you scale.")
    draft = Draft(tweet_id="1", model="t", arms=dict(Q),
                  commentary=f"{body}\n\nDid the gap hold, @adriamatz?")
    assert len(draft.commentary) > 278
    o = _orch(SqliteRepository(":memory:"))
    out, ok, notes = o._vet_commentary(_post(), draft)
    assert ok and notes == "ok(trimmed)", notes
    assert out.arms["author_bait"] == "tail"
    text, _ = compose_text(out, _post(), CFG)
    assert text.endswith("h/t @adriamatz") and len(text) <= 280


def test_thread_instructions_follow_the_arm():
    p = g._user_prompt(_post(), CFG, allow_thread=True, arms=Q)
    assert "INCLUDING the h/t tail" not in p and "INCLUDING the \"h/t" not in p
    assert "closing question to @adriamatz" in p
    p = g._user_prompt(_post(), CFG, allow_thread=True, arms=T)
    assert 'INCLUDING the "h/t @adriamatz" tail' in p


def test_web_post_ignores_question_arm():
    web = Post(tweet_id="web:abc", author_handle="blog", author_name="Blog",
               text="A tactic about hooks.", created_at=utcnow(),
               author_follower_count=0, metrics=Metrics())
    p = g._user_prompt(web, CFG, arms=Q)
    assert "question to @" not in p and "ONE short, specific question" not in p
    assert body_budget(web, CFG, arms=Q) == body_budget(web, CFG)


# A 276-char question-arm draft whose closing question forgot the @handle
# (whole-branch review F1): one sentence per line, the what-to-do line 4th.
WHAT_TO_DO = "Before you scale, divide what you spend by the trials it brings in on each."
HANDLELESS = ("A founder ran one ad on two platforms for his free trial.\n\n"
              "On one, each trial cost him four times more.\n\n"
              "Same ad, same offer, so only the platform differed.\n\n"
              f"{WHAT_TO_DO}\n\n"
              "Did the gap hold as you upped the budget?")


def test_handleless_question_draft_is_relabelled_tail_and_composes_under_280():
    assert len(HANDLELESS) == 276
    draft = Draft(tweet_id="1", model="t", arms=dict(Q), commentary=HANDLELESS)
    o = _orch(SqliteRepository(":memory:"))
    out, ok, notes = o._vet_commentary(_post(), draft)
    assert ok, notes
    assert out.arms["author_bait"] == "tail"
    text, _ = compose_text(out, _post(), CFG)
    assert len(text) <= 280
    assert text.endswith("\n\nh/t @adriamatz")
    assert WHAT_TO_DO in text


def _publish_one(o, repo, draft):
    p = _post(); repo.upsert_post(p)
    repo.save_score(Score(tweet_id="1", topic_fit=0.9, quote_worthy=0.8,
                          quote_score=0.8, judged=True))
    draft.safety_passed = True
    draft_id = repo.add_draft(draft)
    o._publish(draft_id, draft, p)
    return repo.conn.execute("SELECT arms FROM post_features").fetchone()["arms"]


def test_proper_question_draft_stays_question_through_vet_compose_features():
    repo = SqliteRepository(":memory:"); repo.init_schema()
    o = _orch(repo)
    draft = Draft(tweet_id="1", model="t", arms=dict(Q),
                  commentary="One ad ran on 2 platforms.\n\n"
                             "One cost 4x more per trial.\n\n"
                             "Test the cheap one first.\n\nDid the gap hold, @adriamatz?")
    out, ok, notes = o._vet_commentary(_post(), draft)
    assert ok and out.arms["author_bait"] == "question", notes
    text, _ = compose_text(out, _post(), CFG)
    assert text.endswith("Did the gap hold, @adriamatz?") and "h/t" not in text
    assert _publish_one(o, repo, out) == '{"author_bait": "question"}'


def test_features_record_tail_when_composed_post_carries_the_tail():
    # A question-labelled draft queued before the fix, with no @handle: the
    # composed post ends with the h/t tail, so the recorded arm is tail.
    repo = SqliteRepository(":memory:"); repo.init_schema()
    o = _orch(repo)
    draft = Draft(tweet_id="1", model="t", arms=dict(Q),
                  commentary="One ad, 2 platforms.\n\nDid the gap hold?")
    assert _publish_one(o, repo, draft) == '{"author_bait": "tail"}'
