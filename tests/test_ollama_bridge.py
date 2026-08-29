"""Tests for ``src/freecad_mcp/ollama_bridge.py``.

We don't need a real Ollama server or a real MCP server here — we
stub the Ollama HTTP roundtrip and the MCP session so the bridge
loop can be exercised end-to-end on its own.
"""

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
        self.tools = [
            _StubTool("echo", "echo back", {"type": "object"}),
            _StubTool("noop", "no-op", {"type": "object"}),
        ]
        self.calls: list[tuple[str, dict]] = []
        self.next_result = types.SimpleNamespace(
            content=[
                types.SimpleNamespace(text="echo says hi", data=None),
            ]
        )

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
    http = _FakeHttp(
        [
            # After first user message
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"function": {"name": "echo", "arguments": {"msg": "hi"}}}],
                }
            },
            # After tool result
            {"message": {"role": "assistant", "content": "Done. Final answer."}},
        ]
    )

    sess = _FakeSession()
    sess.next_result = types.SimpleNamespace(
        content=[
            types.SimpleNamespace(text="echo says hi", data=None),
        ]
    )

    # Stub out the high-level transports
    monkeypatch.setattr(ob.httpx, "post", lambda url, json, timeout: http.post(url, json, timeout))

    # Patch stdio_client via the bridge's _open_mcp so we sidestep real MCP.
    class _FakeOpen:
        def __init__(self):
            self._cm = self  # self acts as both factory + cm

        async def __aenter__(self):
            return (object(), object())

        async def __aexit__(self, *exc):
            return False

    async def _fake_open_mcp():
        return _FakeOpen()

    bridge._open_mcp = _fake_open_mcp  # type: ignore[assignment]
    # Inject the fake session into ClientSession via monkeypatch.
    real_session = ob.ClientSession
    monkeypatch.setattr(ob, "ClientSession", lambda r, w: _PatchSessionCM(sess))

    yield bridge, sess, http

    monkeypatch.setattr(ob, "ClientSession", real_session)


class _PatchSessionCM:
    """Async cm that yields a fake session instead of going to MCP."""

    def __init__(self, session):
        self._s = session

    async def __aenter__(self):
        return self._s

    async def __aexit__(self, *exc):
        return False


async def test_ask_one_tool_call_returns_final_answer(patched_bridge):
    bridge, sess, http = patched_bridge
    out = await bridge.ask("hello?", model="qwen3.6:27b")
    assert out == "Done. Final answer."
    # The bridge invoked the echo tool once with the right args.
    assert sess.calls == [("echo", {"msg": "hi"})]
    # Two Ollama turns (initial + after tool result).
    assert len(http.calls) == 2


async def test_ask_raises_when_model_keeps_calling_tools(monkeypatch):
    cfg = ob.OllamaBridgeConfig(max_tool_iterations=2)
    bridge = ob.OllamaMCPBridge(cfg)
    http = _FakeHttp(
        [
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"function": {"name": "noop", "arguments": {}}}],
                }
            },
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"function": {"name": "noop", "arguments": {}}}],
                }
            },
        ]
    )
    monkeypatch.setattr(ob.httpx, "post", lambda url, json, timeout: http.post(url, json, timeout))
    sess = _FakeSession()
    sess.next_result = types.SimpleNamespace(content=[])

    class _Open:
        def __init__(self):
            pass

        async def __aenter__(self):
            return (object(), object())

        async def __aexit__(self, *exc):
            return False

    async def _fake_open_mcp():
        return _Open()

    bridge._open_mcp = _fake_open_mcp  # type: ignore[assignment]
    monkeypatch.setattr(ob, "ClientSession", lambda r, w: _PatchSessionCM(sess))

    with pytest.raises(ob.BridgeError, match="max_tool_iterations"):
        await bridge.ask("loop forever?", model="qwen3.6:27b")


async def test_ask_returns_content_when_no_tool_calls(monkeypatch):
    cfg = ob.OllamaBridgeConfig(max_tool_iterations=4)
    bridge = ob.OllamaMCPBridge(cfg)
    http = _FakeHttp(
        [
            {"message": {"role": "assistant", "content": "Straight answer."}},
        ]
    )
    monkeypatch.setattr(ob.httpx, "post", lambda url, json, timeout: http.post(url, json, timeout))
    sess = _FakeSession()

    class _Open:
        async def __aenter__(self):
            return (object(), object())

        async def __aexit__(self, *exc):
            return False

    async def _fake_open_mcp():
        return _Open()

    bridge._open_mcp = _fake_open_mcp  # type: ignore[assignment]
    monkeypatch.setattr(ob, "ClientSession", lambda r, w: _PatchSessionCM(sess))
    out = await bridge.ask("any q", model="qwen3.6:27b")
    assert out == "Straight answer."


async def test_ask_tool_failure_is_surfaced_not_raised(monkeypatch):
    """If a tool call raises, the bridge catches it and passes an error
    payload as a tool message back to the model — the loop survives."""
    cfg = ob.OllamaBridgeConfig(max_tool_iterations=4)
    bridge = ob.OllamaMCPBridge(cfg)

    class _BoomSession(_FakeSession):
        async def call_tool(self, name, args):
            raise RuntimeError("kaboom")

    http = _FakeHttp(
        [
            # First call to /api/chat: model decides to call tool (will fail)
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"function": {"name": "boom", "arguments": {}}}],
                }
            },
            # Second call: model sees the error envelope and answers
            {"message": {"role": "assistant", "content": "Tool failed but I recovered."}},
        ]
    )
    monkeypatch.setattr(ob.httpx, "post", lambda url, json, timeout: http.post(url, json, timeout))
    sess = _BoomSession()

    class _Open:
        async def __aenter__(self):
            return (object(), object())

        async def __aexit__(self, *exc):
            return False

    async def _fake_open_mcp():
        return _Open()

    bridge._open_mcp = _fake_open_mcp  # type: ignore[assignment]
    monkeypatch.setattr(ob, "ClientSession", lambda r, w: _PatchSessionCM(sess))

    out = await bridge.ask("go", model="qwen3.6:27b")
    assert "recovered" in out


# ---------------------------------------------------------------------------
# system prompt: small models need an explicit nudge to call tools
# ---------------------------------------------------------------------------


async def test_ask_includes_system_prompt_nudging_tool_use(monkeypatch):
    """Regression: smaller Ollama models (qwen3.5:9b, gemma4:12b) tend
    to answer in plain text instead of emitting ``tool_calls`` unless
    explicitly told the tools are how they take action. The bridge
    must prepend a system message that names the expectation.
    """
    cfg = ob.OllamaBridgeConfig(max_tool_iterations=2)
    bridge = ob.OllamaMCPBridge(cfg)
    http = _FakeHttp(
        [
            {"message": {"role": "assistant", "content": "Final answer."}},
        ]
    )
    monkeypatch.setattr(ob.httpx, "post", lambda url, json, timeout: http.post(url, json, timeout))
    sess = _FakeSession()

    class _Open:
        async def __aenter__(self):
            return (object(), object())

        async def __aexit__(self, *exc):
            return False

    async def _fake_open_mcp():
        return _Open()

    bridge._open_mcp = _fake_open_mcp  # type: ignore[assignment]
    monkeypatch.setattr(ob, "ClientSession", lambda r, w: _PatchSessionCM(sess))
    out = await bridge.ask("create a box", model="qwen3.5:9b")
    assert out == "Final answer."

    # Inspect the captured request body.
    assert http.calls, "bridge never POSTed to /api/chat"
    body = http.calls[0].read().decode()
    import json as _json
    sent = _json.loads(body)
    msgs = sent["messages"]
    assert msgs, "no messages in request"
    first = msgs[0]
    assert first.get("role") == "system", (
        f"first message must be a system prompt, got {first.get('role')!r}"
    )
    # The nudge must mention tools so small models know to use them.
    sys_text = first.get("content", "").lower()
    assert "tool" in sys_text, sys_text
    assert "mcp" in sys_text or "freecad" in sys_text, sys_text
    # And the user question still arrives right after.
    user_msgs = [m for m in msgs if m.get("role") == "user"]
    assert any(m.get("content") == "create a box" for m in user_msgs)


async def test_ask_sanitized_messages_keep_system_prompt(monkeypatch):
    """sanitize_messages_for_llm must not drop the system message we
    prepended. Only ``thinking`` and string-form tool_calls.arguments
    should be touched.
    """
    cfg = ob.OllamaBridgeConfig(max_tool_iterations=3)
    bridge = ob.OllamaMCPBridge(cfg)

    # Two-step loop so we can observe sanitization on iteration 2.
    http = _FakeHttp(
        [
            # iter 1: model wants to call a tool
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "thinking": "I should call create_object",
                    "tool_calls": [
                        {
                            "function": {
                                "name": "create_object",
                                "arguments": '{"name":"Box","type":"Part::Box"}',  # string form
                            }
                        }
                    ],
                }
            },
            # iter 2: model answers
            {"message": {"role": "assistant", "content": "Created."}},
        ]
    )
    monkeypatch.setattr(ob.httpx, "post", lambda url, json, timeout: http.post(url, json, timeout))
    sess = _FakeSession()
    sess.next_result = types.SimpleNamespace(
        content=[types.SimpleNamespace(text="ok", data=None)]
    )

    class _Open:
        async def __aenter__(self):
            return (object(), object())

        async def __aexit__(self, *exc):
            return False

    async def _fake_open_mcp():
        return _Open()

    bridge._open_mcp = _fake_open_mcp  # type: ignore[assignment]
    monkeypatch.setattr(ob, "ClientSession", lambda r, w: _PatchSessionCM(sess))
    out = await bridge.ask("build it", model="qwen3.5:9b")
    assert out == "Created."

    # On iter 2, the request must still carry the original system prompt.
    assert len(http.calls) == 2
    import json as _json
    second = _json.loads(http.calls[1].read().decode())
    roles = [m.get("role") for m in second["messages"]]
    assert roles[0] == "system", roles
    # And the tool_calls arguments were normalized from string → object.
    tool_msgs = [m for m in second["messages"] if m.get("role") == "assistant" and m.get("tool_calls")]
    assert tool_msgs
    args = tool_msgs[0]["tool_calls"][0]["function"]["arguments"]
    assert isinstance(args, dict), f"arguments must be object, got {type(args)}"
    assert args.get("name") == "Box"
    # And ``thinking`` was stripped so Ollama doesn't 400 on it.
    assert "thinking" not in tool_msgs[0]


# ---------------------------------------------------------------------------
# _post_json httpx vs urllib fallback
# ---------------------------------------------------------------------------


def test_post_json_falls_back_to_urllib_when_httpx_missing(monkeypatch):
    """Simulate a Python environment without httpx (e.g. system python3)
    and confirm _post_json still works using urllib only."""
    import threading
    import json as _json
    from http.server import BaseHTTPRequestHandler, HTTPServer

    received: list[bytes] = []

    class _H(BaseHTTPRequestHandler):
        def log_message(self, *_):
            return

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or "0")
            received.append(self.rfile.read(length))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"message":{"role":"assistant","content":"ok"}}')

    srv = HTTPServer(("127.0.0.1", 0), _H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        monkeypatch.setattr(ob, "_HAS_HTTPX", False, raising=False)
        out = ob._post_json(
            f"http://127.0.0.1:{port}/api/chat",
            {"model": "m", "messages": []},
            timeout=2.0,
        )
        assert out["message"]["content"] == "ok"
        # Verify the JSON body was actually sent.
        body = _json.loads(received[0].decode("utf-8"))
        assert body["model"] == "m"
    finally:
        srv.shutdown()
        srv.server_close()


def test_post_json_uses_httpx_when_available():
    """When httpx is importable we should go down the httpx branch."""
    assert ob._HAS_HTTPX is True
    # Just exercise the code path with a tiny stub server to prove the
    # branch is reachable — the urllib test above covers the other branch.
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class _H(BaseHTTPRequestHandler):
        def log_message(self, *_):
            return

        def do_POST(self):  # noqa: N802
            self.rfile.read(int(self.headers.get("Content-Length") or "0"))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')

    srv = HTTPServer(("127.0.0.1", 0), _H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        out = ob._post_json(f"http://127.0.0.1:{port}", {"x": 1}, timeout=2.0)
        assert out == {"ok": True}
    finally:
        srv.shutdown()
        srv.server_close()
