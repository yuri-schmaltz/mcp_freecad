"""Tests for ``src/freecad_mcp/ollama_bridge.py``.

We don't need a real Ollama server or a real MCP server here — we
stub the Ollama HTTP roundtrip and the MCP session so the bridge
loop can be exercised end-to-end on its own.
"""
import asyncio
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx
import freecad_mcp.ollama_bridge as ob  # noqa: E402


# ---------------------------------------------------------------------------
# _mcp_tool_to_ollama
# ---------------------------------------------------------------------------


class _StubTool:
    def __init__(self, name, description="", inputSchema=None):
        self.name = name
        self.description = description
        self.inputSchema = inputSchema


def test_mcp_tool_to_ollama_basic():
    tool = _StubTool("ping", "ping the server", {"type": "object", "properties": {"x": {}}})
    out = ob._mcp_tool_to_ollama(tool)
    assert out["type"] == "function"
    assert out["function"]["name"] == "ping"
    assert out["function"]["description"] == "ping the server"
    assert out["function"]["parameters"]["properties"] == {"x": {}}


def test_mcp_tool_to_ollama_missing_schema_is_object():
    tool = _StubTool("foo")  # no inputSchema at all
    out = ob._mcp_tool_to_ollama(tool)
    assert out["function"]["parameters"]["type"] == "object"


def test_mcp_tool_to_ollama_non_object_schema_gets_type():
    tool = _StubTool("bar", inputSchema={"properties": {}})  # no type
    out = ob._mcp_tool_to_ollama(tool)
    assert out["function"]["parameters"]["type"] == "object"


# ---------------------------------------------------------------------------
# _result_to_text
# ---------------------------------------------------------------------------


def test_result_to_text_concatenates_text_items():
    item = types.SimpleNamespace(text="hello", data="x")
    result = types.SimpleNamespace(content=[item])
    assert ob._result_to_text(result) == "hello"


def test_result_to_text_handles_data_only_items():
    item = types.SimpleNamespace(text=None, data={"k": 1})
    out = ob._result_to_text(types.SimpleNamespace(content=[item]))
    assert "k" in out


def test_result_to_text_empty_content_returns_marker():
    assert ob._result_to_text(types.SimpleNamespace(content=None)) == "(empty result)"


def test_result_to_text_multiple_parts_joined_by_newline():
    a = types.SimpleNamespace(text="a", data="x")
    b = types.SimpleNamespace(text="b", data="y")
    assert ob._result_to_text(types.SimpleNamespace(content=[a, b])) == "a\nb"


# ---------------------------------------------------------------------------
# ask() happy-path via fakes
# ---------------------------------------------------------------------------


class _FakeSession:
    def __init__(self):
        self.tools = [_StubTool("echo", "echo back", {"type": "object"}),
                      _StubTool("noop", "no-op", {"type": "object"})]
        self.calls: list[tuple[str, dict]] = []
        self.next_result = types.SimpleNamespace(content=[
            types.SimpleNamespace(text="echo says hi", data=None),
        ])

    async def initialize(self): ...

    async def list_tools(self):
        return types.SimpleNamespace(tools=self.tools)

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        return self.next_result


class _FakeHttp:
    """A trimmed ``httpx.AsyncClient`` that returns canned responses."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls: list[httpx.Request] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def post(self, url, json, timeout):
        # Reproduce the API the bridge uses synchronously (inside to_thread)
        reply = self.replies.pop(0)
        req = httpx.Request("POST", url, json=json)
        self.calls.append(req)
        return types.SimpleNamespace(
            status_code=200,
            raise_for_status=lambda: None,
            json=lambda: reply,
        )


@pytest.fixture
def patched_bridge(monkeypatch):
    """Replace the bridge's HTTP and MCP stubs with controllable fakes."""
    cfg = ob.OllamaBridgeConfig(max_tool_iterations=4)
    bridge = ob.OllamaMCPBridge(cfg)

    # First HTTP reply: model decides to call a tool.
    # Second HTTP reply: model gives a final answer.
    http = _FakeHttp([
        # After first user message
        {"message": {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "echo", "arguments": {"msg": "hi"}}}]}},
        # After tool result
        {"message": {"role": "assistant", "content": "Done. Final answer."}},
    ])

    sess = _FakeSession()
    sess.next_result = types.SimpleNamespace(content=[
        types.SimpleNamespace(text="echo says hi", data=None),
    ])

    # Stub out the high-level transports
    monkeypatch.setattr(ob.httpx, "post", lambda url, json, timeout: http.post(url, json, timeout))

    # Patch stdio_client via the bridge's _open_mcp so we sidestep real MCP.
    class _FakeOpen:
        def __init__(self): self._cm = self  # self acts as both factory + cm
        async def __aenter__(self): return (object(), object())
        async def __aexit__(self, *exc): return False

    async def _fake_open_mcp():
        return _FakeOpen()

    bridge._open_mcp = _fake_open_mcp  # type: ignore[assignment]
    # Inject the fake session into ClientSession via monkeypatch.
    real_session = ob.ClientSession
    monkeypatch.setattr(ob, "ClientSession",
                        lambda r, w: _PatchSessionCM(sess))

    yield bridge, sess, http

    monkeypatch.setattr(ob, "ClientSession", real_session)


class _PatchSessionCM:
    """Async cm that yields a fake session instead of going to MCP."""

    def __init__(self, session): self._s = session

    async def __aenter__(self): return self._s

    async def __aexit__(self, *exc): return False


def test_ask_one_tool_call_returns_final_answer(patched_bridge):
    bridge, sess, http = patched_bridge
    out = asyncio.run(bridge.ask("hello?", model="qwen3.6:27b"))
    assert out == "Done. Final answer."
    # The bridge invoked the echo tool once with the right args.
    assert sess.calls == [("echo", {"msg": "hi"})]
    # Two Ollama turns (initial + after tool result).
    assert len(http.calls) == 2


def test_ask_raises_when_model_keeps_calling_tools(monkeypatch):
    cfg = ob.OllamaBridgeConfig(max_tool_iterations=2)
    bridge = ob.OllamaMCPBridge(cfg)
    http = _FakeHttp([
        {"message": {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "noop", "arguments": {}}}]}},
        {"message": {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "noop", "arguments": {}}}]}},
    ])
    monkeypatch.setattr(ob.httpx, "post",
                        lambda url, json, timeout: http.post(url, json, timeout))
    sess = _FakeSession()
    sess.next_result = types.SimpleNamespace(content=[])

    class _Open:
        def __init__(self): pass
        async def __aenter__(self): return (object(), object())
        async def __aexit__(self, *exc): return False

    async def _fake_open_mcp(): return _Open()
    bridge._open_mcp = _fake_open_mcp  # type: ignore[assignment]
    monkeypatch.setattr(ob, "ClientSession", lambda r, w: _PatchSessionCM(sess))

    with pytest.raises(ob.BridgeError, match="max_tool_iterations"):
        asyncio.run(bridge.ask("loop forever?", model="qwen3.6:27b"))


def test_ask_returns_content_when_no_tool_calls(monkeypatch):
    cfg = ob.OllamaBridgeConfig(max_tool_iterations=4)
    bridge = ob.OllamaMCPBridge(cfg)
    http = _FakeHttp([
        {"message": {"role": "assistant", "content": "Straight answer."}},
    ])
    monkeypatch.setattr(ob.httpx, "post",
                        lambda url, json, timeout: http.post(url, json, timeout))
    sess = _FakeSession()
    class _Open:
        async def __aenter__(self): return (object(), object())
        async def __aexit__(self, *exc): return False
    async def _fake_open_mcp(): return _Open()
    bridge._open_mcp = _fake_open_mcp  # type: ignore[assignment]
    monkeypatch.setattr(ob, "ClientSession", lambda r, w: _PatchSessionCM(sess))
    out = asyncio.run(bridge.ask("any q", model="qwen3.6:27b"))
    assert out == "Straight answer."


def test_ask_tool_failure_is_surfaced_not_raised(monkeypatch):
    """If a tool call raises, the bridge catches it and passes an error
    payload as a tool message back to the model — the loop survives."""
    cfg = ob.OllamaBridgeConfig(max_tool_iterations=4)
    bridge = ob.OllamaMCPBridge(cfg)

    class _BoomSession(_FakeSession):
        async def call_tool(self, name, args):
            raise RuntimeError("kaboom")

    http = _FakeHttp([
        # First call to /api/chat: model decides to call tool (will fail)
        {"message": {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "boom", "arguments": {}}}]}},
        # Second call: model sees the error envelope and answers
        {"message": {"role": "assistant", "content": "Tool failed but I recovered."}},
    ])
    monkeypatch.setattr(ob.httpx, "post",
                        lambda url, json, timeout: http.post(url, json, timeout))
    sess = _BoomSession()
    class _Open:
        async def __aenter__(self): return (object(), object())
        async def __aexit__(self, *exc): return False
    async def _fake_open_mcp(): return _Open()
    bridge._open_mcp = _fake_open_mcp  # type: ignore[assignment]
    monkeypatch.setattr(ob, "ClientSession", lambda r, w: _PatchSessionCM(sess))

    out = asyncio.run(bridge.ask("go", model="qwen3.6:27b"))
    assert "recovered" in out
