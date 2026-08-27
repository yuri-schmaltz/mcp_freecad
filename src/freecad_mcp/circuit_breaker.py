"""Circuit breaker for the FreeCAD XML-RPC client.

Why this exists
---------------
A professional MCP server must not let a transient FreeCAD outage turn
into a cascading failure: every ``tool`` call would block on a
``ServerProxy`` that nobody answered, the LLM host would queue up
unanswered requests, and the whole session would feel frozen.

The standard fix is a **circuit breaker** sitting in front of the
remote call. Three states:

* ``closed`` — calls flow through normally. Each transient failure
  increments a counter; ``threshold`` failures in a row trips the
  breaker.
* ``open`` — calls fail fast with ``CircuitOpenError`` without ever
  touching the network. After ``reset_timeout`` seconds the breaker
  moves to ``half_open``.
* ``half_open`` — the next call is allowed through. If it succeeds the
  breaker closes; if it fails the breaker re-opens for another
  ``reset_timeout``.

The breaker also handles **retry with exponential backoff** for
transient failures while the circuit is closed, so a flaky FreeCAD
that comes back after 200ms does not trip the breaker at all.

Which errors are transient?
---------------------------
Anything that looks like a connection problem:

* ``ConnectionError`` (incl. ``ConnectionRefusedError``)
* ``socket.timeout``
* ``TimeoutError``
* ``xmlrpc.client.ProtocolError`` (transport-level)
* ``OSError`` (DNS, network unreachable, etc.)

A ``xmlrpc.client.Fault`` is **not** transient — the server replied
with an application-level error. We do not retry or trip on those.

Configuration
-------------
Two env vars (read once at construction time; pass a dict to override
in tests):

* ``FREECAD_MCP_CB_THRESHOLD`` (default 3) — consecutive failures
  before the breaker opens.
* ``FREECAD_MCP_CB_RESET_S`` (default 60) — seconds to wait before
  half-open.
* ``FREECAD_MCP_RETRY_MAX`` (default 3) — retries while closed.
* ``FREECAD_MCP_RETRY_BASE_S`` (default 0.1) — base delay; doubles
  each attempt.
"""
from __future__ import annotations

import logging
import os
import socket
import time
import xmlrpc.client
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

logger = logging.getLogger("FreeCADMCPcircuit")

T = TypeVar("T")


class CircuitOpenError(RuntimeError):
    """Raised when a call is short-circuited because the breaker is open.

    Surfaces as a regular exception to the caller; the upstream tool
    layer formats it into a ``text_response``.
    """

    def __init__(self, reset_in: float):
        super().__init__(f"Circuit breaker open; retry in {reset_in:.1f}s")
        self.reset_in = float(reset_in)


# Errors that are safe to retry and that count toward the breaker.
_TRANSIENT_ERRORS: tuple[type[BaseException], ...] = (
    ConnectionError,         # includes ConnectionRefusedError, ConnectionResetError
    socket.timeout,
    TimeoutError,
    ConnectionResetError,
    ConnectionAbortedError,
    OSError,                 # covers DNS errors, network unreachable
    xmlrpc.client.ProtocolError,
)


def _is_transient(exc: BaseException) -> bool:
    return isinstance(exc, _TRANSIENT_ERRORS)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        v = int(raw)
        return v if v > 0 else default
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        v = float(raw)
        return v if v > 0 else default
    except ValueError:
        return default


@dataclass
class CircuitState:
    state: str = "closed"  # closed | open | half_open
    consecutive_failures: int = 0
    opened_at: float = 0.0
    last_error: str = ""

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "consecutive_failures": self.consecutive_failures,
            "opened_at": self.opened_at,
            "last_error": self.last_error,
        }


class CircuitBreaker:
    """Wrap a callable with retry + circuit-breaking.

    Use as a decorator: ``@breaker`` or ``@breaker()`` (no args). The
    wrapped function is called with the same arguments. On a transient
    failure the breaker retries up to ``retry_max`` times with
    exponential backoff; if all attempts fail the breaker increments
    its failure counter and may trip to ``open``.
    """

    def __init__(
        self,
        threshold: int | None = None,
        reset_s: float | None = None,
        retry_max: int | None = None,
        retry_base_s: float | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.threshold = threshold if threshold is not None else _env_int("FREECAD_MCP_CB_THRESHOLD", 3)
        self.reset_s = reset_s if reset_s is not None else _env_float("FREECAD_MCP_CB_RESET_S", 60.0)
        self.retry_max = retry_max if retry_max is not None else _env_int("FREECAD_MCP_RETRY_MAX", 3)
        self.retry_base_s = retry_base_s if retry_base_s is not None else _env_float("FREECAD_MCP_RETRY_BASE_S", 0.1)
        self._sleep = sleep  # injectable for tests
        self.status = CircuitState()
        self._total_calls = 0
        self._total_failures = 0
        self._total_short_circuits = 0

    # --- state transitions -------------------------------------------------

    def _open(self, exc: BaseException) -> None:
        self.status.state = "open"
        self.status.opened_at = time.monotonic()
        self.status.last_error = f"{type(exc).__name__}: {exc}"
        logger.warning(
            "circuit OPEN after %d consecutive failures; last=%s",
            self.status.consecutive_failures, self.status.last_error,
        )

    def _close(self) -> None:
        if self.status.state != "closed":
            logger.info(
                "circuit CLOSED (was %s, failures reset from %d)",
                self.status.state, self.status.consecutive_failures,
            )
        self.status.state = "closed"
        self.status.consecutive_failures = 0
        self.status.opened_at = 0.0
        self.status.last_error = ""

    def reset(self) -> dict[str, Any]:
        """Force the breaker back to ``closed`` regardless of state.

        Exposed for operator-driven recovery (admin endpoint, REST
        handler, operator CLI). Returns the post-reset snapshot so the
        caller can confirm the action took effect.
        """
        previous_state = self.status.state
        self._close()
        logger.info(
            "circuit RESET (was %s); consecutive_failures cleared", previous_state
        )
        return self.metrics()

    def _maybe_half_open(self) -> bool:
        """Check if the reset window has elapsed; if so, move to half_open.

        Returns True if a call should be permitted right now.
        """
        if self.status.state != "open":
            return True
        elapsed = time.monotonic() - self.status.opened_at
        if elapsed >= self.reset_s:
            self.status.state = "half_open"
            logger.info(
                "circuit HALF_OPEN after %.1fs; next call is a probe",
                elapsed,
            )
            return True
        return False

    # --- decorator ---------------------------------------------------------

    def __call__(self, fn: Callable[..., T]) -> Callable[..., T]:
        from functools import wraps

        @wraps(fn)
        def wrapper(*args, **kwargs):
            return self.call(lambda: fn(*args, **kwargs))

        return wrapper

    def call(self, fn: Callable[[], Any]) -> Any:
        """Run *fn* through the breaker.

        * If the breaker is open and the reset window has not elapsed,
          raise :class:`CircuitOpenError` immediately.
        * If the breaker is open and the reset window has elapsed,
          move to half_open and let the call through as a probe.
        * Otherwise, retry with exponential backoff while failures
          remain transient. Non-transient exceptions propagate
          immediately.

        The return type is intentionally broad (``Any``) because the
        stdlib ``xmlrpc.client.ServerProxy`` uses dynamic attribute
        access; the caller already knows the concrete shape.
        """
        self._total_calls += 1

        if not self._maybe_half_open():
            self._total_short_circuits += 1
            raise CircuitOpenError(reset_in=self.reset_s - (time.monotonic() - self.status.opened_at))

        attempt = 0
        delay = self.retry_base_s
        last_exc: BaseException | None = None

        while attempt <= self.retry_max:
            try:
                result = fn()
            except BaseException as exc:
                last_exc = exc
                if not _is_transient(exc) or attempt == self.retry_max:
                    # Either non-transient (don't trip on this) or
                    # we've used all retries.
                    if _is_transient(exc):
                        self._record_failure(exc)
                    raise
                logger.debug(
                    "transient failure on attempt %d/%d (%s: %s); sleeping %.2fs",
                    attempt + 1, self.retry_max + 1,
                    type(exc).__name__, exc, delay,
                )
                self._sleep(delay)
                delay *= 2
                attempt += 1
                continue

            # Success path.
            self._close()
            return result

        # Unreachable in normal flow (the loop returns or raises), but
        # defensive: the type checker doesn't know that.
        raise last_exc  # type: ignore[misc]

    def _record_failure(self, exc: BaseException) -> None:
        self._total_failures += 1
        self.status.consecutive_failures += 1
        self.status.last_error = f"{type(exc).__name__}: {exc}"
        if self.status.consecutive_failures >= self.threshold:
            self._open(exc)

    # --- introspection -----------------------------------------------------

    def metrics(self) -> dict[str, Any]:
        return {
            **self.status.snapshot(),
            "threshold": self.threshold,
            "reset_s": self.reset_s,
            "retry_max": self.retry_max,
            "total_calls": self._total_calls,
            "total_failures": self._total_failures,
            "total_short_circuits": self._total_short_circuits,
        }


__all__ = [
    "CircuitBreaker",
    "CircuitOpenError",
    "CircuitState",
    "_is_transient",
    "_TRANSIENT_ERRORS",
]
