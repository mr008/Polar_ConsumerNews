"""Wires the pipeline: collect -> score -> draft -> (review) -> publish.

The collector flow (collect/score/draft) runs often; the publisher flow
(publish_due / approve) runs on the posting schedule. Review gating is controlled
by mode.autonomous.
"""
from __future__ import annotations

import os

from .commentary import check_commentary, get_generator, get_prescreen
from .config import NS, kill_switch_active
from .dedup import author_in_cooldown, is_near_duplicate
from .ingest import SampleSource
from .models import Draft, Post, Score, is_web_source
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
        # source_timeline=list reads a curated List instead of the home feed
        # (cuts reads to chosen authors without changing who you follow).
        list_id = (cfg.get("scoping.list_id", "") or "") \
            if cfg.get("scoping.source_timeline", "home") == "list" else ""
        return ApiSourceAdapter(
            max_posts_per_day=cfg.get("scoping.max_posts_per_day", 120),
            list_id=list_id,
            list_page_size=cfg.get("scoping.list_page_size", 25))
    return SampleSource(cfg.get("ops.fixture_path", "fixtures/sample_posts.json"))


class Orchestrator:
    def __init__(self, cfg: NS, repo: Repository):
        self.cfg = cfg
        self.repo = repo
        self.source = make_source(cfg)
        self.generator = get_generator(cfg)
        self.prescreen = get_prescreen(cfg)  # cheap pre-draft gate; None = disabled
        self.judge = get_teaching_judge(cfg)
        self.publisher = get_publisher(cfg)
        self.judge_reasons: dict[str, str] = {}

    # ---------- collector flow ----------
    def _read_budget_ok(self) -> bool:
        """False (and logs) when the monthly read budget is spent."""
        budget = self.cfg.get("scoping.monthly_read_budget", 0)
        if budget and self.repo.reads_this_month() >= budget:
            used = self.repo.reads_this_month()
            msg = f"circuit_breaker: {used}/{budget} reads this month — skipped"
            print(f"  [collect] {msg}")
            self.repo.log_run("collect", read=0, detail=msg)
            return False
        return True

    def _store(self, posts) -> None:
        for p in posts:
            self.repo.upsert_post(p)
            if not self.repo.has_posted(p.tweet_id):
                self.repo.set_candidate(p.tweet_id, "watching")

    def collect(self) -> int:
        # Circuit breaker: hard monthly read budget (protects against e.g. a
        # silently-ignored since_id running up the bill).
        if not self._read_budget_ok():
            return 0
        cap = self.cfg.get("scoping.max_posts_per_day", 120)
        # PER-SOURCE since_id (state). The List and the discovery sweep both write
        # to the posts table, so a single global max_seen would let one corrupt the
        # other's dedup. Each read source tracks its own watermark instead.
        src_key = "list" if self.cfg.get("scoping.source_timeline", "home") == "list" else "home"
        since = self.repo.get_state(f"since_id:{src_key}", "") or self.repo.max_seen_tweet_id()
        posts = self.source.fetch_timeline(cap, since_id=since)
        self._store(posts)
        if posts:
            newest = str(max(int(p.tweet_id) for p in posts))
            self.repo.set_state(f"since_id:{src_key}", newest)
        self.repo.log_run("collect", read=len(posts))  # n paid API reads this run
        return len(posts)

    def discovery_sweep(self) -> int:
        """Find good accounts you don't yet read, so they can be auto-promoted to
        the List. Two modes (listsync.discovery_mode): `web` (default) searches the
        open web for prominent teachers — free search, only capped vetting reads are
        billed, and it reaches beyond your follows; `home` samples the home feed
        (the legacy fallback, which pays to scroll past spam). Returns kept count."""
        if self.cfg.get("listsync.discovery_mode", "web") == "web":
            return self._web_discovery_sweep()
        return self._home_discovery_sweep()

    def _web_discovery_sweep(self) -> int:
        """Open-web discovery: a web search surfaces prominent teacher accounts;
        NEW handles (deduped vs the List + everything already scored) are vetted by
        reading a few recent posts each — capped by `web_vet_max_reads` — then
        scored so only consistent teachers get promoted. Search is free; the vetting
        reads are the only billed part, and they honour the monthly breaker."""
        src = self.source
        if not hasattr(src, "fetch_user_recent") or not self._read_budget_ok():
            return 0
        api_key = os.environ.get("SEARCH_API_KEY", "")
        if not api_key:
            print("  [discovery] SEARCH_API_KEY unset — web discovery skipped")
            self.repo.log_run("collect", read=0, detail="web discovery: no api key")
            return 0
        from .ingest.web_discovery import search_handles
        cfg = self.cfg
        handles = search_handles(
            cfg.get("listsync.web_queries", []) or [], api_key,
            provider=cfg.get("listsync.web_provider", "brave"),
            per_query=int(cfg.get("listsync.web_results_per_query", 20)))
        fresh = [h for h in handles if h not in self._known_authors()]
        fresh = fresh[:int(cfg.get("listsync.web_max_new", 15))]
        if not fresh:
            self.repo.log_run("collect", read=0,
                              detail=f"web discovery: 0 new of {len(handles)} found")
            return 0
        ids = src.resolve_user_ids(fresh)
        per_author = int(cfg.get("listsync.web_vet_posts_per_author", 5))
        max_reads = int(cfg.get("listsync.web_vet_max_reads", 100))
        posts, read = [], 0
        for h in fresh:
            uid = ids.get(h)
            if not uid or read >= max_reads:
                continue
            got, n = src.fetch_user_recent(uid, max_posts=per_author)
            posts.extend(got)
            read += n
        self._store(posts)
        self.repo.log_run("collect", read=read,
                          detail=f"web discovery: {len(fresh)} candidates, {len(posts)} posts")
        if posts:
            self.score()  # judge the new posts so they enter author_yield
        return len(posts)

    def _known_authors(self) -> set[str]:
        """Handles we already have signal on — skip re-vetting (and paying for) them:
        the bot's own account, every author already scored, and current List members."""
        known = {r["handle"].lower() for r in self.repo.author_yield() if r["handle"]}
        known.add((self.repo.get_state("own_handle", "") or "").lstrip("@").lower())
        src = self.source
        list_id = (self.cfg.get("scoping.list_id", "") or "").strip()
        if list_id and hasattr(src, "list_members"):
            try:
                known.update(m["handle"].lower() for m in src.list_members(list_id)
                             if m.get("handle"))
            except Exception as e:
                print(f"  [discovery] list_members lookup failed: {type(e).__name__}")
        known.discard("")
        return known

    def _home_discovery_sweep(self) -> int:
        """Author-fair HOME-feed sample (even while the bot reads a List) so good
        accounts you don't yet read can be auto-promoted. The window is N DISTINCT
        accounts (config), not a raw post count, so a flooder can't dominate. Its
        own since_id keeps it cheap; honours the read budget. Returns kept count."""
        fetch = getattr(self.source, "fetch_discovery", None)
        if fetch is None or not self._read_budget_ok():
            return 0
        cfg = self.cfg
        since = self.repo.get_state("since_id:discovery", "") or None
        posts, n_read = fetch(
            target_authors=int(cfg.get("listsync.discovery_authors", 50)),
            per_author_cap=int(cfg.get("listsync.discovery_per_author", 2)),
            max_reads=int(cfg.get("listsync.discovery_max_reads", 250)),
            since_id=since)
        self._store(posts)
        if posts:
            self.repo.set_state("since_id:discovery",
                                str(max(int(p.tweet_id) for p in posts)))
        self.repo.log_run("collect", read=n_read, detail="discovery sweep")  # billed reads
        if posts:
            self.score()  # judge the new posts so they enter author_yield
        return len(posts)

    def collect_web(self) -> int:
        """ONLINE CONTENT source: web search finds tactical blog posts, a cheap
        model summarizes each into a teaching brief, and the briefs enter the same
        candidate pool as X posts (published as ORIGINAL teaching posts, no quote).
        Runs on its own light cadence. Free search; only NEW articles cost a small
        summarize call. No X reads. Returns the number of new candidates stored."""
        cfg = self.cfg
        if not cfg.get("webcontent.enabled", False):
            return 0
        key = os.environ.get("SEARCH_API_KEY", "")
        if not key:
            print("  [web-content] SEARCH_API_KEY unset — skipped")
            return 0
        from .ingest.web_articles import (find_article_candidates, summarize_article,
                                          to_web_post, web_id)
        cands = find_article_candidates(
            cfg.get("webcontent.queries", []) or [], key,
            provider=cfg.get("webcontent.provider", "brave"),
            per_query=int(cfg.get("webcontent.results_per_query", 15)))
        cap = int(cfg.get("webcontent.max_new_per_run", 3))
        min_chars = int(cfg.get("webcontent.min_article_chars", 400))
        posts, added = [], 0
        for url, title, blurb in cands:
            if added >= cap:
                break
            wid = web_id(url)
            if self.repo.has_posted(wid) or self.repo.get_post(wid) is not None:
                continue                       # already posted or already a candidate
            brief = summarize_article(title, blurb, url, cfg, min_chars=min_chars)
            if not brief:                      # SKIP / thin / no key
                continue
            posts.append(to_web_post(url, brief))
            added += 1
        self._store(posts)
        self.repo.log_run("collect", read=0, detail=f"web content: +{added} of "
                          f"{len(cands)} candidates")
        if posts:
            self.score()                       # judge them so they enter the ranked pool
        return added

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
        # Web-article candidates have no real tweet id — never send them to X.
        ids = [p.tweet_id for _, _, p in self.repo.pending_drafts()
               if not p.tweet_id.startswith("web:")]
        def stored_score(p: Post) -> float:
            s = self.repo.get_score(p.tweet_id)
            return s.quote_score if s else 0.0
        for p in sorted(posts, key=stored_score, reverse=True):
            if len(ids) >= top_n:
                break
            if p.tweet_id not in ids and not is_web_source(p):
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

            # PRE-DRAFT GATE: a cheap model rejects no-material posts (truncated
            # RTs, teasers, flexes) before the expensive commentary call. ~90% of
            # eligible posts would SKIP anyway; this moves that to a ~24x cheaper
            # model. Fail-open (None or YES drafts). A reject is logged as a
            # blocked draft so it's never re-screened (one-attempt-per-post EVER).
            prescreen = getattr(self, "prescreen", None)
            if prescreen is not None and not prescreen.has_material(post):
                draft = Draft(tweet_id=post.tweet_id, commentary="",
                              model="prescreen", safety_passed=False,
                              safety_notes="no_material:prescreen")
                draft_id = self.repo.add_draft(draft, status="blocked")
                self.repo.set_candidate(post.tweet_id, "skipped", "no_material:prescreen")
                created.append({"draft_id": draft_id, "draft": draft, "post": post,
                                "score": score, "ok": False,
                                "notes": "no_material:prescreen"})
                continue

            # Adaptive threads: only substantial sources earn the multi-part
            # treatment; the generator still falls back to a single post if it
            # can't extract 3+ concrete steps.
            allow_thread = (bool(self.cfg.get("posting.adaptive_threads", False))
                            and not is_web_source(post)  # web posts publish as one post
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
        # Feature tag for the outcome harvester (AUTONOMY.md). Never allowed to
        # break publishing — the post is already live at this point.
        try:
            self._log_features(draft, post, our_id)
        except Exception as e:
            print(f"  [features] tagging failed ({type(e).__name__}) — post unaffected")
        return {"tweet_id": post.tweet_id, "our_id": our_id, "author": post.author_handle}

    def _log_features(self, draft: Draft, post: Post, our_id: str) -> None:
        """Record WHAT KIND of post just went out, so harvested outcomes can be
        attributed to editorial choices (format, hook, slot, source) later."""
        if not our_id:
            return  # dry-run / review modes have nothing to harvest against
        from .models import to_local, utcnow
        score = self.repo.get_score(post.tweet_id)
        text = draft.full_text
        self.repo.log_features({
            "our_tweet_id": our_id,
            "source_tweet_id": post.tweet_id,
            "author_handle": post.author_handle,
            # route: which editorial engine produced the draft. "pipeline" =
            # judge+commentary stages; "curator" arrives with mode.editorial.
            "route": "pipeline",
            "kind": "web" if is_web_source(post) else "qt",
            "format": "thread" if draft.parts else "single",
            "parts_n": len(draft.parts or []),
            "chars": len(text),
            "has_question": "?" in text,
            "hook": (draft.commentary.splitlines() or [""])[0],
            "window_hour": to_local(utcnow(), getattr(self.repo, "tz_name", "UTC")).hour,
            "teaching": score.quote_worthy if score else None,
            "topic_fit": score.topic_fit if score else None,
            "quote_score": score.quote_score if score else None,
            "posted_at": utcnow().isoformat(),
        })

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
                # A conversation-level reply restriction isn't a failure on our
                # side — X just won't let this account reply there. Log it as
                # "skipped" (keeps the failure count honest) and move on; the row
                # still makes has_replied() true so the target is never retried.
                emsg = str(e)
                restricted = ("reply_not_allowed" in emsg
                              or "not allowed" in emsg.lower())
                self.repo.log_reply(
                    post.tweet_id, post.author_handle, post.text, text, model,
                    "skipped" if restricted else "failed",
                    ("reply_restricted: conversation blocks replies from this account"
                     if restricted else f"{type(e).__name__}: {e}")[:200])
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

    # ---------- human-in-the-loop reply queue ----------
    def reply_queue_targets(self, fresh: bool = False, limit: int = 15) -> list[Post]:
        """Eligible reply targets for the MANUAL session (`xbot reply-queue`),
        ranked big-and-fresh-first. Pure selection — no LLM, no posting; drafting
        is deferred to the session so tokens are only spent on what the owner
        reviews. Programmatic replies are dead (X Feb-2026 policy) so this feeds a
        human who posts manually; unknown reply_settings are allowed through."""
        from .select.reply_targets import select_reply_targets
        if fresh:
            self.collect()
        window_h = float(self.cfg.get("replies.max_target_age_minutes", 180)) / 60.0
        posts = self.repo.recent_posts(within_hours=window_h)
        own_handle = self.repo.get_state("own_handle", "")
        targets, _ = select_reply_targets(posts, self.cfg, self.repo, own_handle,
                                          require_open_replies=False)
        return targets[: max(1, limit)]

    def draft_reply(self, post: Post, gen=None) -> tuple[str | None, str]:
        """Draft + vet (safety + QA) one reply. Returns (text, model), or
        (None, model) when the draft is a SKIP or fails the gates — in which case
        it is logged blocked so the target never reappears. Reuses the autonomous
        engine's vetting (`_vet_reply`)."""
        from .commentary.reply import get_reply_generator
        gen = gen or getattr(self, "reply_generator", None) or get_reply_generator(self.cfg)
        if gen is None:
            return None, "no_llm"
        return self._vet_reply(gen, post)

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

    # ---------- outcome harvester (the autonomy learning substrate) ----------
    def harvest(self) -> dict:
        """Capture engagement snapshots of OUR OWN recent posts at fixed
        milestones (outcomes.py). Owned reads ~$0.001 each; a post is captured
        at most once per milestone. Runs at the end of every collect pass.

        Deliberately logged with read=0: the monthly circuit breaker guards the
        $0.005 timeline reads, and letting ~$0.001 owned reads consume that
        budget would trade content supply for telemetry. The owned-read count
        is carried in the run detail instead, and the per-run ceiling plus the
        milestone windows bound the spend structurally (~15 reads/day)."""
        from .models import parse_dt, utcnow
        from .outcomes import (HARVEST_MAX_AGE_DAYS, HARVEST_MAX_READS_PER_RUN,
                               due_milestone)
        fetch = getattr(self.source, "fetch_metrics", None)
        if fetch is None:
            return {"status": "unsupported_source", "count": 0}
        due: dict[str, str] = {}
        for r in self.repo.posted_recent(within_days=HARVEST_MAX_AGE_DAYS):
            age_h = (utcnow() - parse_dt(r["posted_at"])).total_seconds() / 3600.0
            m = due_milestone(age_h, self.repo.outcome_milestones(r["our_tweet_id"]))
            if m:
                due[r["our_tweet_id"]] = m
        ids = list(due)[:HARVEST_MAX_READS_PER_RUN]
        if not ids:
            return {"status": "nothing_due", "count": 0}
        try:
            fresh = fetch(ids)
        except Exception as e:
            # Fail quiet: a missed snapshot self-heals at the next open window.
            print(f"  [harvest] fetch failed ({type(e).__name__}) — will retry next run")
            self.repo.log_run("harvest", detail=f"fetch_failed:{type(e).__name__}")
            return {"status": "fetch_failed", "count": 0}
        for tid, m in fresh.items():
            if tid in due:
                self.repo.log_outcome(tid, due[tid], m)
        self.repo.log_run(
            "harvest", detail=f"owned_reads:{len(ids)} captured:{len(fresh)}")
        return {"status": "ok", "count": len(fresh),
                "milestones": {t: due[t] for t in fresh if t in due}}

    # ---------- curated read-List: auto-update ----------
    def _qualifies(self, r: dict) -> bool:
        """A consistent teacher: enough judged posts, AVERAGE teaching above the
        bar, and not a high-volume-low-quality flooder (the @athcanft/@tailopez
        pattern — posts a flood, occasionally lands one)."""
        cfg = self.cfg
        if r["judged"] < int(cfg.get("listsync.min_judged", 3)):
            return False
        if (r["judged"] > int(cfg.get("listsync.flood_judged", 60))
                and r["avg_qw"] < float(cfg.get("listsync.flood_avg", 0.45))):
            return False
        return r["avg_qw"] >= float(cfg.get("listsync.promote_avg", 0.35))

    def sync_keep_list(self, apply: bool = False) -> dict:
        """Auto-update the curated read-List. PROMOTE accounts that teach
        consistently (avg score over enough recent judged posts); DEMOTE current
        members that drift below the floor or go quiet. Members with thin recent
        data get a grace pass. apply=False returns the diff with no API writes;
        True creates the List if needed, applies adds/removes, and logs the change.
        Your personal follows are never touched."""
        from datetime import timedelta
        from .models import utcnow
        cfg = self.cfg
        rows = self.repo.author_yield(within_days=int(cfg.get("listsync.recency_days", 30)))
        own = (self.repo.get_state("own_handle", "") or "").lstrip("@").lower()
        y = {r["handle"].lower(): r for r in rows
             if r["handle"] and r["handle"].lower() != own}
        qualified = {h for h, r in y.items() if self._qualifies(r)}

        src = self.source
        if not hasattr(src, "list_members"):
            return {"status": "unsupported_source", "qualified": sorted(qualified)}
        list_id = (cfg.get("scoping.list_id", "") or "").strip()
        created = False
        if not list_id:
            if not apply:
                return {"status": "no_list", "promote": sorted(qualified), "demote": []}
            list_id = src.create_list(cfg.get("scoping.list_name", "xbot feed"), private=True)
            created = True
        members = [] if created else src.list_members(list_id)
        member_handles = {m["handle"].lower(): m for m in members if m.get("handle")}

        promote = sorted(h for h in qualified if h not in member_handles)
        demote_avg = float(cfg.get("listsync.demote_avg", 0.25))
        min_judged = int(cfg.get("listsync.min_judged", 3))
        quiet_cut = (utcnow() - timedelta(days=int(cfg.get("listsync.quiet_days", 21)))).isoformat()
        # last activity is ALL-TIME (a member silent past the recency window has no
        # row in the windowed yield, so we'd never see it to demote otherwise).
        last_seen = {r["handle"].lower(): r["last_post"]
                     for r in self.repo.author_yield() if r["handle"]}
        demote = []
        for h in member_handles:
            r = y.get(h)
            lp = last_seen.get(h, "")
            if r is None and not lp:
                continue  # never seen at all — grace pass (discovery may be lagging)
            drifted = bool(r) and (r["judged"] >= min_judged
                                   and r["avg_qw"] < demote_avg and r["posted"] == 0)
            quiet = bool(lp) and lp < quiet_cut
            if drifted or quiet:
                demote.append(h)
        demote = sorted(demote)

        diff = {"list_id": list_id, "created": created,
                "members_n": len(member_handles), "promote": promote, "demote": demote}
        if not apply:
            return {**diff, "status": "dry_run"}

        ids = src.resolve_user_ids(promote) if promote else {}
        added, removed, failed = [], [], []
        for h in promote:
            uid = ids.get(h)
            if not uid:
                failed.append(h); continue
            try:
                src.add_list_member(list_id, uid); added.append(h)
            except Exception as e:
                print(f"  [list-sync] add @{h} failed: {type(e).__name__}"); failed.append(h)
        for h in demote:
            try:
                src.remove_list_member(list_id, member_handles[h]["id"]); removed.append(h)
            except Exception as e:
                print(f"  [list-sync] remove @{h} failed: {type(e).__name__}")
        summary = (f"+{len(added)} -{len(removed)}"
                   + (f" · added {', '.join('@' + a for a in added)}" if added else "")
                   + (f" · dropped {', '.join('@' + r for r in removed)}" if removed else ""))
        self.repo.set_state("last_list_sync", summary)
        self.repo.log_run("list-sync", detail=summary[:200])
        return {**diff, "status": "ok", "added": added, "removed": removed,
                "failed": failed, "summary": summary}

    def auto_list_update(self, discover: bool = False) -> dict:
        """The list-sync job: apply the promote/demote diff (free, from data already
        read). Account DISCOVERY (billed web-search + vetting) runs ONLY when
        discover=True — kept manual (`xbot list-sync --discover`) so the weekly cron
        does cheap List hygiene without auto-spending on new-account vetting."""
        swept = self.discovery_sweep() if discover else 0
        result = self.sync_keep_list(apply=True)
        result["discovery_posts"] = swept
        result["discovered"] = discover
        return result

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
            "list_sync": self.repo.get_state("last_list_sync", ""),
            "activity": {
                "posted": self.repo.activity_posted(72),
                "replies": self.repo.activity_replies(72),
                "problems": self.repo.activity_drafts(["failed", "blocked", "stale"], 72),
                "days": self.repo.daily_run_totals(7),
                "followers": self.repo.account_history(8),
            },
        }
