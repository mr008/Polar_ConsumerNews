"""Wires the pipeline: collect -> score -> draft -> (review) -> publish.

The collector flow (collect/score/draft) runs often; the publisher flow
(publish_due / approve) runs on the posting schedule. Review gating is controlled
by mode.autonomous.
"""
from __future__ import annotations

import os

from .commentary import check_commentary, get_generator
from .config import NS, kill_switch_active
from .ingest import SampleSource
from .models import Draft, Post, Score
from .publish import get_publisher
from .score import score_posts
from .score.teaching_judge import get_teaching_judge, prefilter_for_judge
from .select import select_all
from .storage.repo import Repository


def make_source(cfg: NS):
    if cfg.get("mode.source", "sample") == "api":
        from .ingest.api_source import ApiSourceAdapter
        missing = [k for k in ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN",
                               "X_ACCESS_TOKEN_SECRET", "X_USER_ID")
                   if not os.environ.get(k)]
        if missing:
            raise SystemExit(f"mode.source=api needs these in .env: {', '.join(missing)}")
        return ApiSourceAdapter(max_posts_per_day=cfg.get("scoping.max_posts_per_day", 120))
    return SampleSource(cfg.get("ops.fixture_path", "fixtures/sample_posts.json"))


class Orchestrator:
    def __init__(self, cfg: NS, repo: Repository):
        self.cfg = cfg
        self.repo = repo
        self.source = make_source(cfg)
        self.generator = get_generator(cfg)
        self.judge = get_teaching_judge(cfg)
        self.publisher = get_publisher(cfg)
        self.judge_reasons: dict[str, str] = {}

    # ---------- collector flow ----------
    def collect(self) -> int:
        cap = self.cfg.get("scoping.max_posts_per_day", 120)
        # since_id = read-dedup: only fetch (and pay for) posts newer than the
        # newest one already in the DB.
        posts = self.source.fetch_timeline(cap, since_id=self.repo.max_seen_tweet_id())
        for p in posts:
            self.repo.upsert_post(p)
            if not self.repo.has_posted(p.tweet_id):
                self.repo.set_candidate(p.tweet_id, "watching")
        self.repo.log_run("collect", read=len(posts))  # n paid API reads this run
        return len(posts)

    def score(self) -> tuple[list[Post], list[Score]]:
        posts = self.repo.recent_posts(72)
        judge_posts = prefilter_for_judge(posts, self.cfg)
        self.judged_count = len(judge_posts)  # posts sent to the LLM judge
        judged = self.judge.score_batch(judge_posts)            # semantic topic + teaching
        self.judge_reasons = {tid: reason for tid, (_, _, reason) in judged.items()}
        teaching = {tid: score for tid, (score, _, _) in judged.items()}
        topic = {tid: (1.0 if on_topic else 0.0) for tid, (_, on_topic, _) in judged.items()}
        scores = score_posts(posts, self.cfg, self.repo,
                             teaching_scores=teaching, topic_scores=topic)
        for s in scores:
            self.repo.save_score(s)
            if not self.repo.has_posted(s.tweet_id):
                self.repo.set_candidate(s.tweet_id, "scored")
        return posts, scores

    def make_drafts(self, limit: int | None = None) -> list[dict]:
        posts, scores = self.score()
        score_map = {s.tweet_id: s for s in scores}
        eligible, skipped = select_all(posts, score_map, self.cfg, self.repo)
        for post, reason in skipped:
            if not self.repo.has_posted(post.tweet_id):
                self.repo.set_candidate(post.tweet_id, "skipped", reason)

        # Drop drafts whose moment has passed, then TOP UP the standby queue to
        # per_day+1: enough that every window has one fallback if the best post
        # fails, without paying for commentary that will never be used.
        self.repo.expire_stale_drafts(self.cfg.get("posting.draft_max_age_hours", 48))
        pending_ids = {tid for tid, _, _ in self.repo.pending_drafts()}
        if limit is None:
            queue_target = self.cfg.get("posting.per_day", 3) + 1
            limit = max(0, queue_target - len(pending_ids))
        created: list[dict] = []
        for score, post in eligible:
            if len(created) >= limit:
                break
            if post.tweet_id in pending_ids or self.repo.has_posted(post.tweet_id):
                continue
            draft = self.generator.generate(post)
            ok, notes = check_commentary(post, draft.commentary, self.cfg)
            draft.safety_passed = ok
            draft.safety_notes = notes
            draft_id = self.repo.add_draft(draft, status="pending" if ok else "blocked")
            self.repo.set_candidate(post.tweet_id, "drafted" if ok else "skipped",
                                    "" if ok else notes)
            created.append({"draft_id": draft_id, "draft": draft, "post": post,
                            "score": score, "ok": ok, "notes": notes})
        self.repo.log_run("draft", judged=getattr(self, "judged_count", 0),
                          drafted=len(created))
        return created

    # ---------- publisher flow ----------
    def publish_due(self) -> dict:
        result = self._publish_due()
        detail = result["status"]
        for f in result.get("failed", []):
            detail += f" | draft #{f['draft_id']}: {f['error'][:80]}"
        self.repo.log_run("publish", posted=result.get("count", 0), detail=detail)
        return result

    def _publish_due(self) -> dict:
        if kill_switch_active(self.cfg):
            return {"status": "killed"}
        per_day = self.cfg.get("posting.per_day", 3)
        remaining = per_day - self.repo.count_posted_today()
        if remaining <= 0:
            return {"status": "cap_reached", "posted_today": self.repo.count_posted_today()}
        if not self.cfg.get("mode.autonomous", False):
            return {"status": "review_required", "pending": len(self.repo.pending_drafts())}
        # Each scheduled window posts ONE draft (3 windows/day); per_day stays the
        # hard cap so a manual re-run can't overshoot.
        self.repo.expire_stale_drafts(self.cfg.get("posting.draft_max_age_hours", 48))
        budget = min(remaining, self.cfg.get("posting.per_run", 1))
        results, failures = [], []
        for draft_id, draft, post in self._ranked_pending():
            if len(results) >= budget:
                break
            if not draft.safety_passed:
                continue
            if self.repo.has_posted(post.tweet_id) or self.repo.has_posted(post.canonical_id):
                self.repo.set_draft_status(draft_id, "duplicate")
                continue
            try:
                results.append(self._publish(draft_id, draft, post))
            except Exception as e:  # skip-on-failure: mark it and try the next-best draft
                err = f"{type(e).__name__}: {e}"
                self.repo.set_draft_status(draft_id, "failed", err[:500])
                failures.append({"draft_id": draft_id, "tweet_id": post.tweet_id,
                                 "error": err[:200]})
        status = "posted" if results else ("all_failed" if failures else "queue_empty")
        return {"status": status, "count": len(results), "results": results,
                "failed": failures}

    def _ranked_pending(self) -> list[tuple[int, Draft, Post]]:
        """Pending drafts, best quote_score first — the queue is stored FIFO, but
        each window should post the strongest candidate available right now."""
        def quote_score(row) -> float:
            s = self.repo.get_score(row[2].tweet_id)
            return s.quote_score if s else 0.0
        return sorted(self.repo.pending_drafts(), key=quote_score, reverse=True)

    def approve(self, draft_id: int) -> dict:
        row = self.repo.get_draft(draft_id)
        if not row:
            return {"status": "not_found"}
        draft, post = row
        if self.repo.has_posted(post.tweet_id):
            self.repo.set_draft_status(draft_id, "duplicate")
            return {"status": "duplicate"}
        if not draft.safety_passed:
            return {"status": "blocked", "notes": draft.safety_notes}
        return self._publish(draft_id, draft, post)

    def reject(self, draft_id: int, reason: str = "manual") -> dict:
        self.repo.set_draft_status(draft_id, "rejected", reason)
        return {"status": "rejected", "draft_id": draft_id}

    def _publish(self, draft_id: int, draft: Draft, post: Post) -> dict:
        result = self.publisher.publish(draft, post)
        our_id = result.get("id", "")
        self.repo.log_posted(post.tweet_id, our_id, post.author_handle, post.text, draft.commentary)
        self.repo.set_draft_status(draft_id, "posted")
        self.repo.set_candidate(post.tweet_id, "posted")
        return {"tweet_id": post.tweet_id, "our_id": our_id, "author": post.author_handle}

    # ---------- report ----------
    def report(self) -> dict:
        posts = self.repo.recent_posts(72)
        return {
            "posts_seen": len(posts),
            "posted_today": self.repo.count_posted_today(),
            "pending_drafts": len(self.repo.pending_drafts()),
            "watching": len(self.repo.candidates("watching")),
            "skipped": len(self.repo.candidates("skipped")),
            "activity": {
                "posted": self.repo.activity_posted(72),
                "problems": self.repo.activity_drafts(["failed", "blocked", "stale"], 72),
                "runs": self.repo.recent_runs(72),
            },
        }
