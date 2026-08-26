"""Unit tests for the circuit breaker and its integration with FreeCADConnection.

The breaker is the front-line defence against a FreeCAD crash turning
into a frozen LLM session. Tests inject fake servers and time so the
state machine can be exercised deterministically.
"""
import sys
import xmlrpc.client
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from freecad_mcp.circuit_breaker import (  # noqa: E402
    CircuitBreaker,
    CircuitOpenError,
    _is_transient,
)
from freecad_mcp.freecad_client import FreeCADConnection  # noqa: E402


def _no_sleep(_seconds: float) -> None:
    """Replace time.sleep in tests so retries don't actually delay."""
    return None


# --- state machine --------------------------------------------------------

def test_breaker_starts_closed():
    cb = CircuitBreaker(threshold=3, reset_s=60, sleep=_no_sleep)
    assert cb.status.state == "closed"


def test_breaker_opens_after_threshold_failures():
    cb = CircuitBreaker(threshold=3, reset_s=60, retry_max=0, sleep=_no_sleep)

    def boom():
        raise ConnectionRefusedError("nope")

    for _ in range(3):
        try:
            cb.call(boom)
        except ConnectionRefusedError:
            pass
    assert cb.status.state == "open"


def test_breaker_short_circuits_when_open():
    cb = CircuitBreaker(threshold=1, reset_s=60, retry_max=0, sleep=_no_sleep)
    try:
        cb.call(lambda: (_ for _ in ()).throw(ConnectionRefusedError("x")))
    except ConnectionRefusedError:
        pass
    assert cb.status.state == "open"

    # The next call must NOT reach the underlying function.
    reached = False
    def should_not_run():
        nonlocal reached
        reached = True
        return "ok"

    try:
        cb.call(should_not_run)
    except CircuitOpenError as e:
        assert "open" in str(e).lower()
    assert reached is False, "circuit was open but the call still ran"


def test_breaker_recovers_via_half_open():
    cb = CircuitBreaker(threshold=1, reset_s=0.0, retry_max=0, sleep=_no_sleep)
    try:
        cb.call(lambda: (_ for _ in ()).throw(ConnectionRefusedError("x")))
    except ConnectionRefusedError:
        pass
    assert cb.status.state == "open"

    # With reset_s=0, the very next call should be allowed through.
    result = cb.call(lambda: "ok")
    assert result == "ok"
    assert cb.status.state == "closed"


def test_breaker_reopens_if_probe_fails():
    cb = CircuitBreaker(threshold=1, reset_s=0.0, retry_max=0, sleep=_no_sleep)
    # Open it.
    try:
        cb.call(lambda: (_ for _ in ()).throw(ConnectionRefusedError("x")))
    except ConnectionRefusedError:
        pass
    assert cb.status.state == "open"
    # First call is a probe; make it fail.
    try:
        cb.call(lambda: (_ for _ in ()).throw(ConnectionRefusedError("still down")))
    except ConnectionRefusedError:
        pass
    assert cb.status.state == "open"


def test_breaker_retries_transient_then_succeeds():
    """A flaky call that fails twice and then succeeds should not open the breaker."""
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) < 3:
            raise TimeoutError("flaky")
        return "yay"

    cb = CircuitBreaker(threshold=3, reset_s=60, retry_max=3, retry_base_s=0.01, sleep=_no_sleep)
    result = cb.call(flaky)
    assert result == "yay"
    assert len(attempts) == 3
    assert cb.status.state == "closed"


def test_breaker_does_not_retry_non_transient():
    """xmlrpc.client.Fault is an application-level error, not a transport
    failure. The breaker must let it propagate immediately.
    """
    attempts = []

    def boom():
        attempts.append(1)
        raise xmlrpc.client.Fault(1, "application error")

    cb = CircuitBreaker(threshold=3, reset_s=60, retry_max=3, retry_base_s=0.01, sleep=_no_sleep)
    try:
        cb.call(boom)
    except xmlrpc.client.Fault:
        pass
    assert len(attempts) == 1, "non-transient error was retried"
    assert cb.status.state == "closed", "non-transient error tripped the breaker"


def test_breaker_records_last_error_message():
    cb = CircuitBreaker(threshold=1, reset_s=60, retry_max=0, sleep=_no_sleep)
    try:
        cb.call(lambda: (_ for _ in ()).throw(ConnectionRefusedError("FreeCAD down")))
    except ConnectionRefusedError:
        pass
    assert "FreeCAD down" in cb.status.last_error


# --- is_transient classification ------------------------------------------

def test_is_transient_known_types():
    assert _is_transient(ConnectionRefusedError("x"))
    assert _is_transient(TimeoutError("x"))
    assert _is_transient(TimeoutError("x"))
    assert _is_transient(OSError("x"))
    assert _is_transient(xmlrpc.client.ProtocolError("u", 500, "x", {}))


def test_is_transient_rejects_application_errors():
    assert not _is_transient(ValueError("x"))
    assert not _is_transient(RuntimeError("x"))
    assert not _is_transient(xmlrpc.client.Fault(1, "x"))


# --- integration with FreeCADConnection ----------------------------------

def test_freecad_connection_uses_breaker():
    """A failing RPC method on the underlying proxy must surface through the breaker."""
    proxy = MagicMock()
    proxy.ping = MagicMock(side_effect=ConnectionRefusedError("FreeCAD not running"))
    conn = FreeCADConnection.__new__(FreeCADConnection)  # bypass __init__ (no real proxy)
    conn.server = proxy
    conn.timeout = 10
    conn.breaker = CircuitBreaker(threshold=2, reset_s=60, retry_max=0, sleep=_no_sleep)
    # Two failures trip the breaker.
    for _ in range(2):
        try:
            conn.ping()
        except ConnectionRefusedError:
            pass
    assert conn.breaker.status.state == "open"
    # Third call short-circuits; proxy is NOT called.
    proxy.ping.reset_mock()
    try:
        conn.ping()
    except CircuitOpenError:
        pass
    assert proxy.ping.call_count == 0


def test_breaker_metrics_observable():
    cb = CircuitBreaker(threshold=3, reset_s=60, retry_max=0, sleep=_no_sleep)
    cb.call(lambda: "ok")
    try:
        cb.call(lambda: (_ for _ in ()).throw(ConnectionRefusedError("x")))
    except ConnectionRefusedError:
        pass
    metrics = cb.metrics()
    assert metrics["state"] == "closed"
    assert metrics["consecutive_failures"] == 1
    assert metrics["total_calls"] == 2
    assert metrics["total_failures"] == 1
    assert metrics["threshold"] == 3


# --- env var override ----------------------------------------------------

def test_env_threshold(monkeypatch):
    monkeypatch.setenv("FREECAD_MCP_CB_THRESHOLD", "5")
    monkeypatch.setenv("FREECAD_MCP_RETRY_MAX", "0")
    cb = CircuitBreaker(sleep=_no_sleep)
    assert cb.threshold == 5


def test_env_threshold_invalid_value_falls_back(monkeypatch):
    """Non-integer FREECAD_MCP_CB_THRESHOLD falls back to default."""
    from src.freecad_mcp.circuit_breaker import _env_int
    monkeypatch.setenv("FREECAD_MCP_CB_THRESHOLD", "not-a-number")
    assert _env_int("FREECAD_MCP_CB_THRESHOLD", 7) == 7


def test_env_threshold_zero_or_negative_falls_back(monkeypatch):
    """Zero/negative thresholds are rejected (must be > 0)."""
    from src.freecad_mcp.circuit_breaker import _env_int
    monkeypatch.setenv("FREECAD_MCP_CB_THRESHOLD", "0")
    assert _env_int("FREECAD_MCP_CB_THRESHOLD", 7) == 7
    monkeypatch.setenv("FREECAD_MCP_CB_THRESHOLD", "-3")
    assert _env_int("FREECAD_MCP_CB_THRESHOLD", 7) == 7


def test_env_reset_invalid_value_falls_back(monkeypatch):
    """Non-float FREECAD_MCP_CB_RESET_S falls back to default."""
    from src.freecad_mcp.circuit_breaker import _env_float
    monkeypatch.setenv("FREECAD_MCP_CB_RESET_S", "abc")
    assert _env_float("FREECAD_MCP_CB_RESET_S", 30.0) == 30.0


def test_env_reset_zero_or_negative_falls_back(monkeypatch):
    """Zero/negative reset windows are rejected."""
    from src.freecad_mcp.circuit_breaker import _env_float
    monkeypatch.setenv("FREECAD_MCP_CB_RESET_S", "0")
    assert _env_float("FREECAD_MCP_CB_RESET_S", 30.0) == 30.0
    monkeypatch.setenv("FREECAD_MCP_CB_RESET_S", "-1.5")
    assert _env_float("FREECAD_MCP_CB_RESET_S", 30.0) == 30.0


def test_breaker_decorator_passes_args_and_result():
    """The @breaker decorator routes through .call and returns the value."""
    cb = CircuitBreaker(sleep=_no_sleep)

    @cb
    def add(a, b):
        return a + b

    assert add(2, 3) == 5
    m = cb.metrics()
    assert m["total_calls"] == 1
    assert m["total_failures"] == 0


def test_breaker_decorator_propagates_transient_exceptions():
    """The decorator surfaces transient exceptions and counts failures."""
    cb = CircuitBreaker(sleep=_no_sleep, threshold=10, retry_max=0)

    @cb
    def boom():
        raise ConnectionRefusedError("nope")

    import pytest
    with pytest.raises(ConnectionRefusedError, match="nope"):
        boom()
    m = cb.metrics()
    assert m["total_failures"] == 1


def test_breaker_decorator_propagates_non_transient_without_counting():
    """Non-transient exceptions propagate immediately and don't count
    toward the breaker failure tally (they're caller bugs, not
    transport issues)."""
    cb = CircuitBreaker(sleep=_no_sleep, threshold=10)

    @cb
    def boom():
        raise ValueError("nope")

    import pytest
    with pytest.raises(ValueError, match="nope"):
        boom()
    m = cb.metrics()
    assert m["total_failures"] == 0


if __name__ == "__main__":
    import sys
    print("Run with pytest; direct invocation is not supported.")
    sys.exit(0)
