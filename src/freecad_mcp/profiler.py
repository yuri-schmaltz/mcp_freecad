"""Lightweight in-process profiler for MCP tool calls.

Why a separate module (instead of overloading ``metrics.py``)?
------------------------------------------------------------
The Prometheus-style :mod:`metrics` registry is designed for *aggregate*
counters and histograms that get scraped by an external collector. The
profiler here is for *operational* introspection by the LLM/operator:

* ring buffer of the last N individual tool calls;
* per-tool percentile stats over the ring window;
* flamegraph-friendly collapsed-stack export.

Both the ring buffer and the per-tool stat computation are thread-safe
under an ``RLock`` — the FastMCP dispatch layer invokes tools from
the event loop, while the RPC server may concurrently append entries
from a worker thread.

Public surface
--------------
* :class:`ProfileEntry` — one observed tool call.
* :class:`PerformanceProfiler` — ring buffer + stats + flamegraph export.
* :func:`get_profiler` — module-level singleton accessor.
* :func:`_profile_decorator` — decorator factory used by ``server.py``.
"""
from __future__ import annotations

import os
import statistics
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass
from functools import wraps
from typing import Any, TypeVar

F = TypeVar("F", bound=Callable[..., Any])

_DEFAULT_SLOW_THRESHOLD_MS = float(
    os.environ.get("FREECAD_MCP_SLOW_THRESHOLD_MS", "500")
)


@dataclass
class ProfileEntry:
    """One observed tool invocation."""

    tool_name: str
    start_time: float
    duration_ms: float
    success: bool
    error: str | None = None


class PerformanceProfiler:
    """Thread-safe ring buffer of recent tool-call profiles."""

    def __init__(
        self,
        max_entries: int = 1000,
        slow_threshold_ms: float = _DEFAULT_SLOW_THRESHOLD_MS,
    ) -> None:
        if max_entries <= 0:
            raise ValueError("max_entries must be > 0")
        self._buffer: deque[ProfileEntry] = deque(maxlen=max_entries)
        self._lock = threading.RLock()
        self.slow_threshold_ms = float(slow_threshold_ms)

    def record(self, entry: ProfileEntry) -> None:
        with self._lock:
            self._buffer.append(entry)
            if entry.duration_ms >= self.slow_threshold_ms:
                import logging

                logging.getLogger("FreeCADMCPserver").info(
                    "slow tool call: %s took %.1fms (threshold %.1fms)",
                    entry.tool_name,
                    entry.duration_ms,
                    self.slow_threshold_ms,
                )

    def get_recent(self, n: int = 50) -> list[ProfileEntry]:
        if n <= 0:
            return []
        with self._lock:
            buf = list(self._buffer)
        return buf[-n:]

    def get_slow_calls(self, threshold_ms: float = 1000) -> list[ProfileEntry]:
        if threshold_ms <= 0:
            raise ValueError("threshold_ms must be > 0")
        with self._lock:
            return [e for e in self._buffer if e.duration_ms >= threshold_ms]

    def get_stats(self) -> dict[str, dict[str, float]]:
        """Per-tool summary statistics."""
        grouped: dict[str, list[float]] = defaultdict(list)
        with self._lock:
            for e in self._buffer:
                grouped[e.tool_name].append(e.duration_ms)
        out: dict[str, dict[str, float]] = {}
        for tool, durations in grouped.items():
            n = len(durations)
            mean = sum(durations) / n
            if n < 2:
                out[tool] = {
                    "count": float(n),
                    "mean_ms": round(mean, 3),
                    "p50_ms": round(mean, 3),
                    "p95_ms": round(mean, 3),
                    "p99_ms": round(mean, 3),
                    "max_ms": round(max(durations), 3),
                }
                continue
            qs = statistics.quantiles(durations, n=100)
            out[tool] = {
                "count": float(n),
                "mean_ms": round(mean, 3),
                "p50_ms": round(qs[49], 3),
                "p95_ms": round(qs[94], 3),
                "p99_ms": round(qs[98], 3),
                "max_ms": round(max(durations), 3),
            }
        return out

    def export_flamegraph_data(self) -> str:
        """Collapsed-stacks text for Brendan Gregg's ``flamegraph.pl``.

        Format (one line per stack frame, tab-separated)::

            tool_name count duration_ms
        """
        grouped: dict[str, list[float]] = defaultdict(list)
        with self._lock:
            for e in self._buffer:
                grouped[e.tool_name].append(e.duration_ms)
        lines = []
        for tool, durations in sorted(grouped.items()):
            count = len(durations)
            mean = sum(durations) / count if count else 0.0
            lines.append(f"{tool} {count} {round(mean, 3)}")
        return "\n".join(lines) + ("\n" if lines else "")

    def reset(self) -> None:
        with self._lock:
            self._buffer.clear()

    @property
    def max_entries(self) -> int:
        return self._buffer.maxlen or 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._buffer)


_profiler_singleton: PerformanceProfiler | None = None
_profiler_singleton_lock = threading.Lock()


def get_profiler() -> PerformanceProfiler:
    """Return the process-wide :class:`PerformanceProfiler`."""
    global _profiler_singleton
    if _profiler_singleton is None:
        with _profiler_singleton_lock:
            if _profiler_singleton is None:
                _profiler_singleton = PerformanceProfiler()
    return _profiler_singleton


def reset_profiler_singleton() -> None:
    """Drop the cached singleton. Test-only."""
    global _profiler_singleton
    with _profiler_singleton_lock:
        _profiler_singleton = None


def _profile_decorator[F: Callable[..., Any]](fn: F) -> F:
    """Decorator that records each call into the module-level profiler."""

    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        profiler = get_profiler()
        start_wall = time.time()
        start_mono = time.monotonic()
        success = True
        error_msg: str | None = None
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            success = False
            error_msg = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            duration_ms = (time.monotonic() - start_mono) * 1000.0
            profiler.record(
                ProfileEntry(
                    tool_name=fn.__name__,
                    start_time=start_wall,
                    duration_ms=duration_ms,
                    success=success,
                    error=error_msg,
                )
            )

    return wrapper  # type: ignore[return-value]


__all__ = [
    "ProfileEntry",
    "PerformanceProfiler",
    "get_profiler",
    "reset_profiler_singleton",
    "_profile_decorator",
]
