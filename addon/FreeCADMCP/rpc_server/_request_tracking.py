"""Per-request tracking primitives for the FreeCAD RPC server.

Provides idempotency-key deduplication and cooperative cancellation:

* A caller may attach a string ``request_id`` to any tracked method.
* If the server has already completed a request with that id, the cached
  response is returned without re-executing the handler.
* If the caller invokes ``cancel_request(request_id)`` before the handler
  runs, the queued task short-circuits and reports ``cancelled``.
* After a request finishes its cached response is retained until either
  the cache fills (FIFO eviction) or :func:`clear_request_cache` is called.

Keeping this in a standalone module makes it unit-testable without
FreeCAD / PySide.
"""
from __future__ import annotations

import collections
import threading
from typing import Any


class RequestTracker:
    """Thread-safe idempotency + cancellation state.

    ``max_cached`` bounds memory: the oldest cached response is evicted
    on overflow (FIFO). Default 256 — enough for short-lived agent
    workflows without leaking forever.
    """

    def __init__(self, max_cached: int = 256) -> None:
        self._max_cached = max(1, int(max_cached))
        self._completed: dict[str, Any] = {}
        self._completed_order: collections.deque[str] = collections.deque()
        self._cancelled: set[str] = set()
        self._lock = threading.RLock()

    # --- idempotency -----------------------------------------------------

    def get_cached(self, request_id: str | None) -> Any | None:
        if request_id is None:
            return None
        with self._lock:
            return self._completed.get(request_id)

    def cache_response(self, request_id: str | None, response: Any) -> None:
        if request_id is None:
            return
        with self._lock:
            if request_id in self._completed:
                return  # first writer wins; no clobbering
            self._completed[request_id] = response
            self._completed_order.append(request_id)
            while len(self._completed_order) > self._max_cached:
                old = self._completed_order.popleft()
                self._completed.pop(old, None)

    # --- cancellation ----------------------------------------------------

    def cancel(self, request_id: str) -> bool:
        """Mark *request_id* as cancelled.

        Returns True if the id was newly cancelled, False if it was not
        cancelled (already completed, or input was falsy). Use the
        ``FreeCADRPC.cancel_request`` wrapper for input validation.
        """
        if not request_id:
            return False
        with self._lock:
            if request_id in self._completed:
                return False  # too late
            self._cancelled.add(request_id)
            return True

    def is_cancelled(self, request_id: str | None) -> bool:
        if request_id is None:
            return False
        with self._lock:
            return request_id in self._cancelled

    def consume_cancel(self, request_id: str | None) -> bool:
        """Return and clear the cancelled flag for *request_id*."""
        if request_id is None:
            return False
        with self._lock:
            if request_id in self._cancelled:
                self._cancelled.discard(request_id)
                return True
            return False

    # --- introspection ---------------------------------------------------

    def cached_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._completed_order)

    def pending_cancellations(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._cancelled)

    def clear(self) -> None:
        with self._lock:
            self._completed.clear()
            self._completed_order.clear()
            self._cancelled.clear()


# A default singleton for the running RPC server. Tests construct their
# own instance via the constructor above.
_tracker: RequestTracker | None = None
_tracker_lock = threading.Lock()


def get_default_tracker() -> RequestTracker:
    """Return the process-wide tracker, creating it on first access."""
    global _tracker
    with _tracker_lock:
        if _tracker is None:
            _tracker = RequestTracker()
        return _tracker


def reset_default_tracker() -> None:
    """Reset the process-wide tracker (tests only)."""
    global _tracker
    with _tracker_lock:
        _tracker = None


__all__ = [
    "RequestTracker",
    "get_default_tracker",
    "reset_default_tracker",
]
