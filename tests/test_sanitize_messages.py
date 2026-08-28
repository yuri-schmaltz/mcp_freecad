"""Unit tests for ``_mcp_tool_loop.sanitize_messages_for_llm``."""
import json
from freecad_mcp._mcp_tool_loop import sanitize_messages_for_llm


def test_strips_thinking_field() -> None:
    """The model adds ``thinking`` to its own replies; drop it on re-send."""
    msgs = [
        {"role": "user", "content": "olá"},
        {
            "role": "assistant",
            "content": "",
            "thinking": "user said olá",
            "tool_calls": [{"function": {"name": "f", "arguments": {"x": 1}}}],
        },
    ]
    out = sanitize_messages_for_llm(msgs)
    assert out[1].get("thinking") is None, "thinking must be stripped"
    assert out[1]["role"] == "assistant"


def test_converts_string_arguments_to_object() -> None:
    """Ollama 400s when ``arguments`` is a JSON-encoded string."""
    msgs = [
        {"role": "user", "content": "x"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"function": {"name": "f", "arguments": json.dumps({"x": 1})}}
            ],
        },
    ]
    out = sanitize_messages_for_llm(msgs)
    args = out[1]["tool_calls"][0]["function"]["arguments"]
    assert isinstance(args, dict)
    assert args == {"x": 1}


def test_keeps_object_arguments() -> None:
    """When ``arguments`` is already an object, leave it alone."""
    msgs = [
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "f", "arguments": {"x": 1}}}
        ]}
    ]
    out = sanitize_messages_for_llm(msgs)
    assert out[0]["tool_calls"][0]["function"]["arguments"] == {"x": 1}


def test_empty_string_arguments_become_empty_dict() -> None:
    msgs = [
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "f", "arguments": ""}}
        ]}
    ]
    out = sanitize_messages_for_llm(msgs)
    assert out[0]["tool_calls"][0]["function"]["arguments"] == {}


def test_passes_through_non_assistant_messages() -> None:
    """User and tool messages must not be mutated."""
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "tool", "name": "f", "content": "{}"},
    ]
    out = sanitize_messages_for_llm(msgs)
    assert out == msgs


def test_handles_message_without_tool_calls() -> None:
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "all good"},
    ]
    out = sanitize_messages_for_llm(msgs)
    assert out == msgs


def test_invalid_json_arguments_are_left_alone() -> None:
    """If ``arguments`` is malformed JSON, don't crash — log + leave."""
    msgs = [
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "f", "arguments": "{not valid json"}}
        ]}
    ]
    out = sanitize_messages_for_llm(msgs)
    args = out[0]["tool_calls"][0]["function"]["arguments"]
    assert isinstance(args, str)
    assert args == "{not valid json"


def test_multiple_tool_calls_all_normalized() -> None:
    msgs = [
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "f1", "arguments": json.dumps({"a": 1})}},
            {"function": {"name": "f2", "arguments": {"b": 2}}},
        ]}
    ]
    out = sanitize_messages_for_llm(msgs)
    assert out[0]["tool_calls"][0]["function"]["arguments"] == {"a": 1}
    assert out[0]["tool_calls"][1]["function"]["arguments"] == {"b": 2}


def test_does_not_mutate_input_list() -> None:
    """The function must return a new list — never mutate the caller's list."""
    msg = {
        "role": "assistant",
        "thinking": "secret",
        "tool_calls": [{"function": {"name": "f", "arguments": "{}"}}],
    }
    msgs = [msg]
    sanitize_messages_for_llm(msgs)
    assert "thinking" in msg, "input must be unchanged"
    assert isinstance(msg["tool_calls"][0]["function"]["arguments"], str)
