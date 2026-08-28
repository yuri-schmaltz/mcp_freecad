"""Tests for the Ollama model picker in addon/FreeCADMCP/rpc_server/_panel.py."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import threading
import types
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock


# --- Qt stubs ----------------------------------------------------------------


class _FakeComboBox:
    """Tiny stand-in for QComboBox that records API calls."""

    NoInsert = 0

    def __init__(self) -> None:
        self._items: list[tuple[str, str]] = []  # (display, userData)
        self._current = ""
        self._signals_blocked = False
        self.signals_blocked_calls = 0
        self.set_editable_calls = 0
        self.added_items: list[tuple[str, str]] = []
        self.cleared_count = 0

    def setEditable(self, _flag: bool) -> None:  # noqa: N802
        self.set_editable_calls += 1

    def setInsertPolicy(self, _p: int) -> None:  # noqa: N802
        return

    def addItem(self, display: str, userData: object = None) -> None:  # noqa: N802
        ud = "" if userData is None else str(userData)
        self._items.append((display, ud))
        self.added_items.append((display, ud))
        if not self._current:
            self._current = display

    def clear(self) -> None:
        self._items.clear()
        self._current = ""
        self.added_items.clear()
        self.cleared_count += 1

    def findData(self, key: str) -> int:
        for i, (_display, ud) in enumerate(self._items):
            if ud == key:
                return i
        return -1

    def setCurrentIndex(self, idx: int) -> None:  # noqa: N802
        if 0 <= idx < len(self._items):
            self._current = self._items[idx][0]

    def setEditText(self, text: str) -> None:  # noqa: N802
        self._current = text

    def currentText(self) -> str:  # noqa: N802
        return self._current

    def blockSignals(self, flag: bool) -> None:  # noqa: N802
        self._signals_blocked = flag
        self.signals_blocked_calls += 1


class _FakeLineEdit:
    def __init__(self, text: str = "") -> None:
        self._text = text

    def text(self) -> str:
        return self._text

    def setText(self, t: str) -> None:
        self._text = t


def _install_qt_stub() -> None:
    """Force a Qt stub on sys.modules that has all classes _panel.py touches.

    The conftest of this project sometimes installs a ``SimpleNamespace``
    as ``PySide.QtWidgets`` (see tests/test_settings_fallback.py).
    ``setdefault`` would not overwrite that, so we explicitly check and
    replace with a fresh ``ModuleType`` that carries the rich class set.
    """
    qwid_modules = ("PySide", "PySide.QtCore", "PySide.QtGui", "PySide.QtWidgets")
    mod_ps = types.ModuleType("PySide")
    mod_ps_core = types.ModuleType("PySide.QtCore")
    mod_ps_gui = types.ModuleType("PySide.QtGui")
    mod_ps_wid = types.ModuleType("PySide.QtWidgets")
    sys.modules["PySide"] = mod_ps
    sys.modules["PySide.QtCore"] = mod_ps_core
    sys.modules["PySide.QtGui"] = mod_ps_gui
    sys.modules["PySide.QtWidgets"] = mod_ps_wid
    for name in qwid_modules:
        sys.modules.pop(name, None)
    sys.modules["PySide"] = mod_ps
    sys.modules["PySide.QtCore"] = mod_ps_core
    sys.modules["PySide.QtGui"] = mod_ps_gui
    sys.modules["PySide.QtWidgets"] = mod_ps_wid

    class _NoOp:
        def __init__(self, *a, **k):
            pass

        def __getattr__(self, _name):
            return lambda *a, **k: None

    mod_ps_wid.QFrame = _NoOp
    mod_ps_wid.QLabel = _NoOp
    mod_ps_wid.QPlainTextEdit = _NoOp
    mod_ps_wid.QComboBox = _FakeComboBox
    mod_ps_wid.QPushButton = _NoOp
    mod_ps_wid.QVBoxLayout = _NoOp
    mod_ps_wid.QHBoxLayout = _NoOp
    mod_ps_wid.QSizePolicy = _NoOp
    mod_ps_wid.QWidget = _NoOp
    mod_ps_wid.QApplication = _NoOp
    mod_ps_wid.QLineEdit = _FakeLineEdit
    mod_ps_wid.QCheckBox = _NoOp
    mod_ps_core.QProcess = _NoOp
    mod_ps_core.QTimer = _NoOp
    mod_ps_core.MergedChannels = 0
    mod_ps_gui.QFont = _NoOp
    mod_ps_gui.QPixmap = _NoOp
    mod_ps_gui.QPainter = _NoOp
    mod_ps_gui.QColor = _NoOp
    mod_ps_gui.QPen = _NoOp
    mod_ps_gui.QPalette = _NoOp
    mod_ps_core.Qt = _NoOp
    mod_ps_gui.QTextCursor = _NoOp


# --- Synthetic package + import ---------------------------------------------


def _import_panel():
    rpc_server_dir = os.path.realpath(
        os.path.join(os.path.dirname(__file__), "..", "addon", "FreeCADMCP", "rpc_server")
    )
    pkg = types.ModuleType("FreeCADMCP")
    pkg.__path__ = []
    pkg_rpc = types.ModuleType("FreeCADMCP.rpc_server")
    pkg_rpc.__path__ = [rpc_server_dir]
    sys.modules["FreeCADMCP"] = pkg
    sys.modules["FreeCADMCP.rpc_server"] = pkg_rpc

    fake_rpc = types.ModuleType("FreeCADMCP.rpc_server.rpc_server")
    fake_rpc.start_rpc_server = lambda *a, **k: None
    fake_rpc.stop_rpc_server = lambda *a, **k: None
    fake_rpc.is_rpc_server_running = lambda: False
    fake_rpc.get_rpc_server_host_port = lambda: ("127.0.0.1", 9875, "127.0.0.1")
    sys.modules["FreeCADMCP.rpc_server.rpc_server"] = fake_rpc

    fake_prompts = types.ModuleType("FreeCADMCP.rpc_server._prompt_templates")
    fake_prompts.PromptTemplateRegistry = type(
        "PromptTemplateRegistry",
        (),
        {"names": lambda self: ["Caixa 10x10x10"], "get": lambda self, n: n},
    )
    sys.modules["FreeCADMCP.rpc_server._prompt_templates"] = fake_prompts

    # Load _ollama_models first so it's a real module on sys.modules.
    spec = importlib.util.spec_from_file_location(
        "FreeCADMCP.rpc_server._ollama_models",
        os.path.join(rpc_server_dir, "_ollama_models.py"),
    )
    ollama_mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = ollama_mod
    spec.loader.exec_module(ollama_mod)

    panel_path = os.path.join(rpc_server_dir, "_panel.py")
    spec = importlib.util.spec_from_file_location("FreeCADMCP.rpc_server._panel", panel_path)
    panel_mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = panel_mod
    spec.loader.exec_module(panel_mod)
    return panel_mod


# Lazy initialization — done in the test class setUp to avoid races
# with the conftest's PySide shims during collection.
_panel_mod = None
_saved_qt_modules: dict[str, object] = {}


def _save_qt_modules() -> None:
    """Snapshot whatever Qt modules are in sys.modules so we can restore."""
    for name in ("PySide", "PySide.QtCore", "PySide.QtGui", "PySide.QtWidgets"):
        _saved_qt_modules[name] = sys.modules.get(name)


def _restore_qt_modules() -> None:
    """Restore the snapshot taken by ``_save_qt_modules``.

    The conftest's ``test_settings_fallback`` test installs its own
    SimpleNamespace-based PySide stubs and expects them to still be in
    place after the test suite moves on. Without this restore, the next
    test fails with ``AttributeError: 'SimpleNamespace' object has no
    attribute 'QFrame'``.
    """
    for name, mod in _saved_qt_modules.items():
        if mod is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = mod


def _get_panel_mod():
    """Re-import the panel module with the current Qt shim.

    Returns a fresh module each call so that subsequent imports inside
    the same process see the freshly installed (and later restored)
    ``PySide.*`` shims.
    """

    # Drop any cached panel module so the next import re-runs.
    sys.modules.pop("FreeCADMCP.rpc_server._panel", None)
    if "FreeCADMCP" in sys.modules:
        sys.modules.pop("FreeCADMCP", None)
        sys.modules.pop("FreeCADMCP.rpc_server", None)
        sys.modules.pop("FreeCADMCP.rpc_server.rpc_server", None)
        sys.modules.pop("FreeCADMCP.rpc_server._prompt_templates", None)
        sys.modules.pop("FreeCADMCP.rpc_server._ollama_models", None)
    return _import_panel()


# --- Fake Ollama HTTP server ------------------------------------------------


class _OllamaFakeServer:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self.payload = payload
        self.status = status
        self.requests: list[str] = []

        payload_ref = payload
        status_ref = status
        requests_ref = self.requests

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a):
                return

            def do_GET(self) -> None:  # noqa: N802
                requests_ref.append(self.path)
                self.send_response(status_ref)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(payload_ref).encode("utf-8"))

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
def _ollama(payload: dict | None = None, status: int = 200):
    srv = _OllamaFakeServer(payload or {"models": []}, status)
    try:
        yield srv
    finally:
        srv.close()


# --- Tests ------------------------------------------------------------------


class RefreshModelsTests(unittest.TestCase):
    def setUp(self) -> None:
        # Save whatever Qt shims the conftest (or earlier test) installed
        # so we can put them back in tearDown and not pollute siblings.
        _save_qt_modules()
        _install_qt_stub()
        self.panel_mod = _get_panel_mod()
        self.MCPControlPanel = self.panel_mod.MCPControlPanel

    def tearDown(self) -> None:
        _restore_qt_modules()

    def _make_panel(self):
        """Build a panel instance without invoking the full __init__.

        We bypass ``__init__`` because that calls ``singleShot(0, ...)``
        and other Qt machinery that our stubs can't honor. We set up
        only the attributes ``_on_refresh_models`` touches.
        """
        cls = self.MCPControlPanel
        panel = cls.__new__(cls)
        panel._models_loaded = False
        panel._append_log = lambda msg: None  # type: ignore[assignment]
        panel.model_combo = _FakeComboBox()
        # Seed with a single model entry mirroring what __init__ does so
        # tests reflect the real "user had a previous selection" state.
        panel.model_combo.addItem("placeholder", userData="placeholder")
        panel.ollama_host_edit = _FakeLineEdit("")
        return panel

    def test_populates_combo_with_models(self) -> None:
        with _ollama(
            payload={
                "models": [
                    {"name": "qwen3.6:27b", "details": {"parameter_size": "27.8B"}},
                    {"name": "gemma4:12b"},
                ]
            }
        ) as srv:
            panel = self._make_panel()
            panel.ollama_host_edit.setText(srv.url)
            panel._on_refresh_models()
            self.assertEqual(panel.model_combo.cleared_count, 1)
            names = [ud for _d, ud in panel.model_combo.added_items]
            self.assertEqual(names, ["qwen3.6:27b", "gemma4:12b"])
            self.assertTrue(panel._models_loaded)

    def test_preserves_previous_selection_when_present(self) -> None:
        with _ollama(
            payload={
                "models": [
                    {"name": "qwen3.6:27b"},
                    {"name": "gemma4:12b"},
                ]
            }
        ) as srv:
            panel = self._make_panel()
            panel.ollama_host_edit.setText(srv.url)
            panel.model_combo.setEditText("gemma4:12b")
            panel._on_refresh_models()
            self.assertEqual(panel.model_combo.currentText(), "gemma4:12b")

    def test_keeps_previous_text_when_model_not_listed(self) -> None:
        with _ollama(payload={"models": [{"name": "only:one"}]}) as srv:
            panel = self._make_panel()
            panel.ollama_host_edit.setText(srv.url)
            panel.model_combo.setEditText("ghost:42")
            panel._on_refresh_models()
            # "ghost:42" is not in /api/tags → falls back to setEditText.
            self.assertEqual(panel.model_combo.currentText(), "ghost:42")

    def test_logs_error_when_ollama_unreachable(self) -> None:
        logs: list[str] = []

        def _log(msg: str) -> None:
            logs.append(msg)

        panel = self._make_panel()
        panel._append_log = _log  # type: ignore[assignment]
        # Point at a definitely-closed port (bind then close).
        import socket as _s

        with _s.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        panel.ollama_host_edit.setText(f"http://127.0.0.1:{port}")
        panel._on_refresh_models()
        self.assertTrue(any("indisponível" in m for m in logs), msg=logs)

    def test_logs_error_on_http_error(self) -> None:
        logs: list[str] = []

        def _log(msg: str) -> None:
            logs.append(msg)

        with _ollama(payload={}, status=502) as srv:
            panel = self._make_panel()
            panel._append_log = _log  # type: ignore[assignment]
            panel.ollama_host_edit.setText(srv.url)
            panel._on_refresh_models()
        self.assertTrue(any("HTTP 502" in m for m in logs), msg=logs)

    def test_blocks_signals_during_repopulate(self) -> None:
        with _ollama(payload={"models": [{"name": "m:1"}]}) as srv:
            panel = self._make_panel()
            panel.ollama_host_edit.setText(srv.url)
            panel._on_refresh_models()
            # Two blockSignals calls: True (block) and False (unblock).
            self.assertGreaterEqual(panel.model_combo.signals_blocked_calls, 2)

    def test_no_url_in_edit_uses_environ(self) -> None:
        with _ollama(payload={"models": [{"name": "m:env"}]}) as srv:
            panel = self._make_panel()
            panel.ollama_host_edit = _FakeLineEdit("")
            with mock.patch.dict(os.environ, {"OLLAMA_HOST": srv.url}, clear=False):
                panel._on_refresh_models()
            self.assertEqual([ud for _d, ud in panel.model_combo.added_items], ["m:env"])


if __name__ == "__main__":
    unittest.main()
