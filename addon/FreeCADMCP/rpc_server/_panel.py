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
from ._ollama_models import list_ollama_models
from ._prompt_templates import PromptTemplateRegistry

_PANEL_SINGLETON: MCPControlPanel | None = None


# ----- Status indicator widget ---------------------------------------------------


class _StatusLED(QtWidgets.QFrame):
    """Coloured circle with a label. Colour = server state."""

    COLOURS = {
        "running": "#22c55e",  # green-500
        "stopped": "#9ca3af",  # gray-400
        "error": "#ef4444",  # red-500
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
    THEME_ENV = "FREECAD_MCP_PANEL_THEME"
    OLLAMA_HOST_ENV = "OLLAMA_HOST"
    VENV_PATH_ENV = "FREECAD_MCP_VENV"

    def __init__(self, parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("MCPControlPanel")
        self.setWindowTitle("FreeCAD MCP")

        self._process: QtCore.QProcess | None = None
        self._theme = "auto"
        self._template_registry = PromptTemplateRegistry()
        self._models_loaded = False
        self._build_ui()
        self._wire()
        self._apply_theme(
            os.environ.get(self.THEME_ENV, "auto")
            if os.environ.get(self.THEME_ENV, "auto") in ("light", "dark", "auto")
            else "auto"
        )

        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(self.POLL_INTERVAL_MS)
        self._timer.timeout.connect(self._refresh_status)
        self._timer.start()
        self._refresh_status()

        # Auto-populate the model combobox once on startup so the user
        # sees installed Ollama models without clicking the refresh button.
        # We schedule it with a zero-delay timer so the dock is fully
        # realised before we hit the network.
        QtCore.QTimer.singleShot(0, self._on_refresh_models)

    # ----- UI construction --------------------------------------------------

    def _build_ui(self) -> None:
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        # ---- status card
        card = QtWidgets.QFrame()
        card.setFrameShape(QtWidgets.QFrame.StyledPanel)
        card.setObjectName("MCPCard")
        self._status_card = card
        card.setStyleSheet("QFrame { background:#f3f4f6; border:1px solid #d1d5db; border-radius:6px; }")
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
        prompt_card.setObjectName("MCPPromptCard")
        self._prompt_card = prompt_card
        prompt_card.setStyleSheet(
            "QFrame { background:#ffffff; border:1px solid #d1d5db; border-radius:6px; }"
        )
        pc_layout = QtWidgets.QVBoxLayout(prompt_card)
        pc_layout.setContentsMargins(8, 8, 8, 8)
        pc_layout.setSpacing(6)

        pc_layout.addWidget(QtWidgets.QLabel("Prompt para o Ollama:"))

        # F2: template selector
        template_row = QtWidgets.QHBoxLayout()
        template_row.addWidget(QtWidgets.QLabel("Template:"))
        self.template_combo = QtWidgets.QComboBox()
        self.template_combo.addItem("(escolha um template)")
        for name in self._template_registry.names():
            self.template_combo.addItem(name)
        template_row.addWidget(self.template_combo, 1)
        pc_layout.addLayout(template_row)

        self.prompt_edit = QtWidgets.QPlainTextEdit()
        self.prompt_edit.setPlaceholderText(
            "Ex.: liste os documentos abertos no FreeCAD e rode health_check."
        )
        self.prompt_edit.setFixedHeight(96)
        pc_layout.addWidget(self.prompt_edit)

        # model + auto-dispatch row (combo + refresh)
        meta_row = QtWidgets.QHBoxLayout()
        meta_row.addWidget(QtWidgets.QLabel("Modelo:"))
        self.model_combo = QtWidgets.QComboBox()
        self.model_combo.setEditable(True)
        self.model_combo.setInsertPolicy(QtWidgets.QComboBox.NoInsert)
        initial_model = os.environ.get(self.PROMPT_HISTORY_ENV, "qwen3.6:27b")
        self.model_combo.addItem(initial_model)
        self.model_combo.setCurrentText(initial_model)
        meta_row.addWidget(self.model_combo, 1)
        self.refresh_btn = QtWidgets.QPushButton("⟳")
        self.refresh_btn.setFixedWidth(32)
        self.refresh_btn.setToolTip("Buscar modelos instalados no Ollama")
        meta_row.addWidget(self.refresh_btn)
        meta_row.addSpacing(8)
        self.auto_dispatch = QtWidgets.QCheckBox("Auto-dispatch (enviar sem revisar)")
        self.auto_dispatch.setChecked(False)
        meta_row.addWidget(self.auto_dispatch)
        pc_layout.addLayout(meta_row)

        # Ollama host row (optional override)
        host_row = QtWidgets.QHBoxLayout()
        host_row.addWidget(QtWidgets.QLabel("Ollama:"))
        self.ollama_host_edit = QtWidgets.QLineEdit(os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434"))
        self.ollama_host_edit.setToolTip("URL do servidor Ollama. Vazio usa OLLAMA_HOST do ambiente.")
        self.ollama_host_edit.setPlaceholderText("http://127.0.0.1:11434")
        host_row.addWidget(self.ollama_host_edit, 1)
        pc_layout.addLayout(host_row)

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
        self.log_view.setObjectName("MCPLogView")
        self.log_view.setStyleSheet(
            "QPlainTextEdit { background:#0b1020; color:#d1d5db; "
            "font-family: 'DejaVu Sans Mono', monospace; font-size:11px; }"
        )
        self.log_view.setMinimumHeight(140)
        self.log_view.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
        pc_layout.addWidget(self.log_view, 1)

        root.addWidget(prompt_card, 1)

    # ----- wiring ----------------------------------------------------------

    def _wire(self) -> None:
        self.toggle_btn.clicked.connect(self._on_toggle)
        self.send_btn.clicked.connect(self._on_send)
        self.clear_btn.clicked.connect(self.log_view.clear)
        self.template_combo.currentTextChanged.connect(self._on_template_chosen)
        self.refresh_btn.clicked.connect(self._on_refresh_models)
        self.ollama_host_edit.editingFinished.connect(self._on_refresh_models)

    # ----- F2: template chooser ------------------------------------------

    def _on_template_chosen(self, name: str) -> None:
        if not name or name == "(escolha um template)":
            return
        tpl = self._template_registry.get(name)
        if tpl is None:
            return
        try:
            rendered = tpl.prompt.format(
                doc_name="Untitled",
                doc_a="DocA",
                doc_b="DocB",
            )
        except Exception:
            rendered = tpl.prompt
        self.prompt_edit.setPlainText(rendered)
        self._append_log(f"[mcp] template '{name}' carregado no prompt.\n")

    # ----- F9: theme switching -------------------------------------------

    def _apply_theme(self, theme: str) -> None:
        """Apply 'light', 'dark', or 'auto' (uses FreeCAD palette if available)."""
        self._theme = theme
        effective = theme
        if theme == "auto":
            try:
                import FreeCADGui

                mw = FreeCADGui.getMainWindow()
                base_color = mw.palette().color(QtGui.QPalette.Window)
                luminance = (
                    0.2126 * base_color.red() + 0.7152 * base_color.green() + 0.0722 * base_color.blue()
                )
                effective = "dark" if luminance < 128 else "light"
            except Exception:
                effective = "light"
        if effective == "dark":
            self._set_dark_theme()
        else:
            self._set_light_theme()

    def _set_light_theme(self) -> None:
        self._status_card.setStyleSheet(
            "QFrame { background:#f3f4f6; border:1px solid #d1d5db; border-radius:6px; }"
        )
        self._prompt_card.setStyleSheet(
            "QFrame { background:#ffffff; border:1px solid #d1d5db; border-radius:6px; }"
        )
        self.log_view.setStyleSheet(
            "QPlainTextEdit { background:#0b1020; color:#d1d5db; "
            "font-family: 'DejaVu Sans Mono', monospace; font-size:11px; }"
        )

    def _set_dark_theme(self) -> None:
        self._status_card.setStyleSheet(
            "QFrame { background:#1f2937; border:1px solid #374151; border-radius:6px; }"
            "QLabel { color:#e5e7eb; }"
        )
        self._prompt_card.setStyleSheet(
            "QFrame { background:#111827; border:1px solid #374151; border-radius:6px; }"
            "QLabel { color:#e5e7eb; }"
            "QLineEdit, QPlainTextEdit, QComboBox { background:#0b1220; color:#e5e7eb; "
            "border:1px solid #374151; border-radius:4px; padding:4px; }"
        )
        self.log_view.setStyleSheet(
            "QPlainTextEdit { background:#000000; color:#a7f3d0; "
            "font-family: 'DejaVu Sans Mono', monospace; font-size:11px; }"
        )

    def cycle_theme(self) -> None:
        """Cycle light → dark → auto → light."""
        order = {"light": "dark", "dark": "auto", "auto": "light"}
        self._apply_theme(order.get(self._theme, "light"))
        self._append_log(f"[mcp] theme → {self._theme}\n")

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

        model = self.model_combo.currentText().strip() or "qwen3.6:27b"
        custom_host = self.ollama_host_edit.text().strip()
        if custom_host:
            os.environ["OLLAMA_HOST"] = custom_host
        else:
            os.environ["OLLAMA_HOST"] = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")

        # Sandbox detection: if we're inside a Flatpak/Snap, the host
        # Python isn't reachable, and spawning it would either fail or
        # produce a confusing ModuleNotFoundError. Instead, fall back to
        # in-process execution via a QThread (the FreeCAD app is already
        # running, the sandbox has the right interpreter, and the user
        # can install mcp+freecad_mcp via flatpak enter / pip).
        if self._is_sandboxed():
            self._append_log(
                "[mcp] sandbox detectado — rodando ollama_bridge in-process.\n"
            )
            self._start_in_process(prompt, model)
            return

        argv, cwd = self._build_dispatch_argv(prompt, model)
        if argv is None:
            return

        pretty = " ".join(shlex.quote(a) for a in argv)
        self._append_log(f"[mcp] $ {pretty}\n")

        proc = QtCore.QProcess(self)
        proc.setProcessChannelMode(QtCore.QProcess.MergedChannels)
        proc.readyReadStandardStandardOutput = proc.readyReadStandardOutput  # noqa
        proc.readyReadStandardOutput.connect(
            lambda: self._append_log(bytes(proc.readAllStandardOutput()).decode("utf-8", "replace"))
        )
        proc.finished.connect(self._on_process_finished)
        proc.errorOccurred.connect(self._on_process_error)
        if cwd:
            proc.setWorkingDirectory(cwd)

        env = self._build_subprocess_env(argv)
        proc.setProcessEnvironment(env)
        self._process = proc
        proc.start(argv[0], argv[1:])

    @staticmethod
    def _is_sandboxed() -> bool:
        """Best-effort detection of running inside Flatpak/Snap/AppImage.

        These environments hide the host filesystem (``/usr/bin/python3``
        doesn't resolve to an executable that can run), so we must use
        the in-process interpreter instead of spawning the host python.
        """
        if os.path.exists("/.flatpak-info"):
            return True
        if os.environ.get("SNAP_NAME"):
            return True
        return bool(os.environ.get("APPIMAGE"))  # noqa: SIM103

    def _start_in_process(self, prompt: str, model: str) -> None:
        """Run the Ollama bridge inside this FreeCAD process via QThread.

        We reuse the already-imported interpreter (which has FreeCAD
        available) and rely on the user having installed ``mcp`` +
        ``freecad_mcp`` in the sandbox Python's ``sys.path``. If they
        haven't, the bridge fails fast with a clear error message
        pointing at the install command.
        """
        try:
            from ._in_process_bridge import run_bridge_in_thread
        except Exception as e:
            self._append_log(
                f"[mcp] bridge in-process não disponível: {e!r}\n"
            )
            return

        try:
            worker = run_bridge_in_thread(
                prompt=prompt,
                model=model,
                on_log=lambda m: self._append_log(m),
                on_done=lambda ans, err: self._on_in_process_done(ans, err),
            )
            self._process = worker  # type: ignore[assignment]
        except Exception as e:
            self._append_log(
                f"[mcp] falha ao iniciar bridge in-process: {e!r}\n"
            )

    def _on_in_process_done(self, answer: str, error: str | None) -> None:
        if error:
            self._append_log(f"[mcp] bridge error: {error}\n")
        if answer:
            self._append_log(f"\n[mcp] resposta:\n{answer}\n")
        self._process = None
        self._refresh_status()

    def _build_dispatch_argv(self, prompt: str, model: str) -> tuple[list[str] | None, str | None]:
        """Return (argv, cwd) ready for ``QProcess.start``.

        Strategy:
        1. Prefer the local checkout venv (if present) so the user
           sees their dev code reflected immediately.
        2. If a repo root is known, prepend ``src/`` to ``PYTHONPATH``
           so the system python can import ``freecad_mcp`` without pip.
        3. Fall back to the system ``python -m freecad_mcp.ollama_bridge``.
        4. Fall back to ``uvx mcp-freecad`` if available.

        The repo root is resolved (in order) from:
        * ``$FREECAD_MCP_REPO_ROOT`` env var
        * ``~/.config/freecad-mcp/repo-root`` plain-text file
          (one absolute path per line; first non-empty, non-comment wins)
        * Auto-detect: walk up from this module looking for ``pyproject.toml``
          (works for dev checkouts, not for Flatpak installs).
        """
        cwd = os.getcwd() if QtWidgets else "."
        repo_root = self._resolve_repo_root()
        venv_py = self._find_venv_python(repo_root)

        # 1) venv python (most reliable: has mcp + freecad_mcp installed)
        if venv_py:
            return [venv_py, "-m", "freecad_mcp.ollama_bridge", prompt, "--model", model], repo_root or cwd

        # 2) repo with src/ layout (use PYTHONPATH so we don't need pip install)
        if repo_root and os.path.isfile(os.path.join(repo_root, "pyproject.toml")):
            src_dir = os.path.join(repo_root, "src")
            if os.path.isdir(src_dir):
                for py in (shutil.which("python3"), shutil.which("python")):
                    if not py:
                        continue
                    env_prefix = f"PYTHONPATH={shlex.quote(src_dir)}"
                    return [
                        "env",
                        env_prefix,
                        py,
                        "-m",
                        "freecad_mcp.ollama_bridge",
                        prompt,
                        "--model",
                        model,
                    ], repo_root

        # 3) system python with the package installed
        for py in (shutil.which("python3"), shutil.which("python")):
            if not py:
                continue
            return [py, "-m", "freecad_mcp.ollama_bridge", prompt, "--model", model], cwd

        # 4) uvx fallback (PyPI install)
        uvx = shutil.which("uvx")
        if uvx:
            return [
                uvx,
                "--from",
                "mcp-freecad",
                "python",
                "-m",
                "freecad_mcp.ollama_bridge",
                prompt,
                "--model",
                model,
            ], cwd

        self._append_log(
            "[mcp] ERRO: não encontrei python nem uvx no PATH. "
            "Defina FREECAD_MCP_VENV=/caminho/do/venv/bin/python ou "
            "FREECAD_MCP_REPO_ROOT=/caminho/do/repo.\n"
        )
        return None, None

    def _find_venv_python(self, repo_root: str | None) -> str | None:
        """Return an absolute path to a Python that has mcp-freecad installed.

        Tries, in order:
        1. ``$FREECAD_MCP_VENV`` env var (any python inside that venv).
        2. ``<repo_root>/.venv/bin/python`` (dev checkout).
        3. ``/tmp/.venv-rpctest/bin/python`` (this repo's CI venv).
        4. ``~/.local/share/uv/tools/mcp-freecad/bin/python`` (uv tool install).
        """
        # Use ``in self.__dict__`` rather than a sentinel compare so we
        # are robust to Qt subclasses that may define ``__getattr__``.
        if "_cached_venv_python" in self.__dict__:
            cached = self.__dict__["_cached_venv_python"]
            return cached or None

        candidates: list[str] = []

        env_venv = os.environ.get(self.VENV_PATH_ENV, "").strip()
        if env_venv:
            if os.path.isfile(env_venv):
                candidates.append(env_venv)
            elif os.path.isdir(env_venv):
                candidates.append(os.path.join(env_venv, "bin", "python"))
                candidates.append(os.path.join(env_venv, "Scripts", "python.exe"))

        if repo_root:
            candidates.append(os.path.join(repo_root, ".venv", "bin", "python"))

        candidates.append("/tmp/.venv-rpctest/bin/python")
        candidates.append(os.path.expanduser("~/.local/share/uv/tools/mcp-freecad/bin/python"))

        for cand in candidates:
            if os.path.isfile(cand):
                self._cached_venv_python = cand
                return cand

        self._cached_venv_python = ""
        return None

    def _resolve_repo_root(self) -> str | None:
        """Return the absolute path to the ``mcp_freecad`` source repo, if known.

        Order of resolution:
        1. ``$FREECAD_MCP_REPO_ROOT`` environment variable.
        2. ``~/.config/freecad-mcp/repo-root`` plain-text file.
        3. Walk up from this module looking for a sibling ``pyproject.toml``.
        """
        # Use ``in self.__dict__`` rather than a sentinel compare so we
        # are robust to Qt subclasses that may define ``__getattr__``.
        if "_cached_repo_root" in self.__dict__:
            cached = self.__dict__["_cached_repo_root"]
            return cached or None

        # 1) env var
        env = os.environ.get("FREECAD_MCP_REPO_ROOT", "").strip()
        if env and os.path.isfile(os.path.join(env, "pyproject.toml")):
            self._cached_repo_root = env
            return env

        # 2) config file (one-line plaintext)
        cfg = os.path.expanduser("~/.config/freecad-mcp/repo-root")
        if os.path.isfile(cfg):
            try:
                with open(cfg, encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue
                        if os.path.isfile(os.path.join(line, "pyproject.toml")):
                            self._cached_repo_root = line
                            return line
            except OSError:
                pass

        # 3) auto-detect (dev checkout: addon sits next to repo root)
        here = os.path.dirname(os.path.abspath(__file__))
        cur = here
        for _ in range(6):
            if os.path.isfile(os.path.join(cur, "pyproject.toml")):
                self._cached_repo_root = cur
                return cur
            parent = os.path.dirname(cur)
            if parent == cur:
                break
            cur = parent

        self._cached_repo_root = ""
        return None

    def _on_process_finished(self, code: int, status: QtCore.QProcess.ExitStatus) -> None:
        self._append_log(f"[mcp] processo terminou (code={code}, status={status}).\n")
        self._process = None
        self._refresh_status()

    def _on_process_error(self, err: QtCore.QProcess.ProcessError) -> None:
        self._append_log(f"[mcp] QProcess erro: {err}\n")
        self._process = None

    # ----- Ollama model picker ----------------------------------------------

    def _build_subprocess_env(self, argv: list[str]) -> QtCore.QProcessEnvironment:
        """Return a QProcessEnvironment tailored to the chosen interpreter.

        In particular, we want the same ``venv/bin/`` directory that
        owns ``argv[0]`` to appear in ``PATH`` so the bridge can find
        the ``mcp-freecad`` console script without the user having to
        install it globally.
        """
        env = QtCore.QProcessEnvironment.systemEnvironment()

        # Inherit / override OLLAMA_HOST from the field.
        host = self.ollama_host_edit.text().strip()
        if host:
            env.insert(self.OLLAMA_HOST_ENV, host)

        # Make sure the venv bin directory is on PATH.
        interp = argv[0] if argv else ""
        if interp and ("/" in interp or os.sep in interp):
            venv_bin = os.path.dirname(interp)
            current_path = env.value("PATH", "") or ""
            if venv_bin and venv_bin not in current_path.split(os.pathsep):
                env.insert("PATH", venv_bin + os.pathsep + current_path)

        # Belt-and-braces: also prepend repo src/ to PYTHONPATH so the
        # fallback path keeps working when the venv python is missing.
        repo_root = self._resolve_repo_root()
        if repo_root:
            src_dir = os.path.join(repo_root, "src")
            if os.path.isdir(src_dir):
                current_pp = env.value("PYTHONPATH", "") or ""
                if src_dir not in current_pp.split(os.pathsep):
                    env.insert("PYTHONPATH", src_dir + os.pathsep + current_pp)

        return env

    def _on_refresh_models(self) -> None:
        """Hit ``/api/tags`` and repopulate the model combobox."""
        host_text = self.ollama_host_edit.text().strip() or None
        if host_text:
            os.environ[self.OLLAMA_HOST_ENV] = host_text
        try:
            result = list_ollama_models(host_text, timeout=3.0)
        except Exception as e:  # pragma: no cover - defensive
            self._append_log(f"[mcp] erro ao listar modelos: {e}\n")
            return
        if not result.ok:
            self._append_log(f"[mcp] Ollama indisponível: {result.error}\n")
            return
        previous = self.model_combo.currentText().strip()
        self.model_combo.blockSignals(True)
        try:
            self.model_combo.clear()
            for model in result.models:
                self.model_combo.addItem(model.display(), userData=model.name)
            if previous:
                idx = self.model_combo.findData(previous)
                if idx >= 0:
                    self.model_combo.setCurrentIndex(idx)
                else:
                    self.model_combo.setEditText(previous)
            self._models_loaded = True
        finally:
            self.model_combo.blockSignals(False)
        self._append_log(f"[mcp] {len(result.models)} modelo(s) carregado(s) de {result.url}.\n")

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
_BTN_START = _BTN_BASE + "QPushButton { background:#16a34a; }"  # green
_BTN_STOP = _BTN_BASE + "QPushButton { background:#dc2626; }"  # red


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


def cycle_theme() -> None:
    """Cycle theme on the singleton panel. No-op if the panel isn't shown yet."""
    panel = _PANEL_SINGLETON
    if panel is not None:
        panel.cycle_theme()


def notify_status_change() -> None:
    """Called by the toolbar toggle when the server state changes."""
    panel = _PANEL_SINGLETON
    if panel is not None:
        panel._refresh_status()


__all__ = ["MCPControlPanel", "get_or_create_panel", "show_panel", "notify_status_change"]
