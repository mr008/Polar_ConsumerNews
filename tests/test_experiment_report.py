"""Grading (spec §5): per-arm stats, the winner rule, period comparison."""
from datetime import date, timedelta

from xbot.config import NS
from xbot.experiments import render, summarize, verdict
from xbot.models import Metrics
from xbot.storage.sqlite_repo import SqliteRepository


def test_verdict_rule():
    c = {"n": 10, "median_views": 10.0}
    assert verdict(c, {"n": 10, "median_views": 13.0}, 5) == "winner: b"
    assert verdict(c, {"n": 10, "median_views": 12.0}, 5) == "no difference"
    assert verdict(c, {"n": 10, "median_views": 6.0}, 5) == "winner: control"
    assert verdict(c, {"n": 3, "median_views": 30.0}, 5) == "continue"
    assert verdict(c, {"n": 3, "median_views": 30.0}, 21) == "winner: b"
    assert verdict({"n": 0, "median_views": 0.0}, {"n": 0, "median_views": 0.0}, 30) == "no data"


def _rows():
    base = date(2026, 10, 9)
    out = []
    for i in range(8):
        arm = "card" if i % 2 else "text"
        out.append({"our_tweet_id": f"o{i}", "posted_at": f"{base + timedelta(days=i)}T18:00:00",
                    "arms": {"image_card": arm}, "views": 20 if arm == "card" else 10,
                    "likes": 1, "reposts": 0, "replies": 0, "quotes": 0})
    # before the experiment started: no arms, lower views
    for i in range(1, 8):
        out.append({"our_tweet_id": f"p{i}", "posted_at": f"{base - timedelta(days=i)}T18:00:00",
                    "arms": {}, "views": 5, "likes": 0, "reposts": 0, "replies": 0, "quotes": 0})
    return out


CFG = NS({"experiments": {
    "image_card": {"enabled": True, "arms": ["text", "card"], "started": "2026-10-09"},
    "author_bait": {"enabled": False, "arms": ["tail", "question"], "started": "2026-10-09"},
    "volume": {"enabled": True, "period": True, "started": "2026-10-09"}}})


def test_summarize_per_post_and_period():
    rep = summarize(_rows(), CFG, today=date(2026, 10, 17))
    img = next(r for r in rep if r["name"] == "image_card")
    assert img["arms"]["text"]["n"] == 4 and img["arms"]["card"]["n"] == 4
    assert img["arms"]["card"]["median_views"] == 20
    assert img["verdict"] == "continue"            # n < 10 and < 21 days
    assert img["days"] == 8
    vol = next(r for r in rep if r["name"] == "volume")
    assert vol["period"] is True
    assert vol["on"]["posts_per_day"] == 1.0 and vol["before"]["posts_per_day"] == 0.5
    assert vol["on"]["median_views"] == 15 and vol["before"]["median_views"] == 5
    assert "author_bait" not in [r["name"] for r in rep]
    text = render(rep)
    assert "image_card" in text and "card" in text and "volume" in text


def test_summarize_edge_cases():
    # nothing enabled / config missing -> empty report
    assert summarize(_rows(), NS({}), today=date(2026, 10, 17)) == []
    assert render([]) == "(no experiments enabled)"
    # an arm with zero posts, and posts with no 24h row yet (views None)
    rows = [{"our_tweet_id": "a", "posted_at": "2026-10-10T18:00:00",
             "arms": {"image_card": "text"}, "views": None, "likes": None,
             "reposts": None, "replies": None, "quotes": None}]
    rep = summarize(rows, CFG, today=date(2026, 10, 17))
    img = next(r for r in rep if r["name"] == "image_card")
    assert img["arms"]["text"]["posted"] == 1 and img["arms"]["text"]["n"] == 0
    assert img["arms"]["card"]["posted"] == 0 and img["arms"]["card"]["n"] == 0
    assert img["verdict"] == "no data"
    assert "card" in render(rep)


def test_repo_experiment_rows_joins_features_and_24h_outcomes():
    repo = SqliteRepository(":memory:"); repo.init_schema()
    repo.log_posted("src1", "our_1", "alice", "src text", "our text")
    repo.log_features({"our_tweet_id": "our_1", "arms": {"image_card": "card"}})
    repo.log_outcome("our_1", "1h", Metrics(views=3))
    repo.log_outcome("our_1", "24h", Metrics(views=9, likes=1))
    repo.log_posted("src2", "our_2", "bob", "src text 2", "our text 2")
    rows = repo.experiment_rows(within_days=35)
    by = {r["our_tweet_id"]: r for r in rows}
    assert by["our_1"]["arms"] == {"image_card": "card"} and by["our_1"]["views"] == 9
    assert by["our_2"]["arms"] == {} and by["our_2"]["views"] is None


def test_briefing_has_arms_tag_and_experiments_section():
    from xbot.briefing import build_briefing
    repo = SqliteRepository(":memory:"); repo.init_schema()
    repo.log_posted("src1", "our_1", "alice", "src text", "our text")
    repo.log_features({"our_tweet_id": "our_1", "arms": {"image_card": "card"}})
    repo.log_outcome("our_1", "24h", Metrics(views=9))
    today = date.today().isoformat()
    cfg = NS({"experiments": {"image_card": {"enabled": True, "arms": ["text", "card"],
                                             "started": today}}})
    text = build_briefing(repo, cfg)
    assert '"image_card": "card"' in text
    assert "## Experiments" in text and "### image_card" in text
    # no experiments block at all -> section present, report empty
    assert "(no experiments enabled)" in build_briefing(repo, NS({}))


def test_cmd_experiments_prints_report_and_json(monkeypatch, capsys):
    import json
    from types import SimpleNamespace

    from xbot import cli
    repo = SqliteRepository(":memory:"); repo.init_schema()
    cfg = NS({"experiments": {"image_card": {"enabled": True, "arms": ["text", "card"],
                                             "started": "2026-10-09"}}})
    monkeypatch.setattr(cli, "_setup_light", lambda a: SimpleNamespace(repo=repo, cfg=cfg))
    assert cli.cmd_experiments(SimpleNamespace(json=False)) == 0
    assert "### image_card" in capsys.readouterr().out
    assert cli.cmd_experiments(SimpleNamespace(json=True)) == 0
    assert json.loads(capsys.readouterr().out)[0]["name"] == "image_card"
