"""Tests for addon/FreeCADMCP/rpc_server/_panel.py dispatch resolver.

We can't import the panel module directly (it requires PySide6 +
FreeCAD), so we re-implement the same logic against fake Qt modules and
exercise the same algorithm via the real source file using ``ast`` to
extract the pure-Python pieces, OR we monkeypatch the heavy imports.

The simplest robust path is: import the module with ``PySide`` and
``FreeCAD`` stubbed, then drive ``_resolve_repo_root`` and
``_build_dispatch_argv`` directly.
"""

from __future__ import annotations

import os
import sys
import types
import tempfile
import unittest
from unittest import mock


def _install_pyside_stub() -> None:
    """Provide the minimum PySide surface that ``_panel`` imports."""
    mod_ps = types.ModuleType("PySide")
    mod_ps_core = types.ModuleType("PySide.QtCore")
    mod_ps_gui = types.ModuleType("PySide.QtGui")
    mod_ps_wid = types.ModuleType("PySide.QtWidgets")
    sys.modules.setdefault("PySide", mod_ps)
    sys.modules.setdefault("PySide.QtCore", mod_ps_core)
    sys.modules.setdefault("PySide.QtGui", mod_ps_gui)
    sys.modules.setdefault("PySide.QtWidgets", mod_ps_wid)
    # Classes referenced at import time
    mod_ps_wid.QFrame = type("QFrame", (), {})
    mod_ps_wid.QLabel = type("QLabel", (), {})
    mod_ps_wid.QPlainTextEdit = type("QPlainTextEdit", (), {})
    mod_ps_wid.QComboBox = type("QComboBox", (), {})
    mod_ps_wid.QPushButton = type("QPushButton", (), {})
    mod_ps_wid.QVBoxLayout = type("QVBoxLayout", (), {})
    mod_ps_wid.QHBoxLayout = type("QHBoxLayout", (), {})
    mod_ps_wid.QSizePolicy = type("QSizePolicy", (), {})
    mod_ps_wid.QWidget = type("QWidget", (), {})
    mod_ps_wid.QApplication = type("QApplication", (), {})
    mod_ps_core.QProcess = type("QProcess", (), {})
    mod_ps_core.MergedChannels = 0
    mod_ps_gui.QFont = type("QFont", (), {})


def _import_panel():
    """Import the panel module without needing FreeCAD/PySide installed.

    ``FreeCADMCP`` is not a real package (no ``__init__.py`` at the root),
    so we use ``importlib.util.spec_from_file_location`` to load the
    sibling modules directly and stitch them together under a synthetic
    ``FreeCADMCP.rpc_server`` package on ``sys.path``.
    """
    import importlib.util

    rpc_server_dir = os.path.realpath(
        os.path.join(os.path.dirname(__file__), "..", "addon", "FreeCADMCP", "rpc_server")
    )

    # Build a synthetic FreeCADMCP.rpc_server package
    pkg = types.ModuleType("FreeCADMCP")
    pkg.__path__ = []  # mark as package
    pkg_rpc = types.ModuleType("FreeCADMCP.rpc_server")
    pkg_rpc.__path__ = [rpc_server_dir]
    sys.modules["FreeCADMCP"] = pkg
    sys.modules["FreeCADMCP.rpc_server"] = pkg_rpc

    # Stub the rpc_server sibling so the panel's relative import works
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

    panel_path = os.path.join(rpc_server_dir, "_panel.py")
    spec = importlib.util.spec_from_file_location("FreeCADMCP.rpc_server._panel", panel_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _TestPanel(unittest.TestCase):
    def setUp(self) -> None:
        _install_pyside_stub()
        self._mod = _import_panel()
        self.cls = self._mod.MCPControlPanel

    def _new(self):
        return self.cls.__new__(self.cls)


class ResolveRepoRootTests(_TestPanel):
    def test_env_var_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pyproject = os.path.join(tmp, "pyproject.toml")
            open(pyproject, "w").close()
            with mock.patch.dict(os.environ, {"FREECAD_MCP_REPO_ROOT": tmp}):
                p = self._new()
                self.assertEqual(p._resolve_repo_root(), tmp)

    def test_env_var_with_invalid_path_falls_through(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # No pyproject.toml under tmp
            with mock.patch.dict(os.environ, {"FREECAD_MCP_REPO_ROOT": tmp}):
                p = self._new()
                # Falls through; we don't assert on the return — just no crash.
                p._resolve_repo_root()

    def test_config_file_used_when_env_missing(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            pyproject = os.path.join(repo, "pyproject.toml")
            open(pyproject, "w").close()
            with tempfile.TemporaryDirectory() as cfgdir:
                cfg_path = os.path.join(cfgdir, "repo-root")
                with open(cfg_path, "w", encoding="utf-8") as fh:
                    fh.write(f"# comment line\n\n{repo}\n")
                # Ensure env var doesn't shadow; redirect expanduser to our cfg.
                env_no_repo = {k: v for k, v in os.environ.items() if k not in {"FREECAD_MCP_REPO_ROOT"}}
                with mock.patch.dict(os.environ, env_no_repo, clear=True):
                    with mock.patch(
                        "os.path.expanduser",
                        lambda x: cfg_path if x.startswith("~/.config/freecad-mcp") else x,
                    ):
                        p = self._new()
                        result = p._resolve_repo_root()
                        self.assertEqual(result, repo)


class BuildDispatchArgvTests(_TestPanel):
    def test_returns_none_with_clear_error_when_nothing_available(self) -> None:
        # Force every fallback to fail by clearing PATH-like helpers.
        with mock.patch("shutil.which", return_value=None):
            with mock.patch.object(self.cls, "_resolve_repo_root", return_value=None):
                p = self._new()
                # Patch _append_log to swallow noise.
                p._append_log = lambda msg: None  # type: ignore[assignment]
                argv, cwd = p._build_dispatch_argv("hello", "qwen3:27b")
                self.assertIsNone(argv)
                self.assertIsNone(cwd)

    def test_uses_venv_python_when_repo_root_has_dotvenv(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            # Create .venv/bin/python directly inside the repo dir.
            venv_bin = os.path.join(repo, ".venv", "bin")
            os.makedirs(venv_bin, exist_ok=True)
            py = os.path.join(venv_bin, "python")
            open(py, "w").close()
            open(os.path.join(repo, "pyproject.toml"), "w").close()
            with mock.patch.object(self.cls, "_resolve_repo_root", return_value=repo):
                p = self._new()
                argv, cwd = p._build_dispatch_argv("hi", "m")
                self.assertIsNotNone(argv)
                self.assertEqual(argv[0], py)
                self.assertEqual(argv[1:], ["-m", "freecad_mcp.ollama_bridge", "hi", "--model", "m"])
                self.assertEqual(cwd, repo)


if __name__ == "__main__":
    unittest.main()
