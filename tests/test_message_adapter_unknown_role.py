"""Regression coverage for conservative transcript speaker attribution."""

from types import SimpleNamespace

from src.message_adapter import MessageAdapter


def test_unknown_replay_role_is_not_misattributed_as_user_in_prompt():
    items = [
        SimpleNamespace(role="user", content="first"),
        SimpleNamespace(role="tool", content="tool output"),
        SimpleNamespace(role="assistant", content="last"),
    ]

    assert MessageAdapter.response_input_to_prompt(items) == (
        "User: first\n\ntool output\n\nAssistant: last"
    )


def test_unknown_replay_role_is_not_misattributed_as_user_in_blocks():
    items = [
        SimpleNamespace(role="user", content="first"),
        SimpleNamespace(role="tool", content="tool output"),
        SimpleNamespace(role="assistant", content="last"),
    ]

    assert MessageAdapter.response_input_to_claude_blocks(items) == [
        {"type": "text", "text": "User:"},
        {"type": "text", "text": "first"},
        {"type": "text", "text": "tool output"},
        {"type": "text", "text": "Assistant:"},
        {"type": "text", "text": "last"},
    ]
