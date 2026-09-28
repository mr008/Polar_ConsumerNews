"""The shipped config must calibrate the judge in BOTH directions: without BAD
examples it rewards generic founder advice and dev-tool chatter (the 2026-09
engagement review found those were the account's worst performers)."""
from pathlib import Path

from xbot.config import load_config
from xbot.score.teaching_judge import _build_system

ROOT = Path(__file__).resolve().parents[1]


def test_shipped_judge_examples_cover_good_and_bad():
    cfg = load_config(ROOT / "config.yaml")
    verdicts = {str(ex["verdict"]).lower()
                for ex in cfg.get("ranking.judge_examples", [])}
    assert {"good", "bad"} <= verdicts


def test_judge_prompt_carries_calibration_and_focus():
    system = _build_system(load_config(ROOT / "config.yaml"))
    assert "[BAD]" in system and "[GOOD]" in system
    # generic founder/productivity advice must be named as off-focus
    assert "founder" in system.lower() and "0.0-0.3" in system
