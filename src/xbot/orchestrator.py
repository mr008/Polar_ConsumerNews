"""Wires the pipeline: collect -> score -> draft -> (review) -> publish.

The collector flow (collect/score/draft) runs often; the publisher flow
(publish_due / approve) runs on the posting schedule. Review gating is controlled
by mode.autonomous.
"""
from __future__ import annotations

import os

from .commentary import check_commentary, get_generator
from .config import NS, kill_switch_active
from .dedup import author_in_cooldown, is_near_duplicate
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
        # Circuit breaker: hard monthly read budget. Protects against API
        # weirdness (e.g. since_id ignored) silently running up the bill.
        budget = self.cfg.get("scoping.monthly_read_budget", 0)
        if budget:
            used = self.repo.reads_this_month()
            if used >= budget:
                msg = f"circuit_breaker: {used}/{budget} reads this month — collect skipped"
                print(f"  [collect] {msg}")
                self.repo.log_run("collect", read=0, detail=msg)
                return 0
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
        self._refresh_reads = self._refresh_metrics(posts)

        # JUDGE-ONCE: teaching value doesn't change after publication, so a post
        # is judged exactly one time. Stored verdicts (judged=True) are reused;
        # only never-judged posts go to the LLM. This also fixes the clobbering
        # bug where re-scoring overwrote judge values with 0 for any post that
        # fell out of the batch (catastrophically: ALL posts on a judge outage).
        teaching: dict[str, float] = {}
        topic: dict[str, float] = {}
        judged_ids: set[str] = set()
        unjudged: list[Post] = []
        for p in posts:
            s = self.repo.get_score(p.tweet_id)
            if s is not None and s.judged:
                teaching[p.tweet_id] = s.quote_worthy
                topic[p.tweet_id] = s.topic_fit
                judged_ids.add(p.tweet_id)
            else:
                unjudged.append(p)

        judge_posts = prefilter_for_judge(unjudged, self.cfg)
        self.judged_count = len(judge_posts)  # posts sent to the LLM judge this run
        verdicts = self.judge.score_batch(judge_posts)  # {} on judge outage — no clobber
        self.judge_reasons = {tid: reason for tid, (_, _, reason) in verdicts.items()}
        for tid, (tscore, topic_fit, reason) in verdicts.items():
            teaching[tid] = tscore
            # GRADED topic fit (0.0-1.0); legacy bool verdicts coerce cleanly.
            topic[tid] = max(0.0, min(1.0, float(topic_fit)))
            if reason != "not judged":  # judge actually returned a verdict
                judged_ids.add(tid)

        scores = score_posts(posts, self.cfg, self.repo,
                             teaching_scores=teaching, topic_scores=topic)
        for s in scores:
            s.judged = s.tweet_id in judged_ids
            self.repo.save_score(s)
            if not self.repo.has_posted(s.tweet_id):
                self.repo.set_candidate(s.tweet_id, "scored")
        return posts, scores

    def _refresh_metrics(self, posts: list[Post]) -> int:
        """Re-poll live engagement for the queue + top-scored candidates (paid,
        small, optional). since_id collection never refreshes metrics, so without
        this the engagement leg of ranking is frozen at first sight."""
        top_n = self.cfg.get("scoping.metrics_refresh_top", 0)
        fetch = getattr(getattr(self, "source", None), "fetch_metrics", None)
        if not top_n or fetch is None:
            return 0
        ids = [p.tweet_id for _, _, p in self.repo.pending_drafts()]
        def stored_score(p: Post) -> float:
            s = self.repo.get_score(p.tweet_id)
            return s.quote_score if s else 0.0
        for p in sorted(posts, key=stored_score, reverse=True):
            if len(ids) >= top_n:
                break
            if p.tweet_id not in ids:
                ids.append(p.tweet_id)
        try:
            fresh = fetch(ids[:top_n])
        except Exception as e:
            print(f"  [metrics] refresh failed ({type(e).__name__}) — using stored metrics")
            return 0
        by_id = {p.tweet_id: p for p in posts}
        for tid, m in fresh.items():
            self.repo.add_metrics(tid, m)       # history row → real velocity slope
            if tid in by_id:
                by_id[tid].metrics = m          # this run scores live numbers too
        return len(fresh)

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
        # One Sonnet attempt per post EVER — a blocked draft must not be
        # re-rolled every collect run.
        drafted_ids = self.repo.drafted_tweet_ids()
        if limit is None:
            queue_target = self.cfg.get("posting.per_day", 3) + 1
            limit = max(0, queue_target - len(self.repo.pending_drafts()))
            if limit == 0:
                limit = self._maybe_supersede(eligible, drafted_ids)
        created: list[dict] = []
        n_ok = 0  # blocked drafts don't consume the top-up budget
        for score, post in eligible:
            if n_ok >= limit:
                break
            if post.tweet_id in drafted_ids or self.repo.has_posted(post.tweet_id):
                continue
            # Adaptive threads: only substantial sources earn the multi-part
            # treatment; the generator still falls back to a single post if it
            # can't extract 3+ concrete steps.
            allow_thread = (bool(self.cfg.get("posting.adaptive_threads", False))
                            and score.quote_worthy
                            >= self.cfg.get("posting.thread_min_teaching", 0.75))
            draft = self.generator.generate(post, allow_thread=allow_thread)

            # SKIP sentinel: the generator's only sanctioned refusal. Store a
            # blocked draft row (keeps one-attempt-per-post-EVER) but never
            # treat the refusal as commentary.
            if draft.commentary.strip().lower().startswith("skip:"):
                reason = draft.commentary.strip()[5:].strip()[:80]
                draft.safety_passed = False
                draft.safety_notes = f"no_material:{reason}"
                draft_id = self.repo.add_draft(draft, status="blocked")
                self.repo.set_candidate(post.tweet_id, "skipped",
                                        f"no_material:{reason}")
                created.append({"draft_id": draft_id, "draft": draft, "post": post,
                                "score": score, "ok": False,
                                "notes": draft.safety_notes})
                continue

            draft, ok, notes = self._vet_commentary(post, draft)
            draft.safety_passed = ok
            draft.safety_notes = notes
            draft_id = self.repo.add_draft(draft, status="pending" if ok else "blocked")
            self.repo.set_candidate(post.tweet_id, "drafted" if ok else "skipped",
                                    "" if ok else notes)
            created.append({"draft_id": draft_id, "draft": draft, "post": post,
                            "score": score, "ok": ok, "notes": notes})
            if ok:
                n_ok += 1
        self.repo.log_run("draft", read=getattr(self, "_refresh_reads", 0),
                          judged=getattr(self, "judged_count", 0),
                          drafted=len(created))
        return created

    def _vet_commentary(self, post: Post, draft: Draft) -> tuple[Draft, bool, str]:
        """Deterministic safety gates + LLM QA gate, with ONE revision attempt.
        Too-long / fabricated-number / QA-rejected drafts get a single editor-
        feedback rewrite; a STILL-too-long rewrite gets a deterministic trim
        (trimming beats blocking — too_long was 50% of all draft blocks)."""
        from .commentary.qa import qa_commentary
        notes = ""
        for attempt in (1, 2):
            ok, notes = check_commentary(post, draft.commentary, self.cfg,
                                         parts=draft.parts)
            if ok:
                qa_ok, qa_issue = qa_commentary(post, draft.full_text, self.cfg)
                if qa_ok:
                    return draft, True, "ok"
                notes = qa_issue
            if attempt == 2:
                break
            revise = getattr(self.generator, "revise", None)
            if revise is None:  # offline template generator can't rewrite
                break
            draft = revise(post, draft.full_text, self._revision_feedback(post, notes))
            if draft.commentary.strip().lower().startswith("skip:"):
                return draft, False, f"no_material:{draft.commentary.strip()[5:].strip()[:80]}"

        # Last resort for a PURE length failure: deterministic trim + re-check.
        if notes.startswith("too_long"):
            from .publish.publisher import body_budget, smart_trim
            draft.commentary = smart_trim(draft.commentary, body_budget(post, self.cfg))
            ok, notes2 = check_commentary(post, draft.commentary, self.cfg,
                                          parts=draft.parts)
            if ok:
                return draft, True, "ok(trimmed)"
            notes = notes2
        return draft, False, notes

    def _revision_feedback(self, post: Post, notes: str) -> str:
        if notes.startswith("too_long"):
            from .publish.publisher import body_budget
            return (f"It is too long ({notes}). Rewrite to UNDER "
                    f"{body_budget(post, self.cfg)} characters: tighter hook, "
                    f"max 2 bullets, one-line takeaway.")
        if notes.startswith("fabricated_number"):
            return ("You used a number that is not in the source post. Remove it; "
                    "use only numbers that literally appear in the source.")
        if notes.startswith("qa:"):
            return (f"It failed editorial review: {notes[3:]}. Write a proper "
                    "teaching breakdown of the tactic in the source — never address "
                    "the author or reader, never ask for more content.")
        return f"It was rejected ({notes}). Fix that while keeping every other rule."

    def _maybe_supersede(self, eligible, drafted_ids) -> int:
        """Queue is full — but if today's best new candidate clearly outranks the
        weakest pending draft, retire the weak one and free a slot (freshness)."""
        margin = self.cfg.get("posting.supersede_margin", 0.15)
        pending = self._ranked_pending()
        if not pending:
            return 0
        weakest_id, _, weakest_post = pending[-1]
        ws = self.repo.get_score(weakest_post.tweet_id)
        weakest = ws.quote_score if ws else 0.0
        for s, p in eligible:  # sorted best-first; only the top one can supersede
            if p.tweet_id in drafted_ids or self.repo.has_posted(p.tweet_id):
                continue
            if s.quote_score >= weakest + margin:
                self.repo.set_draft_status(
                    weakest_id, "superseded",
                    f"outranked by {p.tweet_id} ({s.quote_score:.2f} vs {weakest:.2f})")
                return 1
            break
        return 0

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
        cooldown_days = self.cfg.get("posting.author_cooldown_days", 5)
        near_dup = self.cfg.get("thresholds.near_dup_similarity", 0.82)
        results, failures = [], []
        for draft_id, draft, post in self._ranked_pending():
            if len(results) >= budget:
                break
            if not draft.safety_passed:
                continue
            if self.repo.has_posted(post.tweet_id) or self.repo.has_posted(post.canonical_id):
                self.repo.set_draft_status(draft_id, "duplicate")
                continue
            # Re-check at POST time what drafting checked at DRAFT time — the
            # queue can hold two drafts by one author, or the same echoed idea
            # from two authors, and an earlier window may have posted its twin.
            if author_in_cooldown(post.author_handle, self.repo, cooldown_days):
                self.repo.set_draft_status(draft_id, "skipped", "author_cooldown_at_publish")
                continue
            if is_near_duplicate(post.text, self.repo.posted_source_texts(), near_dup):
                self.repo.set_draft_status(draft_id, "skipped", "near_duplicate_at_publish")
                continue
            # PUBLISH-TIME RE-VET (the 2026-06-10 lesson: a refusal vetted before
            # the QA gate existed published 26h later). The stored verdict is as
            # old as the draft — re-run the deterministic gates + QA, fail-CLOSED,
            # on exactly what's about to go out. A transient QA outage leaves the
            # draft pending for the next window; a real rejection blocks it.
            from .commentary.qa import qa_commentary  # lazy import
            ok, revet_notes = check_commentary(post, draft.commentary, self.cfg,
                                               parts=draft.parts)
            if ok:
                ok, revet_notes = qa_commentary(post, draft.full_text, self.cfg,
                                                fail_open=False)
            if not ok:
                if revet_notes == "qa_unavailable":
                    continue  # transient — draft stays pending, next window retries
                self.repo.set_draft_status(draft_id, "blocked", f"revet:{revet_notes}")
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
        self.repo.log_posted(post.tweet_id, our_id, post.author_handle, post.text,
                             draft.full_text)
        self.repo.set_draft_status(draft_id, "posted")
        self.repo.set_candidate(post.tweet_id, "posted")
        return {"tweet_id": post.tweet_id, "our_id": our_id, "author": post.author_handle}

    # ---------- auto-reply engine ----------
    def reply_scan(self) -> dict:
        """Pick 0-1 fresh on-topic post from a bigger followed account and reply.
        Runs at the end of the collect workflow — zero extra API reads (targets,
        judge verdicts, and follower counts are already in the DB). Caps and the
        kill switch make this the most conservative path in the bot."""
        result = self._reply_scan()
        self.repo.log_run("reply", replied=result.get("count", 0),
                          detail=result["status"])
        return result

    def _reply_scan(self) -> dict:
        cfg = self.cfg
        if not cfg.get("replies.enabled", False):
            return {"status": "disabled", "count": 0}
        if kill_switch_active(cfg):
            return {"status": "killed", "count": 0}
        max_per_day = int(cfg.get("replies.max_per_day", 6))
        if self.repo.count_replies_today() >= max_per_day:
            return {"status": "cap_reached", "count": 0}
        last = self.repo.last_reply_at()
        min_gap = float(cfg.get("replies.min_minutes_between", 45))
        if last is not None:
            from .models import utcnow
            from datetime import timedelta
            if utcnow() - last < timedelta(minutes=min_gap):
                return {"status": "too_soon", "count": 0}

        from .select.reply_targets import select_reply_targets
        window_h = float(cfg.get("replies.max_target_age_minutes", 180)) / 60.0
        posts = self.repo.recent_posts(within_hours=window_h)
        own_handle = self.repo.get_state("own_handle", "")
        targets, _skipped = select_reply_targets(posts, cfg, self.repo, own_handle)
        if not targets:
            return {"status": "no_targets", "count": 0}

        from .commentary.reply import get_reply_generator
        gen = getattr(self, "reply_generator", None) or get_reply_generator(cfg)
        if gen is None:
            return {"status": "no_llm", "count": 0}

        publisher = self.publisher
        if cfg.get("replies.dry_run", True):
            from .publish.dryrun import DryRunPublisher
            publisher = DryRunPublisher(cfg)

        max_per_run = int(cfg.get("replies.max_per_run", 1))
        posted, attempts = [], 0
        for post in targets:
            if len(posted) >= max_per_run or attempts >= max_per_run + 2:
                break
            attempts += 1
            text, model = self._vet_reply(gen, post)
            if text is None:
                continue  # logged as blocked inside _vet_reply — never retried
            try:
                res = publisher.reply(text, post.tweet_id)
            except Exception as e:
                self.repo.log_reply(post.tweet_id, post.author_handle, post.text,
                                    text, model, "failed",
                                    f"{type(e).__name__}: {e}"[:200])
                continue
            status = "posted" if not cfg.get("replies.dry_run", True) else "dry_run"
            self.repo.log_reply(post.tweet_id, post.author_handle, post.text,
                                text, model, status, "", res.get("id", ""))
            posted.append({"target": post.tweet_id, "author": post.author_handle,
                           "our_id": res.get("id", "")})
        status = "replied" if posted else "no_reply"
        return {"status": status, "count": len(posted), "results": posted}

    def _vet_reply(self, gen, post: Post) -> tuple[str | None, str]:
        """check_reply + qa_reply with one revision. A blocked target is logged
        (=> never retried via has_replied) and the scan moves on."""
        from .commentary.qa import qa_reply
        from .commentary.safety import check_reply
        text, model = gen.generate(post)
        notes = ""
        for attempt in (1, 2):
            if text.strip().lower().startswith("skip:"):
                self.repo.log_reply(post.tweet_id, post.author_handle, post.text,
                                    text, model, "blocked",
                                    f"no_material:{text.strip()[5:].strip()[:80]}")
                return None, model
            ok, notes = check_reply(post, text, self.cfg)
            if ok:
                qa_ok, qa_issue = qa_reply(post, text, self.cfg)
                if qa_ok:
                    return text.strip(), model
                notes = qa_issue
            if attempt == 2:
                break
            text, model = gen.revise(post, text, notes)
        self.repo.log_reply(post.tweet_id, post.author_handle, post.text,
                            text, model, "blocked", notes)
        return None, model

    # ---------- account snapshot (follower trend) ----------
    def snapshot(self) -> dict:
        """Once per PT day, record follower/following counts (~$0.001 read) so
        the report can show whether any of this is actually working."""
        if not self.cfg.get("growth.snapshot_enabled", True):
            return {"status": "disabled"}
        from .models import to_local, utcnow
        today = to_local(utcnow(), getattr(self.repo, "tz_name", "UTC")).date().isoformat()
        if self.repo.get_state("last_snapshot_day") == today:
            return {"status": "already_done", "day": today}
        fetch_me = getattr(self.source, "fetch_me", None)
        if fetch_me is None:
            return {"status": "unsupported_source"}
        me = fetch_me()
        self.repo.snapshot_account(today, me.get("followers", 0),
                                   me.get("following", 0), me.get("tweets", 0))
        if me.get("handle"):
            self.repo.set_state("own_handle", me["handle"])
        self.repo.set_state("last_snapshot_day", today)
        return {"status": "ok", "day": today, **me}

    # ---------- report ----------
    def report(self) -> dict:
        posts = self.repo.recent_posts(72)
        return {
            "posts_seen": len(posts),
            "posted_today": self.repo.count_posted_today(),
            "replied_today": self.repo.count_replies_today(),
            "pending_drafts": len(self.repo.pending_drafts()),
            "watching": len(self.repo.candidates("watching")),
            "skipped": len(self.repo.candidates("skipped")),
            "activity": {
                "posted": self.repo.activity_posted(72),
                "replies": self.repo.activity_replies(72),
                "problems": self.repo.activity_drafts(["failed", "blocked", "stale"], 72),
                "days": self.repo.daily_run_totals(7),
                "followers": self.repo.account_history(8),
            },
        }
