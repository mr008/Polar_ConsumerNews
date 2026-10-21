from xbot.commentary.generate import _commentary_max_tokens, _omit_temperature


def test_always_thinking_models_get_headroom():
    # Fable's thinking spends from max_tokens; 320 could leave no room for the post.
    assert _commentary_max_tokens("claude-fable-5-1", allow_thread=False) == 8000
    assert _commentary_max_tokens("claude-fable-5-1", allow_thread=True) == 8000
    assert _omit_temperature("claude-fable-5-1")


def test_other_models_keep_short_caps():
    assert _commentary_max_tokens("claude-sonnet-5", allow_thread=False) == 320
    assert _commentary_max_tokens("claude-sonnet-5", allow_thread=True) == 900
