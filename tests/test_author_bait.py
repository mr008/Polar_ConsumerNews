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
    assert body_budget(_post(), CFG, arms=Q) == 278
    assert body_budget(_post(), CFG, arms=T) == 280 - (len("h/t @adriamatz") + 4)
    assert body_budget(_post(), CFG) == body_budget(_post(), CFG, arms=T)


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
    body = "x" * 270
    ok, why = check_commentary(_post(), body, CFG, arms=Q)
    assert ok, why
    ok, why = check_commentary(_post(), body, CFG, arms=T)
    assert not ok and why.startswith("too_long")


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
