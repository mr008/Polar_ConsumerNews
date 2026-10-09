"""Growth experiments: config-driven A/B arms per post + period switches.

Spec: docs/superpowers/specs/2026-10-08-growth-experiments-design.md

    experiments:
      image_card:  {enabled: true, arms: [text, card],     started: 2026-10-09}
      author_bait: {enabled: true, arms: [tail, question], started: 2026-10-09}
      volume:      {enabled: true, period: true,           started: 2026-10-09}

Per-post tests assign an arm at DRAFT time by strict alternation (post 1 ->
arms[0], post 2 -> arms[1], ...) counted over live drafts, so at ~1 post/day
the arms stay balanced (random assignment would not). The first arm of each
test is the control (today's behavior). A disabled or missing test assigns
nothing and every consumer falls back to the control via arm().
"""
from __future__ import annotations

from .models import Post, is_web_source

PER_POST = ("image_card", "author_bait")
CONTROL = {"image_card": "text", "author_bait": "tail"}


def enabled(cfg, name: str) -> bool:
    return bool(cfg.get(f"experiments.{name}.enabled", False))


def arms_of(cfg, name: str) -> list[str]:
    arms = cfg.get(f"experiments.{name}.arms", None)
    return [str(a) for a in arms] if arms else [CONTROL[name]]


def started(cfg, name: str) -> str:
    return str(cfg.get(f"experiments.{name}.started", "") or "")


def arm(arms: dict | None, name: str) -> str:
    """The arm a draft/post is in for one test; control when unassigned."""
    return str((arms or {}).get(name) or CONTROL[name])


def assign_arms(cfg, repo, post: Post) -> dict[str, str]:
    """Arms for a new draft of `post`. Web sources (no author) are always the
    author_bait control and don't consume an alternation slot."""
    out: dict[str, str] = {}
    for name in PER_POST:
        if not enabled(cfg, name):
            continue
        arms = arms_of(cfg, name)
        if name == "author_bait" and is_web_source(post):
            out[name] = arms[0]
            continue
        n = sum(repo.arm_counts(name).values())
        out[name] = arms[n % len(arms)]
    return out


def volume_on(cfg) -> bool:
    return enabled(cfg, "volume")


# ---- volume period test (spec §4): three levers, graded as one ----

def threshold_delta(cfg) -> float:
    """Subtracted from thresholds.topic_fit_min / quote_worthy_min while on."""
    return float(cfg.get("experiments.volume.threshold_delta", 0.05)) if volume_on(cfg) else 0.0


def per_run(cfg) -> int:
    """Drafts a publish window may post: posting.per_run, or the volume override."""
    if volume_on(cfg):
        return int(cfg.get("experiments.volume.per_run", 2))
    return int(cfg.get("posting.per_run", 1))


def max_vet_attempts(cfg) -> int:
    """Generate + revisions the vet loop may spend on one draft (2 today)."""
    return 3 if volume_on(cfg) else 2
