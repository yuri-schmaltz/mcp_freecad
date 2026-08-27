"""Tests for ``src/freecad_mcp/lmstudio_bridge.py``.

The HTTP layer is exercised with a real ``ThreadingHTTPServer`` bound
to ``127.0.0.1:0`` so we never clash with a developer's other ports.
"""
from __future__ import annotations

import json
import socket
import sys
import threading
import types
from pathlib import Path
import urllib.error
import urllib.request  # noqa: F401

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx  # noqa: E402

import freecad_mcp.lmstudio_bridge as lb  # noqa: E402
from freecad_mcp.lmstudio_bridge import LMStudioBridgeConfig  # noqa: E402


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class _StubTool:
    def __init__(self, name, description="", inputSchema=None):
        self.name = name
        self.description = description
        self.inputSchema = inputSchema


class _FakeSession:
    def __init__(self):
        self.tools = [_StubTool("echo", "echo", {"type": "object"})]
        self.calls: list[tuple[str, dict]] = []

    async def initialize(self): ...

    async def list_tools(self):
        return types.SimpleNamespace(tools=self.tools)

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        return types.SimpleNamespace(content=[types.SimpleNamespace(
            text="echoed", data=None)])


class _FakeHttp:
    """Mimics ``httpx.post`` for OpenAI-style responses."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls: list[httpx.Request] = []
        self.last_payload: dict | None = None

    def __call__(self, url, json, timeout, headers=None):
        self.last_payload = json
        self.calls.append(url)
        if not self.replies:
            raise AssertionError(f"unexpected extra POST to {url}")
        r = httpx.Response(200,
                           json=self.replies.pop(0),
                           request=httpx.Request("POST", url))
        return r


def _patch_session_cm(sess):
    """Factory that returns a session-backed async context manager."""
    class _Patch:
        def __init__(self_inner): self_inner._s = sess

        async def __aenter__(self_inner): return self_inner._s

        async def __aexit__(self_inner, *exc): return False
    return _Patch()


def _make_bridge_and_session(monkeypatch):
    """Build a bridge whose MCP session is the supplied fake."""
    sess = _FakeSession()
    cfg = LMStudioBridgeConfig(request_timeout_s=2, max_tool_iterations=3)
    bridge = lb.LMStudioMCPBridge(cfg)

    class _Open:
        async def __aenter__(self_inner): return (object(), object())
        async def __aexit__(self_inner, *exc): return False
    async def _open(): return _Open()
    bridge._open_mcp = _open  # type: ignore[assignment]
    monkeypatch.setattr(lb, "ClientSession", lambda r, w: _patch_session_cm(sess))
    return bridge, sess


# ---------------------------------------------------------------------------
# ask() against a fake upstream
# ---------------------------------------------------------------------------


async def test_ask_returns_final_answer_immediately(monkeypatch):
    bridge, _ = _make_bridge_and_session(monkeypatch)
    fake_http = _FakeHttp([{"choices": [{"message": {
        "role": "assistant", "content": "Plain answer."}}]}])
    monkeypatch.setattr(lb.httpx, "post", fake_http)
    out = await bridge.ask("hi", model="qwen3.6:27b")
    assert out == "Plain answer."
    # The payload sent upstream carries our user message + tools.
    assert fake_http.last_payload["messages"][0]["role"] == "user"
    assert fake_http.last_payload["messages"][0]["content"] == "hi"
    assert fake_http.last_payload["tools"][0]["function"]["name"] == "echo"


async def test_ask_dispatches_tool_calls_then_continues(monkeypatch):
    bridge, sess = _make_bridge_and_session(monkeypatch)
    fake_http = _FakeHttp([
        {"choices": [{"message": {"tool_calls": [
            {"id": "1", "type": "function",
             "function": {"name": "echo", "arguments": json.dumps({"a": 1})}}]}}]},
        {"choices": [{"message": {"content": "done after tool"}}]},
    ])
    monkeypatch.setattr(lb.httpx, "post", fake_http)
    out = await bridge.ask("do", model="m")
    assert out == "done after tool"
    assert sess.calls == [("echo", {"a": 1})]


async def test_ask_max_iterations_raises(monkeypatch):
    bridge, _ = _make_bridge_and_session(monkeypatch)
    fake_http = _FakeHttp([
        {"choices": [{"message": {"tool_calls": [
            {"function": {"name": "echo", "arguments": "{}"}}]}}]}] * 5)
    monkeypatch.setattr(lb.httpx, "post", fake_http)
    with pytest.raises(lb.LMStudioBridgeError, match="max_tool_iterations"):
        await bridge.ask("spin", model="m")


# ---------------------------------------------------------------------------
# HTTP serve() smoke test
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_serve_endpoint_proxies_to_bridge(monkeypatch):
    """Spawn the stdlib server, POST a request, read the OpenAI reply."""
    sess = _FakeSession()
    cfg = LMStudioBridgeConfig(request_timeout_s=2, max_tool_iterations=2)
    bridge = lb.LMStudioMCPBridge(cfg)

    class _Open:
        async def __aenter__(self_inner): return (object(), object())
        async def __aexit__(self_inner, *exc): return False
    async def _open(): return _Open()
    bridge._open_mcp = _open  # type: ignore[assignment]
    monkeypatch.setattr(lb, "ClientSession", lambda r, w: _patch_session_cm(sess))

    fake_http = _FakeHttp([{"choices": [{"message": {
        "role": "assistant", "content": "http hello"}}]}])
    monkeypatch.setattr(lb.httpx, "post", fake_http)

    port = _free_port()
    th = threading.Thread(target=lb.serve, kwargs={
        "host": "127.0.0.1", "port": port,
        "mcp_command": ("mcp-freecad",)}, daemon=True)
    th.start()

    # Poll for readiness: try POSTing bad payload (server returns 400 when alive).
    import time
    import urllib.error
    deadline = time.time() + 5
    body = json.dumps({"messages": []}).encode()
    while time.time() < deadline:
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                data=body, method="POST",
                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=0.5)
        except urllib.error.HTTPError as e:
            if e.code == 400:
                break
        except Exception:
            time.sleep(0.05)
    else:
        pytest.fail("server never came up")

    # Real call. We use urllib so the global httpx.post monkeypatch
    # (used by the bridge itself) doesn't intercept our client requests.
    body = {"model": "x", "messages": [{"role": "user", "content": "hello"}]}
    encoded = json.dumps(body).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        data=encoded, method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 200
        parsed = json.loads(resp.read())
    assert parsed["choices"][0]["message"]["content"] == "http hello"
    assert parsed["choices"][0]["message"]["role"] == "assistant"

    # 404 on a wrong path (urllib raises HTTPError).
    import urllib.error
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(urllib.request.Request(
            f"http://127.0.0.1:{port}/nope", data=encoded, method="POST",
            headers={"Content-Type": "application/json"}), timeout=5)
    assert exc_info.value.code == 404

    # Bad JSON → 400.
    bad_req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        data=b"not json", method="POST",
        headers={"Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(bad_req, timeout=5)
    assert exc_info.value.code == 400
