"""Semantic judge — the SOLE authority on topic relevance and teaching value.

There is NO lexical fallback. If the LLM judge can't run, the bot produces no
candidates that round (it posts nothing rather than guess from keywords). An LLM
provider key is required.

- Cheap structural pre-filter (`prefilter_for_judge`) drops replies/link-only/
  unsafe posts — but NOT on topic keywords (the judge decides topic by meaning).
- ONE batched LLM call returns {on_topic, teaching score, reason} per post.
- Short-video-reliant posts are explicitly discouraged.
"""
from __future__ import annotations

import json
import os
import re
from typing import Protocol

from ..commentary.generate import AUTO_ORDER, PROVIDERS
from ..commentary.safety import classify_source
from ..config import NS
from ..models import Post

JUDGE_SYSTEM = """You are a strict editorial judge for an account that teaches people how to make viral CONSUMER-APP content (AI UGC, product, growth, marketing, distribution, indie startups, builder tools).

For each post return TWO things:

1) topic_fit (0.0-1.0): how squarely is it about BUILDING or GROWING consumer apps / software products?
- 0.8-1.0: squarely on-topic — AI & AI-UGC, app growth, marketing, distribution, content tactics, indie hacking, startups, app revenue/MRR, AI/dev tools for builders.
- 0.4-0.7: adjacent but useful — general marketing/creator-economy/audience-building tactics a consumer-app builder could apply, general AI tooling.
- 0.0-0.3: unrelated — sports, fitness/bodybuilding, politics, personal life, crypto trading, random chatter.
Judge by MEANING, not keywords — "build a voice agent", "$100k MRR", "AI influencer", "Codex client", "RevenueCat" are all squarely on-topic even with no obvious keyword.

2) score (0.0-1.0): TEACHING VALUE — how much someone building a consumer app would actually LEARN a real, applicable practice. Popularity/engagement is NOT teaching value.
- 0.7-1.0: specific, actionable method; concrete steps; non-obvious insight; a real tactic.
- 0.4-0.6: a real point, but generic or lightly developed.
- 0.0-0.3: vague inspiration, hype/flex with no method, or off-topic.

Credit a REAL underlying tactic even when the framing is casual or "save this"-style (e.g. "reusing one AI image got millions of views" is a genuine distribution insight). Score low only when there's no real tactic underneath.

DISCOURAGE short-video posts: if a post is marked [attached video/media] and its point depends on that video (the text alone doesn't teach the practice), score it LOW (<=0.3) — we share TEXT and cannot extract a point locked inside a short video.

PENALIZE incomplete text: if the post text appears truncated or cut off (e.g. a retweet stub ending in "…"), or it teases a story/payoff that is not actually in the text, score it LOW (<=0.3) — we cannot teach from a cliffhanger.

Return ONLY JSON, no prose:
{"scores":[{"id":<int>,"topic_fit":<float 0.0-1.0>,"score":<float 0.0-1.0>,"reason":"<=8 words"}]}"""


class TeachingJudge(Protocol):
    def score_batch(self, posts: list[Post]) -> dict[str, tuple[float, float, str]]:
        """Return {tweet_id: (teaching_value 0..1, topic_fit 0..1, short_reason)}.
        topic_fit is GRADED (legacy bool verdicts coerce to 1.0/0.0)."""
        ...


def prefilter_for_judge(posts: list[Post], cfg: NS, limit: int = 30) -> list[Post]:
    """Structural gates only (NO topic keywords — the judge decides topic)."""
    cands = []
    for p in posts:
        if p.is_reply:
            continue
        if p.has_link and len(p.text) < 60:
            continue
        if len(p.text.split()) < 4:
            continue
        ok, _ = classify_source(p, cfg)
        if not ok:
            continue
        cands.append(p)
    cands.sort(key=lambda p: p.metrics.total_engagement, reverse=True)
    return cands[:limit]


def _build_system(cfg: NS) -> str:
    examples = cfg.get("ranking.judge_examples", []) or []
    if not examples:
        return JUDGE_SYSTEM
    lines = ["\n\nCALIBRATION — the account owner's own judgments. Match this taste:"]
    for ex in examples:
        verdict = str(ex.get("verdict", "")).upper()
        why = ex.get("why", "")
        text = str(ex.get("text", ""))[:200]
        lines.append(f'- [{verdict}] "{text}" -> {why}')
    return JUDGE_SYSTEM + "\n".join(lines)


class LLMTeachingJudge:
    def __init__(self, cfg: NS, provider: str):
        self.cfg = cfg
        self.provider = provider
        self.model = cfg.get("ranking.judge_model",
                             cfg.get("llm.commentary_model", "llama-3.3-70b-versatile"))
        self.system = _build_system(cfg)

    def score_batch(self, posts: list[Post]) -> dict[str, tuple[float, float, str]]:
        if not posts:
            return {}
        try:
            parsed = _parse(self._call(posts))
        except Exception as e:
            # NO lexical fallback — produce no candidates this round (safe default).
            print(f"  [judge] LLM judge unavailable ({type(e).__name__}); no fallback "
                  f"— producing 0 candidates this run.")
            return {}
        out: dict[str, tuple[float, float, str]] = {}
        for i, p in enumerate(posts, 1):
            # a post the judge omitted is treated as not-on-topic / no teaching value
            out[p.tweet_id] = parsed.get(i, (0.0, 0.0, "not judged"))
        return out

    def _call(self, posts: list[Post]) -> str:
        from openai import OpenAI
        kwargs = {"api_key": os.environ[PROVIDERS[self.provider]["key_env"]]}
        if PROVIDERS[self.provider]["base_url"]:
            kwargs["base_url"] = PROVIDERS[self.provider]["base_url"]
        client = OpenAI(**kwargs)
        listing = "\n".join(
            f"{i}: {p.text[:500]}" + (" [attached video/media]" if p.has_media else "")
            for i, p in enumerate(posts, 1))
        resp = client.chat.completions.create(
            model=self.model, temperature=0, max_tokens=4096,
            messages=[{"role": "system", "content": self.system},
                      {"role": "user", "content": f"Posts to score:\n{listing}"}],
        )
        return resp.choices[0].message.content


def _parse(content: str) -> dict[int, tuple[float, float, str]]:
    out: dict[int, tuple[float, float, str]] = {}

    def add(it):
        try:
            idx = int(it["id"])
            score = max(0.0, min(1.0, float(it["score"])))
            # Graded topic_fit; legacy on_topic bool coerces to 1.0/0.0 so stored
            # prompts/older models keep working.
            if "topic_fit" in it:
                topic = max(0.0, min(1.0, float(it["topic_fit"])))
            else:
                topic = 1.0 if bool(it.get("on_topic", True)) else 0.0
            out[idx] = (score, topic, str(it.get("reason", ""))[:60])
        except (KeyError, ValueError, TypeError):
            pass

    # Try the whole JSON object first.
    m = re.search(r"\{.*\}", content, re.S)
    if m:
        try:
            data = json.loads(m.group(0))
            items = data.get("scores", []) if isinstance(data, dict) else data
            for it in items:
                add(it)
            if out:
                return out
        except json.JSONDecodeError:
            pass

    # Salvage: parse each individual {...} entry (survives truncation / preamble).
    for mm in re.finditer(r"\{[^{}]*\}", content):
        try:
            add(json.loads(mm.group(0)))
        except json.JSONDecodeError:
            continue
    return out


def get_teaching_judge(cfg: NS) -> TeachingJudge:
    """Require an LLM provider — there is no lexical judge anymore."""
    provider = cfg.get("llm.provider", "auto")
    order = AUTO_ORDER if provider == "auto" else [provider]
    for prov in order:
        if prov in PROVIDERS and os.environ.get(PROVIDERS[prov]["key_env"]):
            return LLMTeachingJudge(cfg, prov)
    raise SystemExit(
        "No LLM provider key found. The semantic judge is required (no lexical "
        "fallback). Set one of GROQ_API_KEY / XAI_API_KEY / GEMINI_API_KEY / "
        "OPENAI_API_KEY in .env and llm.provider in config.yaml."
    )
