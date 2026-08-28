"""Test the diagnostic body-dump helper used when Ollama 4xxs."""
import json
import os
from unittest.mock import MagicMock

import httpx
import pytest

from freecad_mcp import ollama_bridge
from freecad_mcp.ollama_bridge import _log_ollama_400, _post_json


def test_log_ollama_400_writes_file(tmp_path, monkeypatch):
    """``_log_ollama_400`` writes the body + response to ``/tmp/freecad-mcp``."""
    monkeypatch.setattr(ollama_bridge, "_HAS_HTTPX", True)
    monkeypatch.setattr(ollama_bridge, "httpx", httpx)

    fake_response = MagicMock()
    fake_response.status_code = 400
    fake_response.text = json.dumps({"error": "Value looks like object"})
    body = {"model": "qwen3.5:9b", "messages": [{"role": "user", "content": "hi"}]}

    _log_ollama_400("http://x/api/chat", body, 400, fake_response.text)

    # File should exist with the expected content
    # Same dir as the helper uses
    import glob
    real_files = glob.glob("/tmp/freecad-mcp/ollama_400_*.json")
    assert real_files, "_log_ollama_400 should have created a file in /tmp/freecad-mcp"
    # Verify the most recent dump is the one we just made
    latest = max(real_files, key=os.path.getmtime)
    dumped = json.load(open(latest))
    assert dumped["status"] == 400
    assert dumped["url"] == "http://x/api/chat"
    assert dumped["request"] == body
    assert "Value looks like object" in dumped["response"]


def test_post_json_dumps_body_on_400(monkeypatch, tmp_path):
    """``_post_json`` should dump the body to disk on any 4xx/5xx."""
    monkeypatch.setattr(ollama_bridge, "_HAS_HTTPX", True)
    # Build a fake httpx.post that raises HTTPStatusError on 400
    class FakeResp:
        status_code = 400
        text = '{"error":"test 400"}'

    def fake_post(*a, **k):
        req = httpx.Request("POST", "http://test/api/chat")
        resp = httpx.Response(400, text='{"error":"test 400"}', request=req)
        raise httpx.HTTPStatusError("400", request=req, response=resp)

    monkeypatch.setattr(httpx, "post", fake_post)

    body = {"model": "x", "messages": []}
    with pytest.raises(httpx.HTTPStatusError):
        _post_json("http://test/api/chat", body, 5.0)

    # A dump file should exist in /tmp/freecad-mcp
    import glob
    real_files = glob.glob("/tmp/freecad-mcp/ollama_400_*.json")
    assert real_files
