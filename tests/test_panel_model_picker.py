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

    def currentData(self, role: int = 0) -> str:  # noqa: N802
        # Look up the userData for the currently-selected display.
        for display, ud in self._items:
            if display == self._current:
                return ud
        # Fallback for setEditText-style scenarios where no item matches.
        return ""

    def blockSignals(self, flag: bool) -> None:  # noqa: N802
        self._signals_blocked = flag
        self.signals_blocked_calls += 1


class _FakeLineEdit:
    def __init__(self, text: str = "") -> None:
        self._text = text

    def text(self) -> str:
        return self._text

    def toPlainText(self) -> str:  # QTextEdit compat
        return self._text

    def setText(self, t: str) -> None:
        self._text = t


class _FakeBusyLabel:
    """Tracks text and visibility so the busy indicator can be asserted."""

    def __init__(self) -> None:
        self._text = ""
        self._visible = False
        self.set_text_calls: list[str] = []
        self.set_style_calls: list[str] = []
        self.show_calls = 0
        self.hide_calls = 0

    def setText(self, t: str) -> None:  # noqa: N802
        self._text = t
        self.set_text_calls.append(t)

    def text(self) -> str:
        return self._text

    def setStyleSheet(self, s: str) -> None:  # noqa: N802
        self.set_style_calls.append(s)

    def show(self) -> None:
        self._visible = True
        self.show_calls += 1

    def hide(self) -> None:
        self._visible = False
        self.hide_calls += 1

    def isVisible(self) -> bool:  # noqa: N802
        return self._visible


class _FakeBusyButton:
    """Tracks enable/disable and text/style transitions for the send button."""

    def __init__(self) -> None:
        self._enabled = True
        self._text = "Enviar"
        self._style = ""
        self.set_enabled_calls: list[bool] = []
        self.set_text_calls: list[str] = []
        self.set_style_calls: list[str] = []
        self.set_min_height_calls = 0

    def setEnabled(self, flag: bool) -> None:  # noqa: N802
        self._enabled = flag
        self.set_enabled_calls.append(flag)

    def isEnabled(self) -> bool:  # noqa: N802
        return self._enabled

    def setText(self, t: str) -> None:  # noqa: N802
        self._text = t
        self.set_text_calls.append(t)

    def text(self) -> str:
        return self._text

    def setStyleSheet(self, s: str) -> None:  # noqa: N802
        self._style = s
        self.set_style_calls.append(s)

    def setMinimumHeight(self, _h: int) -> None:  # noqa: N802
        self.set_min_height_calls += 1


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
    mod_ps_wid.QLabel = _FakeBusyLabel
    mod_ps_wid.QPlainTextEdit = _NoOp
    mod_ps_wid.QComboBox = _FakeComboBox
    mod_ps_wid.QPushButton = _FakeBusyButton
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


class SendPromptModelSelectionTests(unittest.TestCase):
    """Regression: ``_on_send`` must pass the *model name* (userData),
    not the display string. Sending the display string made Ollama
    reject every request with ``{"error":"invalid model name"}``.
    """

    def setUp(self) -> None:
        _save_qt_modules()
        _install_qt_stub()
        self.panel_mod = _get_panel_mod()
        self.MCPControlPanel = self.panel_mod.MCPControlPanel

    def tearDown(self) -> None:
        _restore_qt_modules()

    def _install_panel_attrs(self, panel: object) -> None:
        """Add the attributes ``_on_send`` reads before our test target."""
        # `_process is not None and state() != NotRunning` check.
        panel._process = None
        # Logs go to devnull.
        panel._append_log = lambda msg: None  # type: ignore[assignment]
        # Prompt source.
        if not hasattr(panel, "prompt_edit"):
            panel.prompt_edit = _FakeLineEdit("hello?")
        # Provide harmless no-ops for env/QProcess plumbing so the
        # non-sandbox branch can complete without spinning a real process.
        panel._build_subprocess_env = lambda argv: None  # type: ignore[assignment]
        panel._on_process_finished = lambda *a, **kw: None  # type: ignore[assignment]
        panel._on_process_error = lambda *a, **kw: None  # type: ignore[assignment]
        # Busy indicator widgets — installed by _build_ui, but tests
        # bypass __init__, so we wire them up here.
        panel.send_btn = _FakeBusyButton()
        panel.busy_label = _FakeBusyLabel()
        # Animation timer stub: capture start/stop calls.
        panel._busy = False
        panel._busy_frame = 0
        timer_calls: list[str] = []

        class _FakeTimer:
            def __init__(self) -> None:
                self._running = False

            def setInterval(self, _ms: int) -> None:  # noqa: N802
                pass

            def start(self) -> None:
                self._running = True
                timer_calls.append("start")

            def stop(self) -> None:
                self._running = False
                timer_calls.append("stop")

            def timeout(self) -> None:
                return None

        panel._busy_timer = _FakeTimer()  # type: ignore[assignment]
        panel._timer_calls = timer_calls  # type: ignore[attr-defined]
        # Theme stub (called when busy state clears to restore button style).
        panel._apply_theme = lambda t: None  # type: ignore[assignment]
        panel._theme = "auto"

    def _build_panel_with_display_model(self) -> object:
        """Build a panel-like object whose ``model_combo`` carries one
        entry whose ``currentText()`` includes a display suffix but whose
        ``currentData()`` holds the bare model name — exactly like the
        real combo after ``_on_refresh_models``.
        """
        cls = self.MCPControlPanel
        panel = cls.__new__(cls)
        panel.model_combo = _FakeComboBox()
        panel.model_combo.addItem(
            "qwen3.6:27b — 27.8B — Q4_K_M — vision,completion,tools,thinking",
            userData="qwen3.6:27b",
        )
        panel.ollama_host_edit = _FakeLineEdit("")
        panel.prompt_edit = _FakeLineEdit("hello?")
        self._install_panel_attrs(panel)
        # Pretend we're not sandboxed so we hit the dispatch path.
        panel._is_sandboxed = lambda: False  # type: ignore[assignment]
        return panel

    def test_on_send_uses_model_name_not_display(self) -> None:
        """Regression for 'invalid model name' 400 from Ollama."""
        panel = self._build_panel_with_display_model()
        self._install_panel_attrs(panel)
        captured: dict[str, object] = {}

        def fake_dispatch(self, prompt: str, model: str):
            captured["prompt"] = prompt
            captured["model"] = model
            return (["echo"], None)

        with mock.patch.object(
            self.MCPControlPanel,
            "_build_dispatch_argv",
            autospec=True,
            side_effect=fake_dispatch,
        ), mock.patch(
            "FreeCADMCP.rpc_server._panel.QtCore.QProcess", autospec=False
        ), mock.patch.dict(
            os.environ, {}, clear=False
        ):
            panel._on_send()

        self.assertEqual(captured.get("model"), "qwen3.6:27b")
        self.assertNotIn("—", captured.get("model", ""))

    def test_on_send_falls_back_to_default_when_combo_empty(self) -> None:
        """Empty combo (no userData, no text) → default model."""
        cls = self.MCPControlPanel
        panel = cls.__new__(cls)
        panel.model_combo = _FakeComboBox()
        # No items added — currentText() == "" and currentData() == "".
        panel.ollama_host_edit = _FakeLineEdit("")
        panel.prompt_edit = _FakeLineEdit("hi")
        self._install_panel_attrs(panel)
        panel._is_sandboxed = lambda: False  # type: ignore[assignment]

        captured: dict[str, object] = {}

        def fake_dispatch(self, prompt: str, model: str):
            captured["model"] = model
            return (["echo"], None)

        with mock.patch.object(
            self.MCPControlPanel,
            "_build_dispatch_argv",
            autospec=True,
            side_effect=fake_dispatch,
        ), mock.patch(
            "FreeCADMCP.rpc_server._panel.QtCore.QProcess", autospec=False
        ):
            panel._on_send()

        self.assertEqual(captured.get("model"), "qwen3.6:27b")

    def test_on_send_uses_userdata_when_in_sandbox(self) -> None:
        """When sandboxed, ``_start_in_process(prompt, model)`` must get
        the bare name, not the display.
        """
        panel = self._build_panel_with_display_model()
        self._install_panel_attrs(panel)
        panel._is_sandboxed = lambda: True  # type: ignore[assignment]

        captured: dict[str, object] = {}

        def fake_start_in_process(self, prompt: str, model: str):
            captured["prompt"] = prompt
            captured["model"] = model

        with mock.patch.object(
            self.MCPControlPanel,
            "_start_in_process",
            autospec=True,
            side_effect=fake_start_in_process,
        ):
            panel._on_send()

        self.assertEqual(captured.get("model"), "qwen3.6:27b")


class BusyIndicatorTests(unittest.TestCase):
    """The send button must surface 'IA pensando' feedback while a query
    is in flight (disabling input, animating a spinner, restoring on done).
    """

    def setUp(self) -> None:
        _save_qt_modules()
        _install_qt_stub()
        self.panel_mod = _get_panel_mod()
        self.MCPControlPanel = self.panel_mod.MCPControlPanel

    def tearDown(self) -> None:
        _restore_qt_modules()

    def _make_panel(self) -> tuple[object, _FakeBusyButton, _FakeBusyLabel]:
        cls = self.MCPControlPanel
        panel = cls.__new__(cls)
        send_btn = _FakeBusyButton()
        busy_label = _FakeBusyLabel()
        panel.send_btn = send_btn
        panel.busy_label = busy_label
        panel._busy = False
        panel._busy_frame = 0

        # Animation timer stub.
        class _FakeTimer:
            def __init__(self) -> None:
                self.running = False

            def setInterval(self, _ms: int) -> None:  # noqa: N802
                pass

            def start(self) -> None:
                self.running = True

            def stop(self) -> None:
                self.running = False

        panel._busy_timer = _FakeTimer()  # type: ignore[assignment]
        panel._apply_theme = lambda t: None  # type: ignore[assignment]
        panel._theme = "auto"
        return panel, send_btn, busy_label

    def test_set_busy_true_shows_label_and_disables_button(self) -> None:
        panel, send_btn, busy_label = self._make_panel()
        panel._set_busy(True)
        self.assertTrue(busy_label.isVisible())
        self.assertIn("pensando", busy_label.text())
        self.assertFalse(send_btn.isEnabled())
        self.assertEqual(send_btn.text(), "Pensando…")
        self.assertTrue(panel._busy_timer.running)
        self.assertTrue(panel._busy)

    def test_set_busy_false_restores_button(self) -> None:
        panel, send_btn, busy_label = self._make_panel()
        panel._set_busy(True)
        panel._set_busy(False)
        self.assertFalse(busy_label.isVisible())
        self.assertTrue(send_btn.isEnabled())
        self.assertEqual(send_btn.text(), "Enviar")
        self.assertFalse(panel._busy_timer.running)
        self.assertFalse(panel._busy)

    def test_set_busy_is_idempotent(self) -> None:
        panel, send_btn, busy_label = self._make_panel()
        panel._set_busy(True)
        # Second True call must not re-arm (no extra setText calls, no
        # extra style changes), so the animation does not jitter.
        text_calls_before = len(busy_label.set_text_calls)
        panel._set_busy(True)
        self.assertEqual(len(busy_label.set_text_calls), text_calls_before)
        self.assertEqual(send_btn.set_enabled_calls.count(False), 1)

    def test_tick_busy_advances_spinner_frames(self) -> None:
        panel, _, busy_label = self._make_panel()
        panel._set_busy(True)
        first_text = busy_label.text()
        panel._tick_busy()
        second_text = busy_label.text()
        self.assertNotEqual(first_text, second_text)
        # After 4 ticks we should be back at frame 0.
        for _ in range(3):
            panel._tick_busy()
        self.assertEqual(busy_label.text(), first_text)

    def test_tick_busy_noop_when_not_busy(self) -> None:
        panel, _, _ = self._make_panel()
        # Not busy → tick must be a no-op (no AttributeError, no state change).
        panel._tick_busy()
        self.assertFalse(panel._busy)

    def test_set_busy_restores_themed_button_style(self) -> None:
        """The busy state overrides the button style; on clear we must
        reapply the current theme so the button doesn't stay amber.
        """
        panel, send_btn, _ = self._make_panel()
        applied: list[str] = []
        panel._apply_theme = lambda t: applied.append(t)  # type: ignore[assignment]
        panel._set_busy(True)
        panel._set_busy(False)
        self.assertEqual(applied, [panel._theme])


if __name__ == "__main__":
    unittest.main()
