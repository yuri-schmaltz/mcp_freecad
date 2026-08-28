"""Tests for addon/FreeCADMCP/rpc_server/_ollama_models.py."""

from __future__ import annotations

import importlib.util
import json
import os
import socket
import sys
import threading
import types
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer


def _import_ollama_models():
    """Load ``_ollama_models`` against the same synthetic package
    used by ``test_panel_dispatch.py`` so relative imports resolve.
    """

    rpc_server_dir = os.path.realpath(
        os.path.join(os.path.dirname(__file__), "..", "addon", "FreeCADMCP", "rpc_server")
    )

    if "FreeCADMCP" not in sys.modules:
        pkg = types.ModuleType("FreeCADMCP")
        pkg.__path__ = []
        pkg_rpc = types.ModuleType("FreeCADMCP.rpc_server")
        pkg_rpc.__path__ = [rpc_server_dir]
        sys.modules["FreeCADMCP"] = pkg
        sys.modules["FreeCADMCP.rpc_server"] = pkg_rpc

    module_path = os.path.join(rpc_server_dir, "_ollama_models.py")
    spec = importlib.util.spec_from_file_location("FreeCADMCP.rpc_server._ollama_models", module_path)
    module = importlib.util.module_from_spec(spec)
    # Register in sys.modules BEFORE exec so dataclass can resolve typing.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ollama_models = _import_ollama_models()


# --- Fake HTTP server fixture -------------------------------------------------


class _OllamaFakeServer:
    """Tiny HTTP server that mimics ``GET /api/tags``."""

    def __init__(self, payload: dict | None = None, status: int = 200) -> None:
        self.payload = payload
        self.status = status
        self.requests: list[tuple[str, str]] = []

        payload_ref = payload
        status_ref = status
        requests_ref = self.requests

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args) -> None:  # silence stderr
                return

            def do_GET(self) -> None:  # noqa: N802
                requests_ref.append((self.command, self.path))
                self.send_response(status_ref)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                if payload_ref is not None:
                    self.wfile.write(json.dumps(payload_ref).encode("utf-8"))
                else:
                    self.wfile.write(b"")

        self._server = HTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@contextmanager
def _ollama_up(payload: dict | None = None, status: int = 200):
    srv = _OllamaFakeServer(payload, status)
    try:
        yield srv
    finally:
        srv.close()


# --- Tests --------------------------------------------------------------------


class OllamaModelInfoTests(unittest.TestCase):
    def test_from_api_extracts_canonical_fields(self) -> None:
        raw = {
            "name": "qwen3.6:27b",
            "size": 17420432739,
            "modified_at": "2026-08-12T19:58:18Z",
            "digest": "deadbeef",
            "details": {
                "family": "qwen35",
                "parameter_size": "27.8B",
                "quantization_level": "Q4_K_M",
                "capabilities": ["vision", "completion", "tools"],
            },
        }
        info = ollama_models.OllamaModelInfo.from_api(raw)
        self.assertEqual(info.name, "qwen3.6:27b")
        self.assertEqual(info.family, "qwen35")
        self.assertEqual(info.parameter_size, "27.8B")
        self.assertEqual(info.quantization_level, "Q4_K_M")
        self.assertEqual(info.capabilities, ("vision", "completion", "tools"))
        self.assertIn("qwen3.6:27b", info.display())
        self.assertIn("27.8B", info.display())

    def test_from_api_handles_missing_details(self) -> None:
        info = ollama_models.OllamaModelInfo.from_api({"name": "foo"})
        self.assertEqual(info.name, "foo")
        self.assertEqual(info.capabilities, ())
        self.assertEqual(info.family, "")


class ListOllamaModelsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved_env = {k: os.environ.get(k) for k in ("OLLAMA_HOST",)}

    def tearDown(self) -> None:
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_returns_parsed_models(self) -> None:
        with _ollama_up(
            {
                "models": [
                    {
                        "name": "qwen3.6:27b",
                        "size": 1,
                        "details": {"parameter_size": "27B", "family": "qwen"},
                        "capabilities": ["tools"],
                    },
                    {"name": "gemma4:12b", "size": 2},
                ]
            }
        ) as srv:
            result = ollama_models.list_ollama_models(srv.url, timeout=2.0)

        self.assertTrue(result.ok, msg=result.error)
        self.assertEqual(result.names, ("qwen3.6:27b", "gemma4:12b"))
        self.assertEqual(result.models[0].family, "qwen")
        self.assertEqual(result.models[0].capabilities, ("tools",))
        self.assertEqual(srv.requests, [("GET", "/api/tags")])

    def test_uses_ollama_host_env_when_url_omitted(self) -> None:
        with _ollama_up({"models": []}) as srv:
            os.environ["OLLAMA_HOST"] = srv.url
            result = ollama_models.list_ollama_models(timeout=2.0)
        self.assertTrue(result.ok)
        self.assertEqual(result.url, srv.url)

    def test_reports_ollama_unreachable(self) -> None:
        # Bind then immediately close to get a free port that's unreachable.
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        result = ollama_models.list_ollama_models(f"http://127.0.0.1:{port}", timeout=0.5)
        self.assertFalse(result.ok)
        self.assertIn("não está respondendo", result.error)

    def test_reports_http_error(self) -> None:
        with _ollama_up(status=502, payload=None) as srv:
            result = ollama_models.list_ollama_models(srv.url, timeout=2.0)
        self.assertFalse(result.ok)
        self.assertIn("HTTP 502", result.error)

    def test_reports_invalid_json(self) -> None:
        # Malformed JSON body served with HTTP 200.
        with _ollama_up(payload={"models": "not-a-list"}, status=200) as srv:
            result = ollama_models.list_ollama_models(srv.url, timeout=2.0)
        self.assertFalse(result.ok)
        self.assertIn("lista", result.error)

    def test_reports_non_list_models(self) -> None:
        with _ollama_up(payload={"models": {"oops": "dict"}}, status=200) as srv:
            result = ollama_models.list_ollama_models(srv.url, timeout=2.0)
        self.assertFalse(result.ok)
        self.assertIn("lista", result.error)

    def test_skips_malformed_model_entries(self) -> None:
        with _ollama_up(
            payload={
                "models": [
                    {"name": "ok:1"},
                    "not a dict",
                    {"name": "ok:2"},
                ]
            },
            status=200,
        ) as srv:
            result = ollama_models.list_ollama_models(srv.url, timeout=2.0)
        self.assertTrue(result.ok, msg=result.error)
        self.assertEqual(result.names, ("ok:1", "ok:2"))

    def test_reports_empty_ollama_host(self) -> None:
        os.environ.pop("OLLAMA_HOST", None)
        result = ollama_models.list_ollama_models("   ", timeout=0.5)
        self.assertFalse(result.ok)
        self.assertIn("vazio", result.error)


if __name__ == "__main__":
    unittest.main()
