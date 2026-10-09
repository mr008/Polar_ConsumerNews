# tests/test_volume.py
"""volume period test (spec §4): thresholds -0.05, per_run 2, one extra
fabricated-number revision that names the allowed numbers."""
from xbot.config import NS
from xbot.models import Draft, Metrics, Post, Score, utcnow
from xbot.orchestrator import Orchestrator
from xbot.select.rules import evaluate
from xbot.storage.sqlite_repo import SqliteRepository


def _cfg(on):
    return NS({"thresholds": {"topic_fit_min": 0.45, "quote_worthy_min": 0.35},
               "posting": {"per_day": 3, "per_run": 1, "format": "mention"},
               "safety": {"exclude": []}, "llm": {"max_commentary_chars": 262},
               "mode": {"autonomous": True}, "ranking": {"qa_gate": False},
               "experiments": {"volume": {"enabled": on, "period": True}}})


def _repo():
    r = SqliteRepository(":memory:"); r.init_schema(); return r


def _post(tid="1", text=None):
    return Post(tweet_id=tid, author_handle=f"u{tid}", author_name="U",
                text=text or f"he spent 30 a day and got 4 times more trials {tid}",
                created_at=utcnow(), author_follower_count=100, metrics=Metrics())


def test_thresholds_unchanged_when_off_and_lowered_when_on():
    repo = _repo()
    s = Score(tweet_id="1", topic_fit=0.42, quote_worthy=0.32)
    ok, why = evaluate(_post(), s, _cfg(False), repo)
    assert not ok and why.startswith("low_topic_fit")
    ok, why = evaluate(_post(), s, _cfg(True), repo)
    assert ok, why


def _orch(cfg, repo, gen=None):
    o = object.__new__(Orchestrator)
    o.cfg, o.repo, o.judge_reasons = cfg, repo, {}
    o.generator, o.prescreen, o.judge = gen, None, None
    class _Pub:
        def publish(self, d, p): return {"ok": True, "id": f"our_{p.tweet_id}"}
    o.publisher = _Pub()
    return o


def _queue(repo, tid):
    # distinct tokens per tid so the publish-time near-duplicate check keeps both
    repo.upsert_post(_post(tid, text=f"post {tid} growth tactic alpha{tid} beta{tid} gamma{tid}"))
    repo.save_score(Score(tweet_id=tid, quote_score=0.8, judged=True))
    repo.add_draft(Draft(tweet_id=tid, commentary="a sharp growth take worth stealing",
                         model="t", safety_passed=True))


def test_per_run_is_two_when_on():
    repo = _repo(); _queue(repo, "1"); _queue(repo, "2")
    assert _orch(_cfg(False), repo).publish_due()["count"] == 1
    repo = _repo(); _queue(repo, "1"); _queue(repo, "2")
    assert _orch(_cfg(True), repo).publish_due()["count"] == 2


class _Gen:
    """First draft fabricates '10'; first revision still does; second fixes it."""
    def __init__(self):
        self.feedback = []

    def generate(self, post, allow_thread=False, arms=None):
        return Draft(tweet_id=post.tweet_id, model="t",
                     commentary="He spent 30 a day and got 10 times more trials.")

    def revise(self, post, previous, feedback, arms=None):
        self.feedback.append(feedback)
        n = len(self.feedback)
        text = ("He spent 30 a day and got 10 times more trials." if n == 1
                else "He spent 30 a day and got 4 times more trials.")
        return Draft(tweet_id=post.tweet_id, model="t", commentary=text)


def test_second_revision_only_when_volume_on_and_feedback_names_numbers():
    gen = _Gen()
    o = _orch(_cfg(True), _repo(), gen)
    draft, ok, notes = o._vet_commentary(_post(), gen.generate(_post()))
    assert ok, notes
    assert "30" in gen.feedback[0] and "4" in gen.feedback[0]   # allowed numbers listed
    assert len(gen.feedback) == 2
    gen = _Gen()
    o = _orch(_cfg(False), _repo(), gen)
    _, ok, notes = o._vet_commentary(_post(), gen.generate(_post()))
    assert not ok and notes.startswith("fabricated_number:10")
    assert len(gen.feedback) == 1


def test_revision_feedback_too_long_is_plain_story_and_arm_aware():
    o = _orch(_cfg(True), _repo())
    from xbot.publish.publisher import body_budget
    post = _post()
    fb = o._revision_feedback(post, "too_long:300>262", {"author_bait": "question"})
    assert str(body_budget(post, o.cfg, {"author_bait": "question"})) in fb
    assert "bullet" not in fb.lower() and "hook" not in fb.lower()


def test_revision_feedback_qa_question_arm_keeps_closing_question():
    o = _orch(_cfg(True), _repo())
    post = _post()
    fb = o._revision_feedback(post, "qa:too vague", {"author_bait": "question"})
    assert "closing question to @u1" in fb
    assert "never address the author" not in fb.lower()


def test_revision_feedback_qa_tail_arm_keeps_never_address():
    o = _orch(_cfg(True), _repo())
    post = _post()
    for arms in (None, {"author_bait": "tail"}):
        fb = o._revision_feedback(post, "qa:too vague", arms)
        assert "never address the author" in fb.lower()
        assert "closing question" not in fb
