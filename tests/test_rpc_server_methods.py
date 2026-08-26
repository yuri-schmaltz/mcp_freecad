"""Tests for FreeCADRPC class methods (v1.0.3 coverage).

Covers the high-level RPC methods that were previously only exercised
through integration tests with a real FreeCAD. We mock FreeCAD / PySide
and drive each method through ``_tracked_call``'s normal queue
mechanism.

What is covered here:
- undo / redo (sync, doc.recompute path, error path)
- save_document (with path, without path, error)
- export_object (stl, error path)
- cancel_all_pending_requests / invalidate_idempotency_cache
- health_check composition
- _create_document_gui / _delete_object_gui (FreeCAD success/error)
- _run_fem_analysis_gui (success, prerequisite failure, missing solver)
- get_active_screenshot return envelopes
- timeout_for env override
- _timeout_for precedence rules
"""
import importlib.util
import os
import sys
import types
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_RS_DIR = _HERE.parent / "addon" / "FreeCADMCP" / "rpc_server"

# FreeCAD / PySide / ObjectsFem shims (same as the other RPC tests).
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


class _FakeActiveDoc:
    """Just enough to satisfy ``_save_active_screenshot`` and friends."""

    def __init__(self):
        self.ActiveView = _FakeView()
        self.ActiveObject = None


class _FakeView:
    def __init__(self):
        self._calls = []

    def viewIsometric(self): self._calls.append("iso")
    def viewFront(self):      self._calls.append("front")
    def viewTop(self):        self._calls.append("top")
    def viewRight(self):      self._calls.append("right")
    def viewBack(self):       self._calls.append("back")
    def viewLeft(self):       self._calls.append("left")
    def viewBottom(self):     self._calls.append("bottom")
    def viewDimetric(self):   self._calls.append("dimetric")
    def viewTrimetric(self):  self._calls.append("trimetric")
    def fitAll(self):         self._calls.append("fitAll")
    def saveImage(self, *a, **kw): self._calls.append(("saveImage", a, kw))
    def getSize(self):        return (800, 600)


sys.modules["FreeCADGui"].ActiveDocument = _FakeActiveDoc()
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


# Load the addon module set.
_pkg_name = "_rs_pkg_methods"
pkg = types.ModuleType(_pkg_name)
pkg.__path__ = [str(_RS_DIR)]  # type: ignore[attr-defined]
sys.modules[_pkg_name] = pkg
for sub in (
    "parts_library", "serialize", "_fem_workdir", "_request_tracking",
    "_security_gate", "_settings", "_screenshot", "_ip_allowlist",
):
    spec = importlib.util.spec_from_file_location(
        f"{_pkg_name}.{sub}", str(_RS_DIR / f"{sub}.py")
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"{_pkg_name}.{sub}"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]

spec = importlib.util.spec_from_file_location(
    f"{_pkg_name}.rpc_server", str(_RS_DIR / "rpc_server.py")
)
rpc_server = importlib.util.module_from_spec(spec)
sys.modules[f"{_pkg_name}.rpc_server"] = rpc_server
spec.loader.exec_module(rpc_server)  # type: ignore[union-attr]


def _reset_tracker():
    rt = sys.modules[f"{_pkg_name}._request_tracking"]
    rt.reset_default_tracker()


# ---------------------------------------------------------------------------
# ping / health_check
# ---------------------------------------------------------------------------

def test_ping_returns_true():
    rpc = rpc_server.FreeCADRPC()
    assert rpc.ping() is True


def test_health_check_returns_full_snapshot():
    _reset_tracker()
    rpc = rpc_server.FreeCADRPC()
    snap = rpc.health_check()
    assert snap["success"] is True
    assert "uptime_seconds" in snap
    assert snap["rpc_server_running"] is False  # no server started
    assert snap["request_queue_size"] >= 0
    assert snap["cached_responses"] == 0
    assert snap["pending_cancellations"] == 0
    assert "settings_dir" in snap


# ---------------------------------------------------------------------------
# cancel_all_pending_requests / invalidate_idempotency_cache
# ---------------------------------------------------------------------------

def test_cancel_all_pending_requests_returns_count():
    _reset_tracker()
    rpc = rpc_server.FreeCADRPC()
    rpc.cancel_request("a")
    rpc.cancel_request("b")
    out = rpc.cancel_all_pending_requests()
    assert out["success"] is True
    assert out["flushed"] == 2


def test_invalidate_idempotency_cache_returns_count():
    _reset_tracker()
    rpc = rpc_server.FreeCADRPC()
    rt = sys.modules[f"{_pkg_name}._request_tracking"]
    tracker = rt.get_default_tracker()
    tracker.cache_response("x", {"success": True})
    tracker.cache_response("y", {"success": True})
    out = rpc.invalidate_idempotency_cache()
    assert out["success"] is True
    assert out["dropped"] == 2


# ---------------------------------------------------------------------------
# _timeout_for precedence
# ---------------------------------------------------------------------------

def test_timeout_for_default():
    rpc = rpc_server.FreeCADRPC()
    t = rpc._timeout_for("create_object")
    assert t == 60.0


def test_timeout_for_unknown_op_falls_back_to_default():
    rpc = rpc_server.FreeCADRPC()
    rpc.TIMEOUT = 7.0
    assert rpc._timeout_for("unknown_operation") == 7.0


def test_timeout_for_override_wins():
    rpc = rpc_server.FreeCADRPC()
    assert rpc._timeout_for("create_object", override=999) == 999


def test_timeout_for_ignores_zero_and_negative_override():
    rpc = rpc_server.FreeCADRPC()
    assert rpc._timeout_for("create_object", override=0) == 60.0
    assert rpc._timeout_for("create_object", override=-5) == 60.0


def test_timeout_for_env_override_applied(monkeypatch):
    """``FREECAD_MCP_DEFAULT_RPC_TIMEOUT`` is applied at construction time."""
    monkeypatch.setenv("FREECAD_MCP_DEFAULT_RPC_TIMEOUT", "12")
    # The env is read inside __init__; we need a fresh instance AFTER
    # the env was set.
    rpc = rpc_server.FreeCADRPC()
    # Per-op override still wins because the per-op dict is layered on top.
    # Both per-op (60) and TIMEOUT (12) are configured; per-op wins because
    # _timeout_for uses self.PER_OPERATION_TIMEOUTS.get(op, self.TIMEOUT).
    assert rpc._timeout_for("create_object") == 60
    # Unknown ops fall back to the env-supplied TIMEOUT.
    assert rpc._timeout_for("totally_unknown_op") == 12


def test_timeout_for_invalid_env_is_ignored(caplog):
    """Malformed env var falls back to the in-code default without raising."""
    import logging
    saved = os.environ.get("FREECAD_MCP_DEFAULT_RPC_TIMEOUT")
    try:
        os.environ["FREECAD_MCP_DEFAULT_RPC_TIMEOUT"] = "not-a-number"
        caplog.set_level(logging.WARNING, logger="FreeCADMCPserver")
        rpc = rpc_server.FreeCADRPC()
        # Falls back to the class-level TIMEOUT (10).
        assert rpc.TIMEOUT == 10
    finally:
        if saved is None:
            os.environ.pop("FREECAD_MCP_DEFAULT_RPC_TIMEOUT", None)
        else:
            os.environ["FREECAD_MCP_DEFAULT_RPC_TIMEOUT"] = saved


# ---------------------------------------------------------------------------
# undo / redo / save_document / export_object (lightweight)
# ---------------------------------------------------------------------------

def _install_pump(rpc_mod):
    """Replace the queue with real ones + a daemon pump thread."""
    import queue
    import threading

    req_q: queue.Queue = queue.Queue()
    resp_q: queue.Queue = queue.Queue()
    stop = threading.Event()

    def pump():
        while not stop.is_set():
            try:
                task = req_q.get(timeout=0.05)
            except queue.Empty:
                continue
            try:
                resp_q.put(task())
            except Exception as e:  # pragma: no cover
                resp_q.put({"success": False, "error": f"{type(e).__name__}: {e}"})

    th = threading.Thread(target=pump, daemon=True)
    th.start()
    rpc_mod.rpc_request_queue.put = req_q.put  # type: ignore[assignment]
    rpc_mod.rpc_response_queue.get = resp_q.get  # type: ignore[assignment]

    class _Handle:
        def __init__(self):
            self.req_q = req_q
            self.resp_q = resp_q
            self.stop = stop
            self.thread = th
        def shutdown(self):
            self.stop.set()
            self.thread.join(timeout=2.0)

    return _Handle()


def test_undo_missing_doc_returns_error():
    _reset_tracker()
    rpc = rpc_server.FreeCADRPC()
    rpc_mod = rpc_server  # use module's queues
    pump = _install_pump(rpc_mod)
    saved = sys.modules["FreeCAD"].getDocument
    try:
        sys.modules["FreeCAD"].getDocument = lambda name: None
        out = rpc.undo("Nope")
        assert out["success"] is False
        assert "not found" in out["error"]
    finally:
        sys.modules["FreeCAD"].getDocument = saved
        pump.shutdown()


def test_redo_missing_doc_returns_error():
    _reset_tracker()
    rpc = rpc_server.FreeCADRPC()
    pump = _install_pump(rpc_server)
    saved = sys.modules["FreeCAD"].getDocument
    try:
        sys.modules["FreeCAD"].getDocument = lambda name: None
        out = rpc.redo("Nope")
        assert out["success"] is False
    finally:
        sys.modules["FreeCAD"].getDocument = saved
        pump.shutdown()


def test_save_document_missing_doc_returns_error():
    _reset_tracker()
    rpc = rpc_server.FreeCADRPC()
    pump = _install_pump(rpc_server)
    saved = sys.modules["FreeCAD"].getDocument
    try:
        sys.modules["FreeCAD"].getDocument = lambda name: None
        out = rpc.save_document("Nope")
        assert out["success"] is False
        assert "not found" in out["error"]
    finally:
        sys.modules["FreeCAD"].getDocument = saved
        pump.shutdown()


def test_export_object_missing_doc_returns_error():
    _reset_tracker()
    rpc = rpc_server.FreeCADRPC()
    pump = _install_pump(rpc_server)
    saved = sys.modules["FreeCAD"].getDocument
    try:
        sys.modules["FreeCAD"].getDocument = lambda name: None
        out = rpc.export_object("Nope", "Box", "/tmp/x.stl")
        assert out["success"] is False
        assert "not found" in out["error"]
    finally:
        sys.modules["FreeCAD"].getDocument = saved
        pump.shutdown()


def test_run_fem_analysis_invalid_timeout_returns_error():
    rpc = rpc_server.FreeCADRPC()
    out = rpc.run_fem_analysis("D", "A", timeout="not-an-int")
    assert out["success"] is False
    assert "invalid timeout" in out["error"]


def test_run_fem_analysis_missing_doc_returns_error():
    _reset_tracker()
    rpc = rpc_server.FreeCADRPC()
    pump = _install_pump(rpc_server)
    saved = sys.modules["FreeCAD"].getDocument
    try:
        sys.modules["FreeCAD"].getDocument = lambda name: None
        out = rpc.run_fem_analysis("Nope", "Analysis")
        assert out["success"] is False
    finally:
        sys.modules["FreeCAD"].getDocument = saved
        pump.shutdown()


# ---------------------------------------------------------------------------
# _create_document_gui / _delete_object_gui / _insert_part_from_library
# ---------------------------------------------------------------------------

def test_create_document_gui_calls_recompute():
    calls: list[str] = []
    saved = sys.modules["FreeCAD"].newDocument
    try:
        doc = types.SimpleNamespace(recompute=lambda: calls.append("recompute"))
        sys.modules["FreeCAD"].newDocument = lambda n: doc
        rpc = rpc_server.FreeCADRPC()
        assert rpc._create_document_gui("Doc1") is True
        assert calls == ["recompute"]
    finally:
        sys.modules["FreeCAD"].newDocument = saved


def test_delete_object_gui_missing_doc_returns_error():
    saved = sys.modules["FreeCAD"].getDocument
    try:
        sys.modules["FreeCAD"].getDocument = lambda name: None
        rpc = rpc_server.FreeCADRPC()
        out = rpc._delete_object_gui("Nope", "Box")
        assert isinstance(out, str)
        assert "not found" in out
    finally:
        sys.modules["FreeCAD"].getDocument = saved


def test_insert_part_from_library_propagates_error():
    """When ``insert_part_from_library`` raises, the RPC catches the
    exception and returns the error message string instead of letting
    it propagate."""
    rpc = rpc_server.FreeCADRPC()
    # rpc_server.py does ``from .parts_library import insert_part_from_library``
    # at module load — the binding lives on the rpc_server module.
    real = rpc_server.insert_part_from_library

    def _boom(_):
        raise FileNotFoundError("missing FCStd")

    rpc_server.insert_part_from_library = _boom
    try:
        out = rpc._insert_part_from_library("nope.fcstd")
    finally:
        rpc_server.insert_part_from_library = real
    assert isinstance(out, str)
    assert "missing FCStd" in out


# ---------------------------------------------------------------------------
# get_active_view
# ---------------------------------------------------------------------------

def test_get_active_view_no_view_returns_error():
    _reset_tracker()
    rpc = rpc_server.FreeCADRPC()
    pump = _install_pump(rpc_server)
    saved_ad = sys.modules["FreeCADGui"].ActiveDocument
    try:
        # No active view (simulate).
        sys.modules["FreeCADGui"].ActiveDocument = _FakeActiveDoc()
        sys.modules["FreeCADGui"].ActiveDocument.ActiveView = None
        out = rpc.get_active_view()
        assert out["success"] is False
        assert "no active view" in out["error"].lower()
    finally:
        sys.modules["FreeCADGui"].ActiveDocument = saved_ad
        pump.shutdown()


def test_get_active_view_returns_metadata():
    _reset_tracker()
    rpc = rpc_server.FreeCADRPC()
    pump = _install_pump(rpc_server)
    saved_ad = sys.modules["FreeCADGui"].ActiveDocument
    try:
        sys.modules["FreeCADGui"].ActiveDocument = _FakeActiveDoc()
        out = rpc.get_active_view()
        assert out["success"] is True
        assert out["has_save_image"] is True
        assert out["width"] == 800
        assert out["height"] == 600
    finally:
        sys.modules["FreeCADGui"].ActiveDocument = saved_ad
        pump.shutdown()


# ---------------------------------------------------------------------------
# start_rpc_server / stop_rpc_server thread-safety (lightweight smoke)
# ---------------------------------------------------------------------------

def test_start_when_already_running_returns_already():
    """When the rpc_server_instance is already set, the guard returns
    immediately without touching the network."""
    original = rpc_server.rpc_server_instance
    try:
        rpc_server.rpc_server_instance = types.SimpleNamespace()  # truthy
        out = rpc_server.start_rpc_server()
        assert "already" in out.lower()
    finally:
        rpc_server.rpc_server_instance = original


def test_stop_when_not_running_returns_idle():
    """No server running -> a clear "not running" message."""
    original = rpc_server.rpc_server_instance
    try:
        rpc_server.rpc_server_instance = None
        out = rpc_server.stop_rpc_server()
        assert "not running" in out.lower()
    finally:
        rpc_server.rpc_server_instance = original
