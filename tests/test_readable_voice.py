"""The plain-language voice (spec §6): prompt rules + offline template output."""
from xbot.commentary import generate as g
from xbot.commentary.safety import check_readability
from xbot.config import NS
from xbot.models import Post, utcnow

CFG = NS({"voice": {"style": "growth_first", "credit_style": "subtle_tail"},
          "llm": {"max_commentary_chars": 262}})


def test_system_prompt_carries_the_readable_rules():
    p = g.build_system_prompt(CFG)
    for needle in ("ONE point", "any founder or creator", "No bullets",
                   "262", "h/t @"):
        assert needle in p, needle
    assert "steal this" not in p


def test_template_generator_obeys_readability_rules():
    post = Post(tweet_id="1", author_handle="alice", author_name="A",
                text="1. post daily\n2. reuse the winner\n3. track views",
                created_at=utcnow())
    draft = g.TemplateCommentaryGenerator(CFG).generate(post)
    assert check_readability(draft.commentary) == ""
    assert draft.commentary.endswith("h/t @alice")
    assert len(draft.commentary) <= 262 + len("\n\nh/t @alice")
