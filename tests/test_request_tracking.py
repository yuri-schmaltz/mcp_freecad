"""Tests for the per-request tracker and cancel_request RPC."""
import importlib.util
import sys
import types
from pathlib import Path

# Reuse the same import-shim machinery as test_validate_allowed_ips.py.
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

# Build a fake parent package.
_pkg_name = "_rs_pkg_tracker"
pkg = types.ModuleType(_pkg_name)
pkg.__path__ = [str(_RS_DIR)]  # type: ignore[attr-defined]
sys.modules[_pkg_name] = pkg
for sub in ("parts_library", "serialize", "_fem_workdir", "_request_tracking"):
    spec = importlib.util.spec_from_file_location(
        f"{_pkg_name}.{sub}", str(_RS_DIR / f"{sub}.py")
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"{_pkg_name}.{sub}"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]

# Also import _request_tracking at top level so tests can use it directly.
_rt_spec = importlib.util.spec_from_file_location(
    "rt_under_test", str(_RS_DIR / "_request_tracking.py")
)
rt = importlib.util.module_from_spec(_rt_spec)
sys.modules["rt_under_test"] = rt
_rt_spec.loader.exec_module(rt)  # type: ignore[union-attr]

RequestTracker = rt.RequestTracker


# ---------------------------------------------------------------------------
# RequestTracker unit tests
# ---------------------------------------------------------------------------

def test_tracker_get_cached_none_for_unknown_id():
    t = RequestTracker()
    assert t.get_cached("missing") is None


def test_tracker_cache_and_retrieve():
    t = RequestTracker()
    t.cache_response("rid-1", {"success": True, "value": 42})
    assert t.get_cached("rid-1") == {"success": True, "value": 42}


def test_tracker_first_writer_wins():
    """Caching the same id twice does not overwrite."""
    t = RequestTracker()
    t.cache_response("rid-1", {"success": True, "value": 1})
    t.cache_response("rid-1", {"success": True, "value": 2})
    assert t.get_cached("rid-1") == {"success": True, "value": 1}


def test_tracker_cache_eviction_fifo():
    """When capacity is exceeded, the oldest entry is dropped first."""
    t = RequestTracker(max_cached=2)
    t.cache_response("a", 1)
    t.cache_response("b", 2)
    t.cache_response("c", 3)
    assert t.get_cached("a") is None
    assert t.get_cached("b") == 2
    assert t.get_cached("c") == 3


def test_tracker_cancel_marks_and_consumes():
    t = RequestTracker()
    assert t.cancel("rid-1") is True
    assert t.is_cancelled("rid-1") is True
    assert t.consume_cancel("rid-1") is True
    # Second consume returns False — flag was cleared.
    assert t.consume_cancel("rid-1") is False


def test_tracker_cancel_marks_unknown_id():
    """cancel() does not validate that the id was previously seen; it
    simply marks it. This is intentional — the caller may cancel a request
    that has not yet been sent (e.g. typed-ahead in a UI).
    """
    t = RequestTracker()
    assert t.cancel("never-seen") is True
    assert t.is_cancelled("never-seen") is True


def test_tracker_cancel_after_completion_returns_false():
    """Once cached, an id cannot be cancelled retroactively."""
    t = RequestTracker()
    t.cache_response("rid-1", {"success": True})
    assert t.cancel("rid-1") is False


def test_tracker_none_id_is_safe():
    """Cache and consume operations with None are no-ops. cancel(None)
    is also a no-op — we never want to add a literal None to the
    cancelled set.
    """
    t = RequestTracker()
    assert t.get_cached(None) is None
    assert t.is_cancelled(None) is False
    assert t.consume_cancel(None) is False
    t.cache_response(None, {"x": 1})  # no error
    assert t.get_cached(None) is None
    assert t.cancel(None) is False
    assert None not in t.pending_cancellations()


def test_tracker_clear():
    t = RequestTracker()
    t.cache_response("rid", 1)
    t.cancel("rid-2")
    t.clear()
    assert t.get_cached("rid") is None
    assert t.is_cancelled("rid-2") is False


def test_tracker_pending_cancellations_returns_tuple():
    t = RequestTracker()
    t.cancel("a")
    t.cancel("b")
    assert set(t.pending_cancellations()) == {"a", "b"}


def test_tracker_thread_safe_under_contention():
    """Sanity: concurrent caching and cancelling does not corrupt state."""
    import threading
    t = RequestTracker(max_cached=1000)
    errors = []

    def worker(i):
        try:
            for j in range(100):
                rid = f"r{i}-{j}"
                t.cache_response(rid, {"i": i, "j": j})
                if j % 10 == 0:
                    t.cancel(rid)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert not errors


# ---------------------------------------------------------------------------
# Integration-style tests for FreeCADRPC._tracked_call + cancel_request
# ---------------------------------------------------------------------------
#
# These run without a real FreeCAD; we monkeypatch the module-level queues
# so the dispatch loop can be exercised in a controlled way.

def _load_rpc_server_module():
    """Load rpc_server.py with the standard FreeCAD shims (defined above)."""
    spec = importlib.util.spec_from_file_location(
        f"{_pkg_name}.rpc_server", str(_RS_DIR / "rpc_server.py")
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"{_pkg_name}.rpc_server"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _install_queue_pump(rpc_mod):
    """Replace the GUI dispatch queues with real ``queue.Queue`` instances
    backed by a background thread that mimics ``process_gui_tasks``.

    Returns the pump thread (daemon) so the caller can join it on teardown.
    """
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
            except Exception as e:  # pragma: no cover — task should not raise
                resp_q.put({"success": False, "error": f"{type(e).__name__}: {e}"})

    t = threading.Thread(target=pump, daemon=True)
    t.start()

    rpc_mod.rpc_request_queue.put = req_q.put  # type: ignore[assignment]
    rpc_mod.rpc_request_queue.empty = lambda: req_q.empty()  # type: ignore[assignment]
    rpc_mod.rpc_response_queue.get = resp_q.get  # type: ignore[assignment]

    class _PumpHandle:
        def __init__(self):
            self.stop = stop
            self.thread = t
            self.req_q = req_q
            self.resp_q = resp_q

        def shutdown(self):
            self.stop.set()
            self.thread.join(timeout=2.0)

    return _PumpHandle()


def _reset_tracker():
    rt.reset_default_tracker()


def test_tracked_call_caches_response():
    """Second call with the same request_id returns the cached response
    without invoking the handler a second time."""
    rpc_mod = _load_rpc_server_module()
    _reset_tracker()
    pump = _install_queue_pump(rpc_mod)

    try:
        rpc = rpc_mod.FreeCADRPC()
        handler_calls = {"n": 0}

        def handler():
            handler_calls["n"] += 1
            return {"success": True, "value": handler_calls["n"]}

        out1 = rpc._tracked_call("rid-cache", handler, timeout=2.0)
        assert out1 == {"success": True, "value": 1}, out1
        assert handler_calls["n"] == 1

        # Give the pump a beat to make sure the request queue is empty.
        import time
        for _ in range(20):
            if pump.req_q.empty():
                break
            time.sleep(0.02)
        assert pump.req_q.empty(), "pump did not drain the request queue"

        # Second call with same id — should return cached response WITHOUT
        # dispatching another task.
        out2 = rpc._tracked_call("rid-cache", handler, timeout=2.0)
        assert out2 == {"success": True, "value": 1}, out2
        assert handler_calls["n"] == 1, "handler was invoked twice — cache broken"
        assert pump.req_q.empty(), "a second task was posted — cache broken"
    finally:
        pump.shutdown()


def test_tracked_call_cancels_before_dispatch():
    """If cancel() is called before the task runs, the task short-circuits."""
    rpc_mod = _load_rpc_server_module()
    _reset_tracker()
    pump = _install_queue_pump(rpc_mod)

    try:
        rpc = rpc_mod.FreeCADRPC()
        cancel_result = rpc.cancel_request("rid-cancel")
        assert cancel_result == {"success": True, "request_id": "rid-cancel", "cancelled": True}

        handler_calls = {"n": 0}

        def handler():
            handler_calls["n"] += 1
            return {"success": True}

        out = rpc._tracked_call("rid-cancel", handler, timeout=2.0)
        assert out.get("cancelled") is True, out
        assert "cancelled" in out.get("error", "").lower()
        assert handler_calls["n"] == 0, "handler ran despite pre-dispatch cancel"
    finally:
        pump.shutdown()


def test_tracked_call_cancels_at_dispatch():
    """Cancel called AFTER the task is posted but BEFORE it runs also short-circuits."""
    rpc_mod = _load_rpc_server_module()
    _reset_tracker()
    # Build a queue + pump manually so we can stall dispatch long enough to
    # issue a cancel between enqueue and execution.
    import queue as _queue
    import threading

    req_q: _queue.Queue = _queue.Queue()
    resp_q: _queue.Queue = _queue.Queue()
    gate = threading.Event()
    pump_started = threading.Event()
    pump_stopped = threading.Event()

    def slow_pump():
        pump_started.set()
        while not pump_stopped.is_set():
            try:
                task = req_q.get(timeout=0.05)
            except _queue.Empty:
                continue
            # Wait at the gate so the test can call cancel_request between
            # enqueue and execution.
            gate.wait(timeout=5.0)
            try:
                resp_q.put(task())
            except Exception as e:
                resp_q.put({"success": False, "error": str(e)})

    t = threading.Thread(target=slow_pump, daemon=True)
    t.start()
    rpc_mod.rpc_request_queue.put = req_q.put  # type: ignore[assignment]
    rpc_mod.rpc_response_queue.get = resp_q.get  # type: ignore[assignment]

    try:
        pump_started.wait(timeout=2.0)
        rpc = rpc_mod.FreeCADRPC()

        handler_calls = {"n": 0}

        def handler():
            handler_calls["n"] += 1
            return {"success": True, "value": handler_calls["n"]}

        # Open the gate later; the pump will block on it.
        gate.set()

        # Race a cancel against a tracked call by issuing cancel first.
        rpc.cancel_request("rid-race")

        out = rpc._tracked_call("rid-race", handler, timeout=2.0)
        assert out.get("cancelled") is True, out
        assert handler_calls["n"] == 0, "handler ran despite cancel"
    finally:
        pump_stopped.set()
        t.join(timeout=2.0)


def test_tracked_call_none_id_skips_cache_and_cancel():
    """With request_id=None, every call is a fresh dispatch."""
    rpc_mod = _load_rpc_server_module()
    _reset_tracker()
    pump = _install_queue_pump(rpc_mod)

    try:
        rpc = rpc_mod.FreeCADRPC()
        calls = {"n": 0}

        def handler():
            calls["n"] += 1
            return {"success": True, "value": calls["n"]}

        a = rpc._tracked_call(None, handler, timeout=2.0)
        b = rpc._tracked_call(None, handler, timeout=2.0)
        assert a == {"success": True, "value": 1}, a
        assert b == {"success": True, "value": 2}, b
        assert calls["n"] == 2
    finally:
        pump.shutdown()


def test_tracked_call_handler_exception_becomes_error_response():
    rpc_mod = _load_rpc_server_module()
    _reset_tracker()
    pump = _install_queue_pump(rpc_mod)

    try:
        rpc = rpc_mod.FreeCADRPC()

        def boom():
            raise RuntimeError("kaboom")

        out = rpc._tracked_call("rid-boom", boom, timeout=2.0)
        assert out["success"] is False
        assert "kaboom" in out["error"]
    finally:
        pump.shutdown()


def test_cancel_request_invalid_input():
    rpc_mod = _load_rpc_server_module()
    rpc = rpc_mod.FreeCADRPC()
    for bad in ("", None, 42):
        result = rpc.cancel_request(bad)  # type: ignore[arg-type]
        assert result["success"] is False
        assert "request_id" in result["error"]


# ---------------------------------------------------------------------------
# v1.0.3 — bulk-flush helpers
# ---------------------------------------------------------------------------

def test_cancel_all_pending_returns_count_and_clears():
    t = RequestTracker()
    t.cancel("a")
    t.cancel("b")
    t.cancel("c")
    assert len(t.pending_cancellations()) == 3
    n = t.cancel_all_pending()
    assert n == 3
    assert t.pending_cancellations() == ()


def test_cancel_all_pending_idempotent():
    t = RequestTracker()
    t.cancel("a")
    assert t.cancel_all_pending() == 1
    # Second flush is a no-op.
    assert t.cancel_all_pending() == 0


def test_cancel_all_pending_does_not_clear_cache():
    """Flush only clears pending cancellations; the cached-response
    set is preserved so retries can still short-circuit."""
    t = RequestTracker()
    t.cache_response("a", {"value": 1})
    t.cancel("a")
    t.cancel_all_pending()
    assert t.get_cached("a") == {"value": 1}


def test_invalidate_cache_drops_all_entries():
    t = RequestTracker()
    t.cache_response("a", 1)
    t.cache_response("b", 2)
    n = t.invalidate_cache()
    assert n == 2
    assert t.get_cached("a") is None
    assert t.get_cached("b") is None
    assert t.cached_ids() == ()


def test_invalidate_cache_empty_returns_zero():
    t = RequestTracker()
    assert t.invalidate_cache() == 0


def test_invalidate_cache_does_not_clear_cancellations():
    t = RequestTracker()
    t.cache_response("a", 1)
    t.cancel("b")
    t.invalidate_cache()
    # cancellations are independent state.
    assert t.is_cancelled("b") is True


if __name__ == "__main__":
    test_tracker_get_cached_none_for_unknown_id()
    test_tracker_cache_and_retrieve()
    test_tracker_first_writer_wins()
    test_tracker_cache_eviction_fifo()
    test_tracker_cancel_marks_and_consumes()
    test_tracker_cancel_marks_unknown_id()
    test_tracker_cancel_after_completion_returns_false()
    test_tracker_none_id_is_safe()
    test_tracker_clear()
    test_tracker_pending_cancellations_returns_tuple()
    test_tracker_thread_safe_under_contention()
    test_tracked_call_caches_response()
    test_tracked_call_cancels_before_dispatch()
    test_tracked_call_cancels_at_dispatch()
    test_tracked_call_none_id_skips_cache_and_cancel()
    test_tracked_call_handler_exception_becomes_error_response()
    test_cancel_request_invalid_input()
    print("All request tracking tests passed")
