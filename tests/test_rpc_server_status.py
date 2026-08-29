"""Tests for the new status helpers added in v1.0.4.

Covers:

* ``is_rpc_server_running`` — pure read of the module-level instance.
* ``get_rpc_status`` — snapshot consumed by the toolbar toggle and the
  dock panel; verifies keys/values, the host:port extraction from
  ``server_address``, and graceful failure when the server address is
  malformed.
* ``toggle_rpc_server`` — routes to ``start_rpc_server`` when nothing is
  running and to ``stop_rpc_server`` otherwise.

The test deliberately loads the real ``rpc_server`` module (not a copy)
so it doubles as a smoke test for the new public surface.
"""
from __future__ import annotations

import importlib.util
import sys
import threading
import types
from pathlib import Path

# ----------------------------------------------------------------------
# Shims: FreeCAD / FreeCADGui / ObjectsFem / PySide stubs
# ----------------------------------------------------------------------

_RS_DIR = Path(__file__).resolve().parent.parent / "addon" / "FreeCADMCP" / "rpc_server"

for _name in ("FreeCAD", "FreeCADGui", "ObjectsFem", "PySide"):
    if _name not in sys.modules:
        sys.modules[_name] = types.ModuleType(_name)

_fc = sys.modules["FreeCAD"]
_fc.Console = types.SimpleNamespace(
    PrintWarning=lambda *a, **k: None,
    PrintMessage=lambda *a, **k: None,
    PrintError=lambda *a, **k: None,
)
_fc.getUserAppDataDir = lambda: "/tmp"
_fc.newDocument = lambda *a, **k: None
_fc.getDocument = lambda *a, **k: None
_fc.listDocuments = lambda: {}
_fc.Document = type("Document", (), {})
_fc.DocumentObject = type("DocumentObject", (), {})
_fc.Vector = type("Vector", (), {})
_fc.Rotation = type("Rotation", (), {})
_fc.Placement = type("Placement", (), {})

sys.modules["FreeCADGui"].ActiveDocument = None
sys.modules["FreeCADGui"].Selection = types.SimpleNamespace(
    clearSelection=lambda: None,
    addSelection=lambda *a, **k: None,
)
sys.modules["FreeCADGui"].SendMsgToActiveView = lambda *a, **k: None
sys.modules["FreeCADGui"].addCommand = lambda *a, **k: None
sys.modules["FreeCADGui"].getMainWindow = lambda: types.SimpleNamespace(
    findChildren=lambda *a, **k: [],
    findChild=lambda *a, **k: None,
    addDockWidget=lambda *a, **k: None,
)

_qc = sys.modules["PySide"].QtCore = types.ModuleType("PySide.QtCore")
_qc.QTimer = types.SimpleNamespace(
    singleShot=lambda *a, **k: None,
    NotRunning=0,
)
_qc.QEventLoop = types.SimpleNamespace(AllEvents=0)
_qc.QThread = types.SimpleNamespace(msleep=lambda *a, **k: None)
_qc.Qt = types.SimpleNamespace(
    PointingHandCursor=0,
    LeftDockWidgetArea=0,
    RightDockWidgetArea=0,
    transparent=0,
    AlignVCenter=0,
    End=0,
)

_qg = sys.modules["PySide"].QtGui = types.ModuleType("PySide.QtGui")
_qg.QPixmap = type("QPixmap", (), {})
_qg.QPainter = type("QPainter", (), {})
_qg.QColor = type("QColor", (), {})
_qg.QPen = type("QPen", (), {})
_qg.QTextCursor = type("QTextCursor", (), {"End": 0})

_qw = sys.modules["PySide"].QtWidgets = types.ModuleType("PySide.QtWidgets")
for _n in (
    "QFrame", "QWidget", "QLabel", "QLineEdit", "QPlainTextEdit",
    "QPushButton", "QCheckBox", "QHBoxLayout", "QVBoxLayout",
    "QApplication", "QInputDialog", "QMessageBox", "QAction",
    "QDockWidget", "QTextEdit",
):
    setattr(_qw, _n, type(_n, (), {"Normal": 0}))
_qw.QApplication.instance = staticmethod(lambda: None)
_qw.QApplication.processEvents = lambda *a, **k: None

sys.modules["ObjectsFem"].makeMeshGmsh = lambda *a, **k: (None,)
sys.modules["ObjectsFem"].makeAnalysis = lambda *a, **k: None
sys.modules["ObjectsFem"].makeMaterialSolid = lambda *a, **k: None
sys.modules["ObjectsFem"].makeSolverCalculiXCcxTools = lambda *a, **k: None


# ----------------------------------------------------------------------
# Load rpc_server under a synthetic package with all submodules wired
# ----------------------------------------------------------------------

_pkg = types.ModuleType("_rs_pkg_status")
_pkg.__path__ = [str(_RS_DIR)]
sys.modules["_rs_pkg_status"] = _pkg

_SUBMODS = (
    "parts_library",
    "serialize",
    "_fem_workdir",
    "_request_tracking",
    "_ip_allowlist",
    "_settings",
    "_dispatch",
    "_screenshot",
    "_security_gate",
    "_commands",
    "_panel",
    "mesh_to_solid",
    "step_metadata",
    "bom",
    "fem_post_process",
    "inspection",
    "multi_instance",
    "api_introspect",
    "job_runner",
    "cam_ops",
)
for _sub in _SUBMODS:
    _spec = importlib.util.spec_from_file_location(
        f"_rs_pkg_status.{_sub}", str(_RS_DIR / f"{_sub}.py")
    )
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules[f"_rs_pkg_status.{_sub}"] = _mod
    _spec.loader.exec_module(_mod)  # type: ignore[union-attr]

_spec = importlib.util.spec_from_file_location(
    "_rs_pkg_status.rpc_server", str(_RS_DIR / "rpc_server.py")
)
rpc_server = importlib.util.module_from_spec(_spec)
sys.modules["_rs_pkg_status.rpc_server"] = rpc_server
_spec.loader.exec_module(rpc_server)  # type: ignore[union-attr]


# ----------------------------------------------------------------------
# Minimal stand-in for the XML-RPC server (mirrors test_rpc_server_lifecycle)
# ----------------------------------------------------------------------


class _FakeServer:
    instances: list = []

    def __init__(self, addr, allowed_ips_str="127.0.0.1", **kwargs):
        self.addr = addr
        self.server_address = addr  # what get_rpc_status() reads
        self.allowed_ips_str = allowed_ips_str
        self.kwargs = kwargs
        self.shutdown_called = 0
        self.server_close_called = 0
        self._release = threading.Event()
        # Open a real listening socket so is_rpc_server_running's
        # liveness probe can succeed when the fake is the active
        # server. The kernel assigns a free port when addr[1] == 0.
        import socket as _socket
        self._socket = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        self._socket.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        self._socket.bind(addr)
        self._socket.listen(1)
        # Re-read the bound address in case the test asked for port 0.
        self.server_address = self._socket.getsockname()
        _FakeServer.instances.append(self)

    def register_instance(self, instance):
        pass

    def serve_forever(self):
        # Block until released so the thread stays "alive" for the test.
        self._release.wait(timeout=5)

    def shutdown(self):
        self.shutdown_called += 1
        self._release.set()
        # Close socket here too so stop_rpc_server() actually frees the
        # port — otherwise the next start_rpc_server hits EADDRINUSE.
        import contextlib
        with contextlib.suppress(Exception):
            self._socket.close()

    def server_close(self):
        self.server_close_called += 1
        import contextlib
        with contextlib.suppress(Exception):
            self._socket.close()


rpc_server.FilteredXMLRPCServer = _FakeServer


def _reset():
    rpc_server.rpc_server_instance = None
    rpc_server.rpc_server_thread = None
    # Close any sockets that fake servers may still be holding so
    # we don't leak ports between tests.
    import contextlib
    for inst in list(_FakeServer.instances):
        with contextlib.suppress(Exception):
            inst._socket.close()
    _FakeServer.instances.clear()
    # Force settings back to a known baseline.
    from _rs_pkg_status._settings import save_settings

    save_settings(
        {
            "auto_start_rpc": False,
            "remote_enabled": False,
            "allowed_ips": "127.0.0.1",
        }
    )


# ----------------------------------------------------------------------
# is_rpc_server_running
# ----------------------------------------------------------------------


def test_is_rpc_server_running_returns_false_when_no_instance():
    _reset()
    assert rpc_server.is_rpc_server_running() is False


def test_is_rpc_server_running_returns_false_when_socket_dead():
    """Regression: the previous implementation only checked that the
    module-level ``rpc_server_instance`` reference was set. After a
    previous FreeCAD session shut down without the stop helper being
    called, the reference stayed alive but the socket was closed — so
    every tool call failed with "Failed to connect to FreeCAD" while
    the dock panel reported "Running". The liveness probe must now
    detect that and return False (clearing the stale reference so the
    next click restarts cleanly).
    """
    _reset()
    # Allocate + close a port so the kernel definitely has nothing
    # listening on it. Then point rpc_server_instance at a fake server
    # bound to that same port — ``is_rpc_server_running`` must refuse
    # it because its socket is closed.
    import socket as _socket
    holder = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))
    port = holder.getsockname()[1]
    holder.close()
    server = _FakeServer(("127.0.0.1", port))
    # The fake opened a new socket on the same port (after the kernel
    # freed it). Close it to simulate "socket dead".
    server._socket.close()
    rpc_server.rpc_server_instance = server
    try:
        assert rpc_server.is_rpc_server_running() is False
        # The stale reference should have been cleared.
        assert rpc_server.rpc_server_instance is None
        # And the fake server's ``server_close`` should have been called
        # once (the liveness probe tries to clean up before giving up).
        assert server.server_close_called == 1
    finally:
        rpc_server.rpc_server_instance = None


def test_is_rpc_server_running_returns_true_when_instance_alive():
    """When the underlying socket is open, liveness passes."""
    _reset()
    server = _FakeServer(("127.0.0.1", 0))  # kernel picks a free port
    rpc_server.rpc_server_instance = server
    try:
        assert rpc_server.is_rpc_server_running() is True
        # Reference is preserved when alive.
        assert rpc_server.rpc_server_instance is server
        assert server.server_close_called == 0
    finally:
        rpc_server.rpc_server_instance = None
        server._socket.close()


def test_is_rpc_server_running_returns_false_when_no_server_address():
    """If the instance has no usable server_address, we treat it as
    not running (cannot probe a port we don't know)."""
    _reset()

    class _NoAddr:
        server_address = None

    rpc_server.rpc_server_instance = _NoAddr()
    try:
        assert rpc_server.is_rpc_server_running() is False
    finally:
        rpc_server.rpc_server_instance = None


def test_is_rpc_server_running_returns_false_when_instance_set():
    """Deprecated: kept as a sanity check that a bare object() sentinel
    no longer fools the helper (it lacks server_address, so it is
    treated as not running)."""
    _reset()
    sentinel = object()
    rpc_server.rpc_server_instance = sentinel
    try:
        assert rpc_server.is_rpc_server_running() is False
    finally:
        rpc_server.rpc_server_instance = None


# ----------------------------------------------------------------------
# get_rpc_status
# ----------------------------------------------------------------------


def test_get_rpc_status_shape_when_stopped():
    _reset()
    st = rpc_server.get_rpc_status()
    assert isinstance(st, dict)
    # All keys are present.
    assert set(st.keys()) == {
        "running",
        "host",
        "port",
        "remote_enabled",
        "allowed_ips",
        "auto_start",
        "pid",
    }
    assert st["running"] is False
    assert st["host"] is None
    assert st["port"] is None
    assert st["pid"] is None
    assert st["remote_enabled"] is False
    assert st["allowed_ips"] == "127.0.0.1"
    assert st["auto_start"] is False


def test_get_rpc_status_when_running_extracts_host_and_port():
    _reset()
    rpc_server.rpc_server_instance = _FakeServer(("127.0.0.1", 9875))
    rpc_server.rpc_server_thread = types.SimpleNamespace(ident=4242)
    try:
        st = rpc_server.get_rpc_status()
        assert st["running"] is True
        assert st["host"] == "127.0.0.1"
        assert st["port"] == 9875
        assert st["pid"] == 4242
    finally:
        rpc_server.rpc_server_instance = None
        rpc_server.rpc_server_thread = None


def test_get_rpc_status_reflects_remote_enabled_from_settings():
    _reset()
    from _rs_pkg_status._settings import save_settings

    save_settings({"remote_enabled": True, "allowed_ips": "10.0.0.0/8"})
    try:
        st = rpc_server.get_rpc_status()
        assert st["remote_enabled"] is True
        assert st["allowed_ips"] == "10.0.0.0/8"
    finally:
        save_settings(
            {"remote_enabled": False, "allowed_ips": "127.0.0.1"}
        )


def test_get_rpc_status_swallows_malformed_server_address():
    """If server_address raises, ``is_rpc_server_running`` should treat
    it as not running (cannot probe a port we don't know how to read).
    ``get_rpc_status`` then surfaces ``running=False`` and leaves the
    host/port fields empty rather than crashing.
    """
    _reset()

    class Weird:
        @property
        def server_address(self):
            raise RuntimeError("boom")

    rpc_server.rpc_server_instance = Weird()
    try:
        st = rpc_server.get_rpc_status()
        assert st["running"] is False
        assert st["host"] is None
        assert st["port"] is None
        # The stale reference should have been cleared by the probe.
        assert rpc_server.rpc_server_instance is None
    finally:
        rpc_server.rpc_server_instance = None


def test_get_rpc_status_no_pid_when_thread_is_none():
    _reset()
    rpc_server.rpc_server_instance = _FakeServer(("localhost", 9999))
    rpc_server.rpc_server_thread = None
    try:
        st = rpc_server.get_rpc_status()
        assert st["pid"] is None
        assert st["port"] == 9999
    finally:
        rpc_server.rpc_server_instance = None


# ----------------------------------------------------------------------
# toggle_rpc_server
# ----------------------------------------------------------------------


def _wait_for_thread_alive(timeout: float = 2.0) -> None:
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        if rpc_server.rpc_server_thread is not None:
            return
        time.sleep(0.02)


def test_toggle_starts_when_stopped():
    _reset()
    msg = rpc_server.toggle_rpc_server(port=9875)
    _wait_for_thread_alive()
    assert "started" in msg.lower()
    assert rpc_server.is_rpc_server_running() is True
    assert len(_FakeServer.instances) == 1
    # Cleanup so subsequent tests do not see a live thread.
    rpc_server.stop_rpc_server()


def test_toggle_stops_when_running():
    _reset()
    rpc_server.start_rpc_server(port=9875)
    _wait_for_thread_alive()
    assert rpc_server.is_rpc_server_running() is True

    msg = rpc_server.toggle_rpc_server()
    assert "stopped" in msg.lower()
    assert rpc_server.is_rpc_server_running() is False


def test_toggle_is_idempotent_after_stop():
    _reset()
    rpc_server.start_rpc_server(port=9875)
    _wait_for_thread_alive()
    rpc_server.toggle_rpc_server()  # → stop
    # Second toggle should start again.
    msg = rpc_server.toggle_rpc_server(port=9875)
    _wait_for_thread_alive()
    assert "started" in msg.lower()
    assert rpc_server.is_rpc_server_running() is True
    rpc_server.stop_rpc_server()


# ----------------------------------------------------------------------
# Sanity: _panel module exposes the public API used by InitGui.py
# ----------------------------------------------------------------------


def test_panel_module_public_api():
    panel = sys.modules["_rs_pkg_status._panel"]
    assert callable(getattr(panel, "MCPControlPanel", None))
    assert callable(getattr(panel, "get_or_create_panel", None))
    assert callable(getattr(panel, "show_panel", None))
    assert callable(getattr(panel, "notify_status_change", None))


def test_commands_module_exposes_toggle_class():
    cmds = sys.modules["_rs_pkg_status._commands"]
    cls = getattr(cmds, "ToggleRPCServerCommand", None)
    assert cls is not None
    resources = cls().GetResources()
    assert "MenuText" in resources
    assert "ToolTip" in resources
    assert cls().IsActive() is True


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
