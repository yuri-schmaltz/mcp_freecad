"""Tests for ``_dispatch.py`` (GUI-thread queue + lifecycle helpers).

v1.0.3 coverage push — this module had 36 % coverage; we exercise
every helper and the dispatcher's exception paths.
"""
import importlib.util
import sys
import types
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_RS_DIR = _HERE.parent / "addon" / "FreeCADMCP" / "rpc_server"

# Shims for FreeCAD / PySide / ObjectsFem so the module imports.
for name in ("FreeCAD", "FreeCADGui", "ObjectsFem", "PySide"):
    if name not in sys.modules:
        sys.modules[name] = types.ModuleType(name)

# Set up minimal Qt stubs that record calls.
_qt_core = sys.modules["PySide"].QtCore = types.SimpleNamespace(
    QTimer=types.SimpleNamespace(singleShot=lambda *a, **k: None),
    QEventLoop=types.SimpleNamespace(AllEvents=0),
    QThread=types.SimpleNamespace(msleep=lambda *a, **k: None),
)
_qt_widgets = sys.modules["PySide"].QtWidgets = types.SimpleNamespace(
    QApplication=type("QApplication", (), {
        "instance": staticmethod(lambda: None),
        "processEvents": lambda *a, **k: None,
    }),
)


# Build a synthetic package so ``from . import`` resolves.
pkg = types.ModuleType("_test_dispatch_pkg")
pkg.__path__ = [str(_RS_DIR)]
sys.modules["_test_dispatch_pkg"] = pkg

spec = importlib.util.spec_from_file_location(
    "_test_dispatch_pkg._dispatch", str(_RS_DIR / "_dispatch.py")
)
dispatch = importlib.util.module_from_spec(spec)
sys.modules["_test_dispatch_pkg._dispatch"] = dispatch
spec.loader.exec_module(dispatch)  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# _flush_gui_events
# ---------------------------------------------------------------------------

def test_flush_gui_events_no_freecadgui_is_noop():
    """When FreeCADGui is None (imported outside FreeCAD) the helper
    must return immediately without touching Qt."""
    saved = dispatch.FreeCADGui
    try:
        dispatch.FreeCADGui = None
        # Should not raise even though Qt is "imported".
        dispatch._flush_gui_events(delay_ms=10)
    finally:
        dispatch.FreeCADGui = saved


def test_flush_gui_events_no_qt_is_noop():
    """When Qt is None (stubbed out) updateGui is called but no
    processEvents / msleep happens."""
    saved_fc = dispatch.FreeCADGui
    saved_qt_widgets = dispatch.QtWidgets
    saved_qt_core = dispatch.QtCore
    calls: list[str] = []
    dispatch.FreeCADGui = types.SimpleNamespace(updateGui=lambda: calls.append("updateGui"))
    dispatch.QtWidgets = None
    dispatch.QtCore = None
    try:
        dispatch._flush_gui_events()
        assert calls == ["updateGui"]
    finally:
        dispatch.FreeCADGui = saved_fc
        dispatch.QtWidgets = saved_qt_widgets
        dispatch.QtCore = saved_qt_core


def test_flush_gui_events_no_app_instance():
    """QApplication.instance() returns None -> we skip processEvents."""
    saved_fc = dispatch.FreeCADGui
    saved_widgets = dispatch.QtWidgets
    saved_core = dispatch.QtCore
    calls: list[str] = []
    dispatch.FreeCADGui = types.SimpleNamespace(updateGui=lambda: calls.append("updateGui"))
    dispatch.QtWidgets = types.SimpleNamespace(
        QApplication=type("QApplication", (), {
            "instance": staticmethod(lambda: None),
            "processEvents": lambda *a, **k: calls.append(("processEvents", a)),
        }),
    )
    dispatch.QtCore = _qt_core
    try:
        dispatch._flush_gui_events()
        assert "updateGui" in calls
        # No processEvents because instance() was None.
        assert not any(c[0] == "processEvents" for c in calls if isinstance(c, tuple))
    finally:
        dispatch.FreeCADGui = saved_fc
        dispatch.QtWidgets = saved_widgets
        dispatch.QtCore = saved_core


def test_flush_gui_events_with_app():
    """QApplication.instance() returns an object -> processEvents is called."""
    saved_fc = dispatch.FreeCADGui
    saved_widgets = dispatch.QtWidgets
    saved_core = dispatch.QtCore
    calls: list[str] = []
    dispatch.FreeCADGui = types.SimpleNamespace(updateGui=lambda: calls.append("updateGui"))
    fake_app = types.SimpleNamespace(
        processEvents=lambda *a, **k: calls.append(("processEvents", a)),
    )
    dispatch.QtWidgets = types.SimpleNamespace(
        QApplication=type("QApplication", (), {
            "instance": staticmethod(lambda: fake_app),
        }),
    )
    dispatch.QtCore = _qt_core
    try:
        dispatch._flush_gui_events(delay_ms=0)
        # No msleep because delay_ms == 0.
        assert ("processEvents", (0, 0)) in calls
    finally:
        dispatch.FreeCADGui = saved_fc
        dispatch.QtWidgets = saved_widgets
        dispatch.QtCore = saved_core


def test_flush_gui_events_delay_triggers_msleep():
    """delay_ms > 0 triggers a QThread.msleep call."""
    saved_fc = dispatch.FreeCADGui
    saved_widgets = dispatch.QtWidgets
    saved_core = dispatch.QtCore
    dispatch.FreeCADGui = types.SimpleNamespace(updateGui=lambda: None)
    fake_app = types.SimpleNamespace(processEvents=lambda *a, **k: None)
    msleep_calls: list[int] = []
    dispatch.QtWidgets = types.SimpleNamespace(
        QApplication=type("QApplication", (), {
            "instance": staticmethod(lambda: fake_app),
        }),
    )
    dispatch.QtCore = types.SimpleNamespace(
        QTimer=types.SimpleNamespace(singleShot=lambda *a, **k: None),
        QEventLoop=types.SimpleNamespace(AllEvents=0),
        QThread=types.SimpleNamespace(msleep=lambda ms: msleep_calls.append(ms)),
    )
    try:
        dispatch._flush_gui_events(delay_ms=25)
        assert 25 in msleep_calls
    finally:
        dispatch.FreeCADGui = saved_fc
        dispatch.QtWidgets = saved_widgets
        dispatch.QtCore = saved_core


# ---------------------------------------------------------------------------
# _get_view_size
# ---------------------------------------------------------------------------

def test_get_view_size_tuple():
    class _V:
        def getSize(self):
            return (640, 480)
    w, h = dispatch._get_view_size(_V())
    assert (w, h) == (640, 480)


def test_get_view_size_list():
    class _V:
        def getSize(self):
            return [800, 600]
    w, h = dispatch._get_view_size(_V())
    assert (w, h) == (800, 600)


def test_get_view_size_object_with_width_height():
    class _V:
        def getSize(self):
            class _S:
                def width(self):  return 320
                def height(self): return 240
            return _S()
    w, h = dispatch._get_view_size(_V())
    assert (w, h) == (320, 240)


def test_get_view_size_zero_clamped_to_one():
    class _V:
        def getSize(self):
            return (0, 0)
    w, h = dispatch._get_view_size(_V())
    assert (w, h) == (1, 1)


def test_get_view_size_exception_returns_default():
    class _V:
        def getSize(self):
            raise RuntimeError("boom")
    w, h = dispatch._get_view_size(_V())
    assert (w, h) == (1024, 768)


# ---------------------------------------------------------------------------
# _resolve_screenshot_size
# ---------------------------------------------------------------------------

def test_resolve_size_uses_request_when_provided():
    class _V:
        def getSize(self):
            return (640, 480)
    w, h = dispatch._resolve_screenshot_size(_V(), 1920, 1080)
    assert (w, h) == (1920, 1080)


def test_resolve_size_falls_back_to_view():
    class _V:
        def getSize(self):
            return (640, 480)
    w, h = dispatch._resolve_screenshot_size(_V(), None, None)
    assert (w, h) == (640, 480)


def test_resolve_size_partial_override():
    class _V:
        def getSize(self):
            return (640, 480)
    w, h = dispatch._resolve_screenshot_size(_V(), None, 200)
    assert (w, h) == (640, 200)
    w, h = dispatch._resolve_screenshot_size(_V(), 100, None)
    assert (w, h) == (100, 480)


def test_resolve_size_clamps_to_min_one():
    class _V:
        def getSize(self):
            return (640, 480)
    w, h = dispatch._resolve_screenshot_size(_V(), -10, -20)
    assert (w, h) == (1, 1)


# ---------------------------------------------------------------------------
# process_gui_tasks
# ---------------------------------------------------------------------------

def test_process_gui_tasks_runs_task_and_puts_result():
    """Happy path: a task is run, its result is put on the response queue."""
    dispatch.rpc_request_queue.put(lambda: {"ok": True})
    dispatch.rpc_response_queue.queue.clear()
    # Save and disable reschedule by removing QtCore.
    saved_qt = dispatch.QtCore
    dispatch.QtCore = None
    try:
        dispatch.process_gui_tasks()
        # The queue should have one item.
        assert dispatch.rpc_response_queue.qsize() == 1
        assert dispatch.rpc_response_queue.get_nowait() == {"ok": True}
    finally:
        dispatch.QtCore = saved_qt


def test_process_gui_tasks_returns_none_string():
    """If a task returns None, we surface 'GUI handler returned None'."""
    dispatch.rpc_request_queue.put(lambda: None)
    dispatch.rpc_response_queue.queue.clear()
    saved_qt = dispatch.QtCore
    dispatch.QtCore = None
    try:
        dispatch.process_gui_tasks()
        assert dispatch.rpc_response_queue.get_nowait() == "GUI handler returned None"
    finally:
        dispatch.QtCore = saved_qt


def test_process_gui_tasks_catches_task_exception():
    """A task that raises surfaces the error string."""
    def boom():
        raise ValueError("task kaboom")
    dispatch.rpc_request_queue.put(boom)
    dispatch.rpc_response_queue.queue.clear()
    saved_qt = dispatch.QtCore
    saved_fc = dispatch.FreeCAD
    fc_stub = types.SimpleNamespace(Console=types.SimpleNamespace(PrintError=lambda *a, **k: None))
    dispatch.FreeCAD = fc_stub
    dispatch.QtCore = None
    try:
        dispatch.process_gui_tasks()
        msg = dispatch.rpc_response_queue.get_nowait()
        assert "ValueError" in msg
        assert "task kaboom" in msg
    finally:
        dispatch.QtCore = saved_qt
        dispatch.FreeCAD = saved_fc


def test_process_gui_tasks_shutdown_sentinel_exits():
    """When the queue contains _DISPATCH_SHUTDOWN we exit cleanly."""
    dispatch.rpc_request_queue.put(dispatch._DISPATCH_SHUTDOWN)
    dispatch.rpc_response_queue.queue.clear()
    saved_qt = dispatch.QtCore
    dispatch.QtCore = None
    try:
        dispatch.process_gui_tasks()
        # No items added to response queue because we exited.
        assert dispatch.rpc_response_queue.qsize() == 0
    finally:
        dispatch.QtCore = saved_qt


def test_process_gui_tasks_reschedules_via_qtimer():
    """If QtCore is present, the dispatcher reschedules itself."""
    calls: list[tuple] = []
    saved_qt = dispatch.QtCore
    dispatch.QtCore = types.SimpleNamespace(
        QTimer=types.SimpleNamespace(singleShot=lambda ms, fn: calls.append((ms, fn))),
    )
    try:
        dispatch.process_gui_tasks()
        # Rescheduled with 500 ms.
        assert any(ms == 500 for ms, _ in calls)
    finally:
        dispatch.QtCore = saved_qt


def test_process_gui_tasks_qtimer_failure_survives():
    """If QtCore.QTimer.singleShot raises, the dispatcher logs and
    does not propagate."""
    saved_qt = dispatch.QtCore
    saved_fc = dispatch.FreeCAD
    fc_stub = types.SimpleNamespace(Console=types.SimpleNamespace(PrintError=lambda *a, **k: None))
    dispatch.FreeCAD = fc_stub
    dispatch.QtCore = types.SimpleNamespace(
        QTimer=types.SimpleNamespace(singleShot=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("qt boom"))),
    )
    try:
        # Should not raise.
        dispatch.process_gui_tasks()
    finally:
        dispatch.QtCore = saved_qt
        dispatch.FreeCAD = saved_fc


def test_process_gui_tasks_outer_exception_is_swallowed():
    """If something OUTSIDE the per-task try raises, we still reschedule."""

    class _BrokenQueue:
        def empty(self):
            raise RuntimeError("queue boom")
        def get(self):
            raise RuntimeError("queue boom")

    saved_q = dispatch.rpc_request_queue
    saved_qt = dispatch.QtCore
    saved_fc = dispatch.FreeCAD
    fc_stub = types.SimpleNamespace(Console=types.SimpleNamespace(PrintError=lambda *a, **k: None))
    dispatch.FreeCAD = fc_stub
    dispatch.QtCore = None
    dispatch.rpc_request_queue = _BrokenQueue()
    try:
        # Should not raise even though queue.empty() raised.
        dispatch.process_gui_tasks()
    finally:
        dispatch.rpc_request_queue = saved_q
        dispatch.QtCore = saved_qt
        dispatch.FreeCAD = saved_fc


# ---------------------------------------------------------------------------
# shutdown sentinel identity
# ---------------------------------------------------------------------------

def test_dispatch_shutdown_sentinel_is_singleton():
    """The sentinel must be a unique object so identity comparisons
    work even after reloads."""
    assert dispatch._DISPATCH_SHUTDOWN is dispatch._DISPATCH_SHUTDOWN
