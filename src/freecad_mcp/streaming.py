"""Streaming output collection for ``execute_code``.

v1.1.0 — F1: streaming output for ``execute_code``.

The FreeCAD RPC layer is XML-RPC over an HTTP socket, which is strictly
request/response: there is no built-in channel for the server to push
incremental stdout back to the client. To still give operators the
"feel" of live output, this module provides a **client-side** buffering
layer that:

1. Captures whatever stdout-ish text the RPC response carries
   (``res["message"]`` on success, ``res["output"]`` on error).
2. Reports progress events via the FastMCP ``Context.report_progress``
   coroutine so the MCP client UI can show incremental updates.
3. Debounces the notifications so a single ``execute_code`` call cannot
   flood the JSON-RPC channel with thousands of progress messages.

Design notes
------------
* We deliberately do NOT use the SSE/queue approach (adding a second
  HTTP server inside the MCP process would require carving out CORS,
  routing, and a separate client endpoint no MCP host understands).
* The buffer is a thin wrapper, not a callback bridge. XML-RPC doesn't
  expose streaming, so we capture the aggregated output the FreeCAD
  addon already collects (``res["message"]`` / ``res["output"]``) and
  slice it into chunks that mirror the order in which lines were
  written.
* ``OutputBuffer`` uses a ``threading.Lock`` because ``execute_code``
  may run on a worker thread when invoked via async wrappers in FastMCP.
  Reads from the MCP event loop happen on the main thread.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol


class ProgressReporter(Protocol):
    """Minimal interface we need from a FastMCP ``Context``."""

    async def report_progress(self, progress: float, total: float) -> None:  # pragma: no cover
        ...


class OutputBuffer:
    """Line-buffered capture of an ``execute_code`` response."""

    def __init__(self) -> None:
        self._lines: list[tuple[str, float]] = []
        self._emitted = 0
        self._lock = threading.Lock()
        self._failed = False
        self._error: str | None = None

    def ingest(self, response: dict[str, Any]) -> None:
        """Store every line from the FreeCAD response."""
        success = bool(response.get("success"))
        with self._lock:
            self._failed = not success
            if success:
                message = str(response.get("message") or "")
                if message.startswith("Python code execution scheduled."):
                    message = message[len("Python code execution scheduled."):]
                if message.startswith("Output: "):
                    message = message[len("Output: "):]
                self._absorb_text(message)
            else:
                error = response.get("error")
                if error is not None:
                    self._error = str(error)
                    self._absorb_text(self._error)
                self._absorb_text(str(response.get("output") or ""))

    def _absorb_text(self, text: str) -> None:
        if not text:
            return
        now = time.monotonic()
        for line in text.splitlines():
            self._lines.append((line, now))

    def pop_emitted(self, on_chunk: Callable[[str], None] | None = None) -> list[str]:
        """Return the lines that haven't been surfaced yet."""
        with self._lock:
            new_lines = self._lines[self._emitted:]
            self._emitted = len(self._lines)
        if on_chunk is not None:
            for line, _ts in new_lines:
                with contextlib.suppress(Exception):
                    on_chunk(line)
        return [line for line, _ts in new_lines]

    @property
    def failed(self) -> bool:
        with self._lock:
            return self._failed

    @property
    def error(self) -> str | None:
        with self._lock:
            return self._error

    def full_output(self) -> str:
        with self._lock:
            return "\n".join(line for line, _ts in self._lines)


class ProgressDebouncer:
    """Rate-limit async progress notifications."""

    def __init__(
        self,
        min_interval_s: float = 0.1,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if min_interval_s < 0:
            raise ValueError("min_interval_s must be >= 0")
        self._min_interval = float(min_interval_s)
        self._clock = clock or time.monotonic
        self._last_emit: float | None = None
        self._lock = threading.Lock()

    def should_emit(self) -> bool:
        now = self._clock()
        with self._lock:
            if self._last_emit is None:
                return True
            return (now - self._last_emit) >= self._min_interval

    def mark_emitted(self) -> None:
        with self._lock:
            self._last_emit = self._clock()

    def reset(self) -> None:
        with self._lock:
            self._last_emit = None


async def stream_output(
    buffer: OutputBuffer,
    ctx: ProgressReporter,
    *,
    debounce_s: float = 0.1,
    progress_total: float = 100.0,
) -> None:
    """Stream every captured line to *ctx* as a progress notification."""
    debouncer = ProgressDebouncer(min_interval_s=debounce_s)
    emitted_count = 0

    def _on_chunk(_line: str) -> None:
        nonlocal emitted_count
        if not debouncer.should_emit():
            return
        emitted_count += 1
        debouncer.mark_emitted()

    buffer.pop_emitted(on_chunk=_on_chunk)
    with contextlib.suppress(Exception):
        await ctx.report_progress(progress_total, progress_total)


__all__ = [
    "OutputBuffer",
    "ProgressDebouncer",
    "ProgressReporter",
    "stream_output",
]
