"""Tests for src/freecad_mcp/web_ui.py."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

try:
    from fastapi.testclient import TestClient
    import httpx
    import freecad_mcp.web_ui as wu
except Exception as e:
    pytest.skip(f"fastapi not available: {e}", allow_module_level=True)


class _FakeFreeCAD:
    def __init__(self, healthy: bool = True) -> None:
        self.healthy = healthy
        self.calls: list = []

    def health_check(self):
        self.calls.append(("health_check", {}))
        if self.healthy:
            return {"success": True, "uptime": 42}
        return {"success": False, "error": "rpc down"}


def _client(monkeypatch, *, healthy: bool = True):
    conn = _FakeFreeCAD(healthy=healthy)
    app = wu.create_web_app(conn, ollama_url="http://ollama.test", default_model="m1")
    return TestClient(app), conn


def _patch_ollama(monkeypatch, payload):
    class _StubClient:
        def __init__(self_inner, *a, **kw):
            pass

        async def __aenter__(self_inner):
            return self_inner

        async def __aexit__(self_inner, *exc):
            return False

        async def post(self_inner, url, json):
            if isinstance(payload, Exception):
                raise payload
            req = httpx.Request("POST", url, json=json)
            return httpx.Response(200, json=payload, request=req)

    monkeypatch.setattr(wu.httpx, "AsyncClient", _StubClient)


def test_root_returns_html(monkeypatch) -> None:
    client, _ = _client(monkeypatch)
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "FreeCAD MCP" in r.text


def test_root_injects_default_model(monkeypatch) -> None:
    client, _ = _client(monkeypatch)
    r = client.get("/")
    assert 'value="m1"' in r.text


def test_health_ok(monkeypatch) -> None:
    client, conn = _client(monkeypatch)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert conn.calls == [("health_check", {})]


def test_health_degraded_returns_503(monkeypatch) -> None:
    client, _ = _client(monkeypatch, healthy=False)
    r = client.get("/health")
    assert r.status_code == 503
    assert r.json()["status"] == "degraded"


def test_docs_lists_tools(monkeypatch) -> None:
    client, _ = _client(monkeypatch)
    r = client.get("/docs")
    assert r.status_code == 200
    body = r.json()
    assert "create_document" in body["tools"]
    assert body["count"] == len(body["tools"])


def test_ask_proxies_to_ollama(monkeypatch) -> None:
    client, _ = _client(monkeypatch)
    _patch_ollama(
        monkeypatch,
        {"model": "m1", "message": {"role": "assistant", "content": "ok"}},
    )
    r = client.post("/ask", json={"prompt": "hi", "model": "m1"})
    assert r.status_code == 200
    body = r.json()
    assert body["response"] == "ok"
    assert body["model"] == "m1"
    assert isinstance(body["duration_ms"], (int, float))


def test_ask_returns_502_on_upstream_error(monkeypatch) -> None:
    client, _ = _client(monkeypatch)
    _patch_ollama(monkeypatch, httpx.ConnectError("down"))
    r = client.post("/ask", json={"prompt": "hi", "model": "m1"})
    assert r.status_code == 502


def test_ask_rejects_empty_prompt(monkeypatch) -> None:
    client, _ = _client(monkeypatch)
    r = client.post("/ask", json={"prompt": "", "model": "m1"})
    assert r.status_code == 422


def test_ask_accepts_non_default_model(monkeypatch) -> None:
    client, _ = _client(monkeypatch)
    _patch_ollama(monkeypatch, {"model": "x", "message": {"content": "hi"}})
    r = client.post("/ask", json={"prompt": "hello", "model": "x"})
    assert r.status_code == 200
    assert r.json()["response"] == "hi"
