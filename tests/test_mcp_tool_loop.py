"""Tests for the shared MCP tool-loop driver in
``src/freecad_mcp/_mcp_tool_loop.py``.

The driver is callback-based — we don't need a real MCP server or a
real LLM; fake session + scripted replies are enough.
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from freecad_mcp import _mcp_tool_loop as mtl  # noqa: E402


# ---------------------------------------------------------------------------
# mcp_tool_to_openai
# ---------------------------------------------------------------------------


class _StubTool:
    def __init__(self, name, description="", inputSchema=None):
        self.name = name
        self.description = description
        self.inputSchema = inputSchema


def test_mcp_tool_to_openai_basic():
    tool = _StubTool("ping", "ping", {"type": "object", "properties": {"x": {}}})
    out = mtl.mcp_tool_to_openai(tool)
    assert out == {"type": "function",
                    "function": {"name": "ping", "description": "ping",
                                  "parameters": {"type": "object",
                                                  "properties": {"x": {}}}}}


def test_mcp_tool_to_openai_missing_schema_pads_type():
    tool = _StubTool("foo")
    out = mtl.mcp_tool_to_openai(tool)
    assert out["function"]["parameters"]["type"] == "object"
    assert out["function"]["parameters"]["properties"] == {}


def test_mcp_tool_to_openai_description_defaults_empty():
    tool = _StubTool("foo", description=None)
    out = mtl.mcp_tool_to_openai(tool)
    assert out["function"]["description"] == ""


# ---------------------------------------------------------------------------
# _result_to_text
# ---------------------------------------------------------------------------


def test_result_to_text_joins_text_parts_with_newline():
    a = types.SimpleNamespace(text="alpha", data=None)
    b = types.SimpleNamespace(text="beta", data=None)
    assert mtl._result_to_text(types.SimpleNamespace(content=[a, b])) == "alpha\nbeta"


def test_result_to_text_falls_back_to_data_when_no_text():
    item = types.SimpleNamespace(text=None, data={"k": "v"})
    out = mtl._result_to_text(types.SimpleNamespace(content=[item]))
    assert "k" in out and "v" in out


def test_result_to_text_empty_content_returns_marker():
    assert mtl._result_to_text(types.SimpleNamespace(content=None)) == "(empty result)"


# ---------------------------------------------------------------------------
# run_tool_loop
# ---------------------------------------------------------------------------


class _FakeSession:
    """Records ``call_tool`` invocations and returns scripted results."""

    def __init__(self, tool_results):
        self.calls: list[tuple[str, dict]] = []
        self._results = list(tool_results)

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        if not self._results:
            return types.SimpleNamespace(content=[])
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _pick_message(r):
    return r.get("message", {})


def _pick_tool_calls(message):
    return list(message.get("tool_calls") or [])


def _pick_content(message):
    return str(message.get("content") or "")


async def test_run_tool_loop_returns_final_answer_immediately():
    """No tool_calls on the first reply → returns the content."""
    sess = _FakeSession([])
    replies = iter([{"message": {"role": "assistant",
                                   "content": "  done.  "}}])
    seen: list[list[dict]] = []

    async def _send(loop):
        seen.append(list(loop.messages))
        loop.last_reply = next(replies)

    res = await mtl.run_tool_loop(sess, [{"role": "user", "content": "hi"}],
                                  pick_message=_pick_message,
                                  pick_tool_calls=_pick_tool_calls,
                                  pick_content=_pick_content,
                                  call_one_step=_send, max_iterations=3)
    assert res.content == "done."  # stripped
    assert res.iterations == 1
    assert seen[0][0]["content"] == "hi"


async def test_run_tool_loop_dispatches_tool_calls_and_continues():
    sess = _FakeSession([types.SimpleNamespace(content=[types.SimpleNamespace(
        text="tool-1-out", data=None)])])
    replies = iter([
        {"message": {"role": "assistant", "tool_calls": [
            {"name": "do_x", "arguments": {"a": 1}}]}},
        {"message": {"role": "assistant", "content": "answered"}},
    ])

    async def _send(loop):
        loop.last_reply = next(replies)

    res = await mtl.run_tool_loop(sess, [{"role": "user", "content": "go"}],
                                  pick_message=_pick_message,
                                  pick_tool_calls=_pick_tool_calls,
                                  pick_content=_pick_content,
                                  call_one_step=_send, max_iterations=5)
    assert res.content == "answered"
    assert res.iterations == 2
    assert sess.calls == [("do_x", {"a": 1})]
    # tool result must be appended as a role: tool message
    roles = [m["role"] for m in res.messages]
    assert "tool" in roles


async def test_run_tool_loop_parses_string_arguments():
    """OpenAI servers send arguments as a JSON string."""
    sess = _FakeSession([types.SimpleNamespace(content=[])])
    replies = iter([
        {"message": {"tool_calls": [
            {"function": {"name": "f", "arguments": json.dumps({"x": 7})}}]}},
        {"message": {"content": "ok"}},
    ])

    async def _send(loop):
        loop.last_reply = next(replies)

    await mtl.run_tool_loop(sess, [{"role": "user", "content": "x"}],
                             pick_message=_pick_message,
                             pick_tool_calls=_pick_tool_calls,
                             pick_content=_pick_content,
                             call_one_step=_send, max_iterations=3)
    assert sess.calls == [("f", {"x": 7})]


async def test_run_tool_loop_surfaces_tool_exception_as_tool_error():
    """Tool errors must not crash the loop — they go back to the model."""
    sess = _FakeSession([RuntimeError("boom")])
    replies = iter([
        {"message": {"tool_calls": [{"name": "bad", "arguments": {}}]}},
        {"message": {"content": "I see the error"}},
    ])

    async def _send(loop):
        loop.last_reply = next(replies)

    res = await mtl.run_tool_loop(sess, [{"role": "user", "content": "x"}],
                                  pick_message=_pick_message,
                                  pick_tool_calls=_pick_tool_calls,
                                  pick_content=_pick_content,
                                  call_one_step=_send, max_iterations=3)
    assert res.content == "I see the error"
    # The tool message carries the error string.
    last_tool = next(m for m in res.messages if m.get("role") == "tool")
    assert "RuntimeError" in last_tool["content"]
    assert "boom" in last_tool["content"]


async def test_run_tool_loop_raises_when_iterations_exhausted():
    sess = _FakeSession([types.SimpleNamespace(content=[])] * 10)

    async def _always_calls(loop):
        loop.last_reply = {"message": {"tool_calls": [
            {"name": "spin", "arguments": {}}]}}

    with pytest.raises(RuntimeError, match="max_tool_iterations"):
        await mtl.run_tool_loop(sess, [{"role": "user", "content": "x"}],
                                pick_message=_pick_message,
                                pick_tool_calls=_pick_tool_calls,
                                pick_content=_pick_content,
                                call_one_step=_always_calls,
                                max_iterations=2)
    assert len(sess.calls) == 2
