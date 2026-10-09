"""Draft -> publish offline with the REAL config.yaml (experiments flipped on),
the template generator and the dry-run publisher: drafts get arms, a card PNG
is written, features carry the arms actually posted, `experiments` renders.
The full Orchestrator() constructor needs an LLM key for the judge, so the
object is assembled by hand the way tests/test_publish_due.py does."""
import pathlib

import pytest

from xbot.commentary.generate import TemplateCommentaryGenerator
from xbot.config import NS, load_config
from xbot.experiments import render, summarize
from xbot.models import Metrics, Post, Score, utcnow
from xbot.orchestrator import Orchestrator
from xbot.publish.dryrun import DryRunPublisher
from xbot.storage.sqlite_repo import SqliteRepository

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _cfg():
    data = load_config(ROOT / "config.yaml").as_dict()
    data["mode"] = {**data.get("mode", {}), "source": "sample",
                    "publisher": "dry_run", "autonomous": True}
    data["ranking"] = {**data.get("ranking", {}), "qa_gate": False,
                       "draft_prescreen": False}
    data["posting"] = {**data.get("posting", {}), "card_handle": "polar",
                       "max_source_age_hours": 0}
    for name in ("image_card", "author_bait", "volume"):
        data["experiments"][name]["enabled"] = True
    return NS(data)


class _NoopJudge:
    """Every seeded score is already judged=True (judge-once), so nothing is sent."""
    def score_batch(self, posts):
        return {}


def _orch(cfg, repo):
    o = object.__new__(Orchestrator)
    o.cfg, o.repo, o.judge_reasons = cfg, repo, {}
    o.source, o.judge, o.prescreen = None, _NoopJudge(), None
    o.generator = TemplateCommentaryGenerator(cfg)
    o.publisher = DryRunPublisher(cfg)
    return o


def test_offline_draft_and_publish_with_all_experiments(tmp_path, monkeypatch):
    pytest.importorskip("PIL")
    monkeypatch.chdir(tmp_path)          # data/cards/ lands here, not in the repo
    cfg = _cfg()
    repo = SqliteRepository(":memory:"); repo.init_schema()
    # Four clearly different sources: the publish-time near-duplicate check
    # (similarity 0.82) would collapse look-alike texts into one post.
    sources = [
        "1. post one short video a day\n2. reuse the winner across accounts\n3. track views weekly",
        "1. put the paywall after the aha moment\n2. test two prices\n3. keep the free tier tiny",
        "1. reply to bigger accounts early\n2. ask one real question\n3. never paste a link",
        "1. write the hook first\n2. cut every sentence that explains the hook\n3. ship daily",
    ]
    for i, text in enumerate(sources, 1):
        p = Post(tweet_id=str(i), author_handle=f"maker{i}", author_name="M",
                 text=text, created_at=utcnow(), author_follower_count=5000,
                 metrics=Metrics(likes=20))
        repo.upsert_post(p)
        repo.save_score(Score(tweet_id=str(i), topic_fit=0.9, quote_worthy=0.8,
                              quote_score=0.8, judged=True))
    orch = _orch(cfg, repo)
    created = orch.make_drafts()
    oks = [c for c in created if c["ok"]]
    assert len(oks) >= 2, [c["notes"] for c in created]
    assert {c["draft"].arms["image_card"] for c in oks} == {"text", "card"}
    assert {c["draft"].arms["author_bait"] for c in oks} == {"tail", "question"}
    res = orch.publish_due()
    assert res["status"] == "posted" and res["count"] == 2      # volume: per_run 2
    rows = repo.conn.execute("SELECT arms FROM post_features").fetchall()
    assert len(rows) == 2 and all(r["arms"] for r in rows)
    assert list((tmp_path / "data" / "cards").glob("*.png"))    # one card arm posted
    text = render(summarize(repo.experiment_rows(), cfg))
    assert "image_card" in text and "volume" in text
