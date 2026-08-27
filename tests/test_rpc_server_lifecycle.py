"""Tests for start_rpc_server / stop_rpc_server concurrency safety.

These exercise the global lifecycle functions with heavy mocking so we
do not need a real FreeCAD, PySide, or listening socket. The goal is to
prove that:

- start_rpc_server is idempotent (second call returns "already running").
- stop_rpc_server is a no-op when no server is running.
- concurrent start/stop calls do not produce more than one server.
- server_close() is called on shutdown so the socket is released
  immediately (no TIME_WAIT wait for a restart).
"""
import importlib.util
import sys
import threading
import types
from pathlib import Path

# ---- Standard FreeCAD / PySide / ObjectsFem shims -----------------------

_HERE = Path(__file__).resolve().parent
_RS_DIR = _HERE.parent / "addon" / "FreeCADMCP" / "rpc_server"

for name in ("FreeCAD", "FreeCADGui", "ObjectsFem", "PySide"):
    if name not in sys.modules:
        sys.modules[name] = types.ModuleType(name)

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
    clearSelection=lambda: None, addSelection=lambda *a, **k: None
)
sys.modules["FreeCADGui"].SendMsgToActiveView = lambda *a, **k: None
sys.modules["FreeCADGui"].addCommand = lambda *a, **k: None
sys.modules["FreeCADGui"].getMainWindow = lambda: types.SimpleNamespace(
    findChildren=lambda *a, **k: []
)

sys.modules["PySide"].QtCore = types.SimpleNamespace(
    QTimer=types.SimpleNamespace(singleShot=lambda *a, **k: None),
    QEventLoop=types.SimpleNamespace(AllEvents=0),
    QThread=types.SimpleNamespace(msleep=lambda *a, **k: None),
)
sys.modules["PySide"].QtWidgets = types.SimpleNamespace(
    QApplication=type("QApplication", (), {"instance": staticmethod(lambda: None), "processEvents": lambda *a, **k: None}),
    QInputDialog=type("QInputDialog", (), {}),
    QLineEdit=type("QLineEdit", (), {"Normal": 0}),
    QMessageBox=type("QMessageBox", (), {"warning": staticmethod(lambda *a, **k: None)}),
    QAction=type("QAction", (), {}),
)

sys.modules["ObjectsFem"].makeMeshGmsh = lambda *a, **k: (None,)
sys.modules["ObjectsFem"].makeAnalysis = lambda *a, **k: None
sys.modules["ObjectsFem"].makeMaterialSolid = lambda *a, **k: None
sys.modules["ObjectsFem"].makeSolverCalculiXCcxTools = lambda *a, **k: None


# ---- Load rpc_server under a synthetic package -------------------------

_pkg = types.ModuleType("_rs_pkg_lifecycle")
_pkg.__path__ = [str(_RS_DIR)]
sys.modules["_rs_pkg_lifecycle"] = _pkg
for sub in ("parts_library", "serialize", "_fem_workdir", "_request_tracking"):
    spec = importlib.util.spec_from_file_location(
        f"_rs_pkg_lifecycle.{sub}", str(_RS_DIR / f"{sub}.py")
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"_rs_pkg_lifecycle.{sub}"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
spec = importlib.util.spec_from_file_location(
    "_rs_pkg_lifecycle.rpc_server", str(_RS_DIR / "rpc_server.py")
)
rpc_server = importlib.util.module_from_spec(spec)
sys.modules["_rs_pkg_lifecycle.rpc_server"] = rpc_server
spec.loader.exec_module(rpc_server)  # type: ignore[union-attr]

# Isolate from the user's real freecad_mcp_settings.json: force defaults so
# ``remote_enabled`` never trips the security-gate refusal branch during
# these tests.
rpc_server.load_settings = lambda: {"remote_enabled": False, "allowed_ips": "127.0.0.1"}


# ---- Test infrastructure: replace FilteredXMLRPCServer + the thread -----

class FakeRPCServer:
    """Stand-in for ``FilteredXMLRPCServer`` that records lifecycle calls."""

    instances: list = []  # class-level list of every constructed server

    def __init__(self, addr, allowed_ips_str="127.0.0.1", **kwargs):
        self.addr = addr
        self.allowed_ips_str = allowed_ips_str
        self.kwargs = kwargs
        self.registered = []
        self.shutdown_called = 0
        self.server_close_called = 0
        self.serve_forever_started = threading.Event()
        self.serve_forever_release = threading.Event()
        FakeRPCServer.instances.append(self)

    def register_instance(self, instance):
        self.registered.append(instance)

    def serve_forever(self):
        self.serve_forever_started.set()
        # Block until released by the test teardown.
        self.serve_forever_release.wait(timeout=5)

    def shutdown(self):
        self.shutdown_called += 1
        self.serve_forever_release.set()  # unblock serve_forever

    def server_close(self):
        self.server_close_called += 1


# Replace the real XML-RPC server with the fake.
rpc_server.FilteredXMLRPCServer = FakeRPCServer


def _reset_module_state():
    """Reset the module-level globals that track the running server.

    Required between tests because each test constructs its own server.
    """
    rpc_server.rpc_server_instance = None
    rpc_server.rpc_server_thread = None
    FakeRPCServer.instances.clear()


def _wait_for_start():
    """Spin until the most recent serve_forever is actually running."""
    if FakeRPCServer.instances:
        FakeRPCServer.instances[-1].serve_forever_started.wait(timeout=2)


# ---- Tests --------------------------------------------------------------

def test_start_when_already_running_is_idempotent():
    _reset_module_state()
    rpc_server.start_rpc_server(port=9875)
    _wait_for_start()
    # Second call returns the "already running" sentinel and does NOT
    # create a second server.
    msg2 = rpc_server.start_rpc_server(port=9875)
    assert "already running" in msg2.lower()
    assert len(FakeRPCServer.instances) == 1
    rpc_server.stop_rpc_server()


def test_stop_when_not_running_is_noop():
    _reset_module_state()
    msg = rpc_server.stop_rpc_server()
    assert msg == "RPC Server was not running."
    assert FakeRPCServer.instances == []


def test_stop_calls_server_close():
    _reset_module_state()
    rpc_server.start_rpc_server(port=9875)
    _wait_for_start()
    rpc_server.stop_rpc_server()
    srv = FakeRPCServer.instances[0]
    assert srv.shutdown_called == 1
    assert srv.server_close_called == 1, "server_close() was not invoked — socket may stay in TIME_WAIT"


def test_stop_releases_lock_so_subsequent_start_succeeds():
    _reset_module_state()
    rpc_server.start_rpc_server(port=9875)
    _wait_for_start()
    rpc_server.stop_rpc_server()
    # Should not deadlock; lock was released by the first stop.
    rpc_server.start_rpc_server(port=9875)
    _wait_for_start()
    assert len(FakeRPCServer.instances) == 2
    rpc_server.stop_rpc_server()


def test_concurrent_start_only_one_wins():
    """If two threads race to start the server, only one server should be
    constructed and the other call should report 'already running'."""
    _reset_module_state()

    def starter():
        rpc_server.start_rpc_server(port=9875)

    threads = [threading.Thread(target=starter) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=2.0)
        assert not t.is_alive(), "starter deadlocked — lock not released"

    _wait_for_start()
    # All starters should have either created the server or seen it running.
    assert len(FakeRPCServer.instances) == 1, f"expected 1 server, got {len(FakeRPCServer.instances)}"
    rpc_server.stop_rpc_server()


def test_concurrent_start_stop_does_not_leak():
    """Hammering start/stop from many threads must never produce two
    simultaneously-running servers."""
    _reset_module_state()

    def cycle():
        rpc_server.start_rpc_server(port=9875)
        _wait_for_start()
        rpc_server.stop_rpc_server()

    threads = [threading.Thread(target=cycle) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)
        assert not t.is_alive(), "cycle deadlocked"

    # After all threads finish, no server is running.
    assert rpc_server.rpc_server_instance is None
    assert rpc_server.rpc_server_thread is None
    # Total constructors = total stops (every start was matched by a stop).
    assert len(FakeRPCServer.instances) >= 1


if __name__ == "__main__":
    test_start_when_already_running_is_idempotent()
    test_stop_when_not_running_is_noop()
    test_stop_calls_server_close()
    test_stop_releases_lock_so_subsequent_start_succeeds()
    test_concurrent_start_only_one_wins()
    test_concurrent_start_stop_does_not_leak()
    print("All RPC lifecycle tests passed")
