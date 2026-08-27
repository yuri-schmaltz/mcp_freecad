"""FreeCAD dock widget: MCP RPC server status + prompt launcher.

Lives as a Qt dock inside the FreeCAD main window. Shows:

* A coloured "LED" indicator + big status text (Running / Stopped /
  Error) and the bound host:port.
* A single ``Start / Stop`` button that mirrors the toolbar toggle.
* A multi-line prompt field + ``Enviar`` button that spawns the
  ``freecad_mcp.ollama_bridge`` console script as a subprocess and
  streams stdout/stderr into a read-only log box.

The panel never blocks the FreeCAD main thread: ``subprocess.Popen`` is
driven by ``QProcess`` so output is delivered via signals.
"""
from __future__ import annotations

import os
import shlex
import shutil

try:
    from PySide import QtCore, QtGui, QtWidgets  # PySide6 inside the flatpak
except Exception:  # pragma: no cover - only hit when run outside FreeCAD
    QtCore = QtGui = QtWidgets = None  # type: ignore[assignment]

from . import rpc_server

_PANEL_SINGLETON: MCPControlPanel | None = None


# ----- Status indicator widget ---------------------------------------------------


class _StatusLED(QtWidgets.QFrame):
    """Coloured circle with a label. Colour = server state."""

    COLOURS = {
        "running": "#22c55e",   # green-500
        "stopped": "#9ca3af",   # gray-400
        "error": "#ef4444",     # red-500
        "starting": "#f59e0b",  # amber-500
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("MCPLed")
        self._pix: QtGui.QPixmap | None = None

        self._label = QtWidgets.QLabel("Stopped")
        self._label.setStyleSheet("font-weight: 600; font-size: 14px;")
        self._detail = QtWidgets.QLabel("localhost:0")
        self._detail.setStyleSheet("color: #6b7280; font-size: 11px;")

        text_col = QtWidgets.QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(2)
        text_col.addWidget(self._label)
        text_col.addWidget(self._detail)

        led_col = QtWidgets.QVBoxLayout()
        led_col.setContentsMargins(0, 0, 0, 0)
        led_col.addStretch(1)
        self._led_label = QtWidgets.QLabel()
        self._led_label.setFixedSize(20, 20)
        led_col.addWidget(self._led_label, 0, QtCore.Qt.AlignVCenter)
        led_col.addStretch(1)

        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(8, 8, 8, 8)
        row.setSpacing(10)
        row.addLayout(led_col)
        row.addLayout(text_col)
        row.addStretch(1)

        self.set_state("stopped")

    def set_state(self, state: str, *, detail: str = "") -> None:
        if state not in self.COLOURS:
            state = "stopped"
        colour = self.COLOURS[state]
        self._led_label.setPixmap(self._make_led(colour))
        self._label.setText(
            {
                "running": "● Running",
                "stopped": "○ Stopped",
                "starting": "◌ Starting…",
                "error": "✕ Error",
            }[state]
        )
        self._detail.setText(detail or "—")

    def _make_led(self, hex_colour: str) -> QtGui.QPixmap:
        pm = QtGui.QPixmap(20, 20)
        pm.fill(QtCore.Qt.transparent)
        p = QtGui.QPainter(pm)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        p.setBrush(QtGui.QColor(hex_colour))
        p.setPen(QtGui.QPen(QtGui.QColor("#1f2937"), 1))
        p.drawEllipse(2, 2, 16, 16)
        p.end()
        return pm


# ----- Main dock widget -------------------------------------------------------


class MCPControlPanel(QtWidgets.QWidget):
    """Dockable widget with LED + start/stop + prompt + log."""

    POLL_INTERVAL_MS = 1000
    PROMPT_HISTORY_ENV = "FREECAD_MCP_OLLAMA_MODEL"

    def __init__(self, parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("MCPControlPanel")
        self.setWindowTitle("FreeCAD MCP")

        self._process: QtCore.QProcess | None = None
        self._build_ui()
        self._wire()

        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(self.POLL_INTERVAL_MS)
        self._timer.timeout.connect(self._refresh_status)
        self._timer.start()
        self._refresh_status()

    # ----- UI construction --------------------------------------------------

    def _build_ui(self) -> None:
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        # ---- status card
        card = QtWidgets.QFrame()
        card.setFrameShape(QtWidgets.QFrame.StyledPanel)
        card.setStyleSheet(
            "QFrame { background:#f3f4f6; border:1px solid #d1d5db; border-radius:6px; }"
        )
        card_layout = QtWidgets.QVBoxLayout(card)
        card_layout.setContentsMargins(8, 8, 8, 8)
        card_layout.setSpacing(6)

        self.led = _StatusLED()
        card_layout.addWidget(self.led)

        # toggle button row
        btn_row = QtWidgets.QHBoxLayout()
        self.toggle_btn = QtWidgets.QPushButton("Start RPC Server")
        self.toggle_btn.setMinimumHeight(32)
        self.toggle_btn.setCursor(QtCore.Qt.PointingHandCursor)
        btn_row.addWidget(self.toggle_btn, 1)
        card_layout.addLayout(btn_row)

        root.addWidget(card)

        # ---- prompt card
        prompt_card = QtWidgets.QFrame()
        prompt_card.setFrameShape(QtWidgets.QFrame.StyledPanel)
        prompt_card.setStyleSheet(
            "QFrame { background:#ffffff; border:1px solid #d1d5db; border-radius:6px; }"
        )
        pc_layout = QtWidgets.QVBoxLayout(prompt_card)
        pc_layout.setContentsMargins(8, 8, 8, 8)
        pc_layout.setSpacing(6)

        pc_layout.addWidget(QtWidgets.QLabel("Prompt para o Ollama:"))
        self.prompt_edit = QtWidgets.QPlainTextEdit()
        self.prompt_edit.setPlaceholderText(
            "Ex.: liste os documentos abertos no FreeCAD e rode health_check."
        )
        self.prompt_edit.setFixedHeight(96)
        pc_layout.addWidget(self.prompt_edit)

        # model + working dir row
        meta_row = QtWidgets.QHBoxLayout()
        meta_row.addWidget(QtWidgets.QLabel("Modelo:"))
        self.model_edit = QtWidgets.QLineEdit(
            os.environ.get(self.PROMPT_HISTORY_ENV, "qwen3.6:27b")
        )
        meta_row.addWidget(self.model_edit, 1)
        meta_row.addSpacing(8)
        self.auto_dispatch = QtWidgets.QCheckBox("Auto-dispatch (enviar sem revisar)")
        self.auto_dispatch.setChecked(False)
        meta_row.addWidget(self.auto_dispatch)
        pc_layout.addLayout(meta_row)

        action_row = QtWidgets.QHBoxLayout()
        self.send_btn = QtWidgets.QPushButton("Enviar")
        self.send_btn.setMinimumHeight(30)
        self.clear_btn = QtWidgets.QPushButton("Limpar log")
        self.clear_btn.setMinimumHeight(30)
        action_row.addWidget(self.send_btn)
        action_row.addWidget(self.clear_btn)
        action_row.addStretch(1)
        pc_layout.addLayout(action_row)

        self.log_view = QtWidgets.QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setStyleSheet(
            "QPlainTextEdit { background:#0b1020; color:#d1d5db; "
            "font-family: 'DejaVu Sans Mono', monospace; font-size:11px; }"
        )
        self.log_view.setMinimumHeight(140)
        pc_layout.addWidget(self.log_view, 1)

        root.addWidget(prompt_card, 1)

    # ----- wiring ----------------------------------------------------------

    def _wire(self) -> None:
        self.toggle_btn.clicked.connect(self._on_toggle)
        self.send_btn.clicked.connect(self._on_send)
        self.clear_btn.clicked.connect(self.log_view.clear)

    # ----- status ----------------------------------------------------------

    def _refresh_status(self) -> None:
        try:
            st = rpc_server.get_rpc_status()
        except Exception as e:
            self.led.set_state("error", detail=str(e))
            self.toggle_btn.setText("Start RPC Server")
            self.toggle_btn.setStyleSheet(_BTN_START)
            return

        running = bool(st.get("running"))
        host = st.get("host") or ("0.0.0.0" if st.get("remote_enabled") else "localhost")
        port = st.get("port") or 9875
        remote = st.get("remote_enabled", False)
        allow = st.get("allowed_ips", "127.0.0.1")

        if running:
            self.led.set_state(
                "running",
                detail=f"{host}:{port} — allow={allow}{' (remote)' if remote else ''}",
            )
            self.toggle_btn.setText("Stop RPC Server")
            self.toggle_btn.setStyleSheet(_BTN_STOP)
        else:
            self.led.set_state(
                "stopped",
                detail=f"port {port} — allow={allow}{' (remote)' if remote else ''}",
            )
            self.toggle_btn.setText("Start RPC Server")
            self.toggle_btn.setStyleSheet(_BTN_START)

    def _on_toggle(self) -> None:
        msg = rpc_server.toggle_rpc_server()
        self._append_log(f"[mcp] {msg}\n")
        self._refresh_status()

    # ----- prompt dispatch -------------------------------------------------

    def _on_send(self) -> None:
        if self._process is not None and self._process.state() != QtCore.QProcess.NotRunning:
            self._append_log("[mcp] já existe uma consulta em andamento.\n")
            return

        prompt = self.prompt_edit.toPlainText().strip()
        if not prompt:
            self._append_log("[mcp] prompt vazio — nada a enviar.\n")
            return

        model = self.model_edit.text().strip() or "qwen3.6:27b"
        os.environ["OLLAMA_HOST"] = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")

        argv, cwd = self._build_dispatch_argv(prompt, model)
        if argv is None:
            return

        pretty = " ".join(shlex.quote(a) for a in argv)
        self._append_log(f"[mcp] $ {pretty}\n")

        proc = QtCore.QProcess(self)
        proc.setProcessChannelMode(QtCore.QProcess.MergedChannels)
        proc.readyReadStandardOutput.connect(
            lambda: self._append_log(bytes(proc.readAllStandardOutput()).decode("utf-8", "replace"))
        )
        proc.finished.connect(self._on_process_finished)
        proc.errorOccurred.connect(self._on_process_error)
        if cwd:
            proc.setWorkingDirectory(cwd)
        self._process = proc
        proc.start(argv[0], argv[1:])

    def _build_dispatch_argv(self, prompt: str, model: str) -> tuple[list[str] | None, str | None]:
        """Return (argv, cwd) ready for ``QProcess.start``.

        Strategy:
        1. Prefer the local checkout venv (if present) so the user
           sees their dev code reflected immediately.
        2. Fall back to the system ``python -m freecad_mcp.ollama_bridge``.
        3. Fall back to ``uvx mcp-freecad`` if available.
        """
        cwd = os.getcwd() if QtWidgets else "."
        repo_root = os.environ.get("FREECAD_MCP_REPO_ROOT")

        # 1) local dev checkout
        if repo_root and os.path.isdir(os.path.join(repo_root, ".venv")):
            py = os.path.join(repo_root, ".venv", "bin", "python")
            if os.path.isfile(py):
                return [py, "-m", "freecad_mcp.ollama_bridge", prompt, "--model", model], repo_root

        # 2) system python with the package installed
        for py in (shutil.which("python3"), shutil.which("python")):
            if not py:
                continue
            return [py, "-m", "freecad_mcp.ollama_bridge", prompt, "--model", model], cwd

        # 3) uvx fallback (PyPI install)
        uvx = shutil.which("uvx")
        if uvx:
            return [uvx, "--from", "mcp-freecad", "python", "-m",
                    "freecad_mcp.ollama_bridge", prompt, "--model", model], cwd

        self._append_log(
            "[mcp] ERRO: não encontrei python nem uvx no PATH. "
            "Instale `uvx` ou rode `pip install -e .[dev]` no repo.\n"
        )
        return None, None

    def _on_process_finished(self, code: int, status: QtCore.QProcess.ExitStatus) -> None:
        self._append_log(f"[mcp] processo terminou (code={code}, status={status}).\n")
        self._process = None
        self._refresh_status()

    def _on_process_error(self, err: QtCore.QProcess.ProcessError) -> None:
        self._append_log(f"[mcp] QProcess erro: {err}\n")
        self._process = None

    def _append_log(self, text: str) -> None:
        self.log_view.moveCursor(QtGui.QTextCursor.End)
        self.log_view.insertPlainText(text)
        self.log_view.moveCursor(QtGui.QTextCursor.End)


# ----- Button style sheets ------------------------------------------------------

_BTN_BASE = (
    "QPushButton { color:white; border:none; border-radius:4px; "
    "font-weight:600; padding:4px 12px; }"
    "QPushButton:hover { filter:brightness(1.1); }"
    "QPushButton:pressed { padding-top:5px; padding-bottom:3px; }"
)
_BTN_START = _BTN_BASE + "QPushButton { background:#16a34a; }"   # green
_BTN_STOP = _BTN_BASE + "QPushButton { background:#dc2626; }"    # red


# ----- Public helpers ---------------------------------------------------------


def get_or_create_panel(mw: QtWidgets.QWidget | None = None) -> MCPControlPanel:
    """Return the singleton panel, creating it if needed."""
    global _PANEL_SINGLETON
    if _PANEL_SINGLETON is not None:
        return _PANEL_SINGLETON
    panel = MCPControlPanel(mw)
    _PANEL_SINGLETON = panel
    return panel


def show_panel() -> MCPControlPanel:
    """Create the dock (if missing) and make it visible + focused."""
    import FreeCADGui

    mw = FreeCADGui.getMainWindow()
    panel = get_or_create_panel(mw)

    # Wrap inside a QDockWidget the first time we are shown.
    if not hasattr(panel, "_dock") or panel._dock is None:
        dock = QtWidgets.QDockWidget("FreeCAD MCP", mw)
        dock.setObjectName("MCPControlDock")
        dock.setWidget(panel)
        dock.setAllowedAreas(QtCore.Qt.LeftDockWidgetArea | QtCore.Qt.RightDockWidgetArea)
        mw.addDockWidget(QtCore.Qt.RightDockWidgetArea, dock)
        panel._dock = dock  # type: ignore[attr-defined]

    dock = panel._dock  # type: ignore[attr-defined]
    dock.show()
    dock.raise_()
    return panel


def notify_status_change() -> None:
    """Called by the toolbar toggle when the server state changes."""
    panel = _PANEL_SINGLETON
    if panel is not None:
        panel._refresh_status()


__all__ = ["MCPControlPanel", "get_or_create_panel", "show_panel", "notify_status_change"]
