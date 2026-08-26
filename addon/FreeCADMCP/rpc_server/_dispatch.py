"""GUI-thread dispatch and RPC server lifecycle.

Extracted from ``rpc_server`` so the queue / timer / lifecycle logic
can be unit-tested without the FreeCAD ``FreeCADRPC`` class (which
itself imports FreeCAD at module load).

The contract is:

* Requests submitted via :func:`submit` are queued on
  ``rpc_request_queue``; the next 500ms ``QTimer`` tick
  (:func:`process_gui_tasks`) drains the queue on the GUI thread.
* Responses are placed on ``rpc_response_queue`` and consumed by the
  Python-side RPC handler.
* :data:`_DISPATCH_SHUTDOWN` is a sentinel that lets
  :func:`stop_rpc_server` exit the dispatch loop without leaving an
  orphan QTimer chain.
"""
from __future__ import annotations

import queue
import threading
import time
import traceback
from typing import Any

try:
    from PySide import QtCore, QtWidgets
except Exception:
    # Module loaded outside FreeCAD during unit tests; the test
    # harness injects stubs as needed.
    QtCore = None  # type: ignore[assignment]
    QtWidgets = None  # type: ignore[assignment]

try:
    import FreeCAD
    import FreeCADGui
except Exception:
    FreeCAD = None  # type: ignore[assignment]
    FreeCADGui = None  # type: ignore[assignment]


# Sentinel posted on rpc_request_queue by stop_rpc_server() so the next
# process_gui_tasks tick exits cleanly without rescheduling itself.
_DISPATCH_SHUTDOWN = object()

# GUI task queue
rpc_request_queue: queue.Queue = queue.Queue()
rpc_response_queue: queue.Queue = queue.Queue()

# Captured once when the module loads; health_check uses it to report uptime.
_SERVER_START_TIME = time.time()

# Lifecycle state (set/cleared by start_rpc_server / stop_rpc_server).
rpc_server_thread: threading.Thread | None = None
rpc_server_instance: Any = None

# Lock serialising start/stop. Without it, two callers (e.g. the
# auto-start QTimer firing concurrently with a manual menu click) can
# both observe ``rpc_server_instance is None``, build two servers, and
# leak one of them.
_rpc_lock = threading.RLock()


def _flush_gui_events(delay_ms: int = 50) -> None:
    """Process pending Qt events and sleep briefly to let the GUI redraw.

    Used by screenshot capture to ensure the view has actually rendered
    before ``saveImage`` is called.

    Headless safety: when ``QApplication.instance()`` is *not* the
    current thread (e.g. running under ``flatpak run --command=python3``
    where there is no GUI event loop at all), we fall back to a plain
    ``time.sleep`` so the screenshot still has time to render on the
    FreeCAD console-side before ``saveImage`` is called.
    """
    if FreeCADGui is None:
        if delay_ms > 0:
            time.sleep(delay_ms / 1000.0)
        return
    try:
        FreeCADGui.updateGui()
    except Exception:
        # Headless: updateGui can raise — fall back to time.sleep and
        # let the FreeCAD-side render finish.
        if delay_ms > 0:
            time.sleep(delay_ms / 1000.0)
        return
    if QtWidgets is None or QtCore is None:
        if delay_ms > 0:
            time.sleep(delay_ms / 1000.0)
        return

    try:
        app = QtWidgets.QApplication.instance()
    except Exception:
        app = None
    if app is None:
        # No GUI event loop installed — running in a non-GUI interpreter.
        if delay_ms > 0:
            time.sleep(delay_ms / 1000.0)
        return

    try:
        app.processEvents(QtCore.QEventLoop.AllEvents, delay_ms)
        if delay_ms > 0:
            try:
                QtCore.QThread.msleep(delay_ms)
            except Exception:
                time.sleep(delay_ms / 1000.0)
            app.processEvents(QtCore.QEventLoop.AllEvents, delay_ms)
    except Exception:
        # processEvents can fail if called from a non-GUI thread or
        # after QApplication shutdown. Treat as best-effort.
        if delay_ms > 0:
            time.sleep(delay_ms / 1000.0)


def _get_view_size(view: Any) -> tuple[int, int]:
    """Return the active view's pixel size, or (1024, 768) on failure."""
    try:
        size = view.getSize()
        if isinstance(size, (list, tuple)) and len(size) >= 2:
            return max(1, int(size[0])), max(1, int(size[1]))
        return max(1, int(size.width())), max(1, int(size.height()))
    except Exception:
        return 1024, 768


def _resolve_screenshot_size(
    view: Any,
    width: int | None,
    height: int | None,
) -> tuple[int, int]:
    """Resolve the requested screenshot size, falling back to the view size."""
    view_width, view_height = _get_view_size(view)
    resolved_width = view_width if width is None else max(1, int(width))
    resolved_height = view_height if height is None else max(1, int(height))
    return resolved_width, resolved_height


def process_gui_tasks() -> None:
    """Drain queued GUI-thread callables and reschedule.

    Resilience guarantees (added to fix the "empty response → permanent
    hang → restart-required" bug):

    1. Exceptions inside ``task()`` are caught and converted to an error
       string on the response queue. The QTimer reschedule still fires,
       so the dispatch loop survives handler bugs instead of dying.
    2. ``None`` returns are normalised to an error string so the response
       queue is never starved.
    3. The :data:`_DISPATCH_SHUTDOWN` sentinel lets :func:`stop_rpc_server`
       exit the loop cleanly without leaving an orphan QTimer chain.
    4. The ``finally`` block ALWAYS reschedules itself, even if something
       outside ``task()`` raises (e.g. while draining the queue itself or
       while scheduling the next tick).
    """
    try:
        while not rpc_request_queue.empty():
            task = rpc_request_queue.get()
            if task is _DISPATCH_SHUTDOWN:
                return  # exit cleanly; do not reschedule
            try:
                res = task()
            except Exception as e:
                if FreeCAD is not None and hasattr(FreeCAD, "Console"):
                    FreeCAD.Console.PrintError(
                        f"MCP RPC: GUI task raised {type(e).__name__}: {e}\n"
                        f"{traceback.format_exc()}"
                    )
                rpc_response_queue.put(f"{type(e).__name__}: {e}")
                continue
            if res is None:
                rpc_response_queue.put("GUI handler returned None")
            else:
                rpc_response_queue.put(res)
    except Exception as e:
        # Anything raised OUTSIDE the per-task try (e.g. queue.empty race,
        # _DISPATCH_SHUTDOWN comparison failure) must not abort the loop.
        if FreeCAD is not None and hasattr(FreeCAD, "Console"):
            FreeCAD.Console.PrintError(
                f"MCP RPC: process_gui_tasks dispatcher raised {type(e).__name__}: {e}\n"
                f"{traceback.format_exc()}"
            )
    finally:
        if QtCore is not None:
            try:
                QtCore.QTimer.singleShot(500, process_gui_tasks)
            except Exception as e:
                if FreeCAD is not None and hasattr(FreeCAD, "Console"):
                    FreeCAD.Console.PrintError(
                        f"MCP RPC: failed to reschedule process_gui_tasks: {type(e).__name__}: {e}\n"
                    )


__all__ = [
    "_DISPATCH_SHUTDOWN",
    "rpc_request_queue",
    "rpc_response_queue",
    "_SERVER_START_TIME",
    "rpc_server_thread",
    "rpc_server_instance",
    "_rpc_lock",
    "_flush_gui_events",
    "_get_view_size",
    "_resolve_screenshot_size",
    "process_gui_tasks",
]
