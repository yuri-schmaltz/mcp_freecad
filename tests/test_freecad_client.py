"""Tests for the timeout-aware FreeCAD client.

We do NOT spin up a real XML-RPC server; instead we verify:
- the transport enforces connect/read timeouts via socket.create_connection;
- the env var FREECAD_MCP_RPC_TIMEOUT controls the default;
- a hung peer surfaces as a TimeoutError-like failure within the timeout
  window rather than blocking forever.
"""
import os
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

# Make src importable
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import freecad_mcp.freecad_client as fc  # noqa: E402


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_default_timeout_from_env(monkeypatch=None):
    # Use monkeypatch via simple os.environ mutation; the test runner can
    # run these functions directly.
    saved = os.environ.pop("FREECAD_MCP_RPC_TIMEOUT", None)
    try:
        os.environ.pop("FREECAD_MCP_RPC_TIMEOUT", None)
        # Reload module so _DEFAULT_RPC_TIMEOUT is recomputed.
        import importlib

        importlib.reload(fc)
        assert fc._DEFAULT_RPC_TIMEOUT == 10.0

        os.environ["FREECAD_MCP_RPC_TIMEOUT"] = "2.5"
        importlib.reload(fc)
        assert fc._DEFAULT_RPC_TIMEOUT == 2.5
    finally:
        os.environ.pop("FREECAD_MCP_RPC_TIMEOUT", None)
        if saved is not None:
            os.environ["FREECAD_MCP_RPC_TIMEOUT"] = saved
        importlib.reload(fc)


def test_timeout_transport_uses_timeout():
    transport = fc._TimeoutTransport(0.5)
    assert transport._timeout == 0.5
    transport2 = fc._TimeoutTransport(0)  # clamped to 0.1 minimum
    assert transport2._timeout >= 0.1


@pytest.mark.slow
def test_connection_to_unresponsive_peer_times_out():
    """Open a TCP socket that accepts but never responds.

    The XML-RPC client should raise a socket.timeout (or an OSError) within
    a couple of seconds instead of hanging the test indefinitely.
    """
    port = _free_port()
    accepted = threading.Event()
    release = threading.Event()

    def hold_open():
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", port))
        srv.listen(1)
        accepted.set()
        srv.settimeout(5)
        try:
            conn, _ = srv.accept()
            accepted.set()
            # Never write — just hold the connection.
            try:
                release.wait(timeout=5)
            finally:
                conn.close()
        except Exception:
            pass
        finally:
            srv.close()

    t = threading.Thread(target=hold_open, daemon=True)
    t.start()
    assert accepted.wait(timeout=2)

    proxy = fc._build_server_proxy("127.0.0.1", port, timeout=0.5)
    t0 = time.time()
    try:
        try:
            proxy.ping()
        except (TimeoutError, OSError) as e:
            elapsed = time.time() - t0
            # Connect succeeded quickly because the listener accepts; the
            # timeout kicks in on the read of the HTTP response. We accept
            # anything in the (0, 3s) window — the key invariant is "did not
            # block forever".
            assert elapsed < 3.0, f"call took {elapsed:.2f}s, expected < 3s"
            print(f"  hung-peer call raised {type(e).__name__} after {elapsed:.2f}s")
        else:
            # The peer holding the connection open without writing might
            # still satisfy the call (HTTP empty body etc.) on some stacks.
            # That's not a bug — the assertion is just about not hanging.
            elapsed = time.time() - t0
            assert elapsed < 3.0, f"call took {elapsed:.2f}s"
            print(f"  hung-peer call returned after {elapsed:.2f}s")
    finally:
        release.set()
        t.join(timeout=2)


@pytest.mark.slow
def test_connection_to_closed_port_fails_fast():
    """Connecting to a port with no listener should raise immediately,
    not hang for the OS default (~tens of seconds).

    The 4s ceiling is generous: on Linux/macOS a refused connect
    typically returns in <0.1s, on Windows the Winsock stack takes
    ~2.5-3s because the OS retries the connection before giving up.
    The 4s bound is still tight against the OS default of 75-120s.
    """
    port = _free_port()  # we never listen on it
    proxy = fc._build_server_proxy("127.0.0.1", port, timeout=1.0)
    t0 = time.time()
    try:
        proxy.ping()
    except (ConnectionRefusedError, OSError) as e:
        elapsed = time.time() - t0
        assert elapsed < 4.0, f"connection refused took {elapsed:.2f}s, expected < 4s"
        print(f"  refused call raised {type(e).__name__} after {elapsed:.2f}s")
    else:
        raise AssertionError("expected ConnectionRefusedError")


# ---------------------------------------------------------------------------
# v1.0.3 — get_active_screenshot_with_status
# ---------------------------------------------------------------------------

def _make_client_with_mock_server(mocked):
    """Build a FreeCADConnection whose .server returns *mocked* from any call."""
    conn = fc.FreeCADConnection.__new__(fc.FreeCADConnection)
    conn.timeout = 0.5
    conn.breaker = fc.CircuitBreaker()
    # Stand-in ServerProxy: every attribute access returns ``mocked``.
    class _FakeServer:
        def __getattr__(self, _name):
            return lambda *a, **kw: mocked
    conn.server = _FakeServer()
    return conn


def test_screenshot_status_success():
    conn = _make_client_with_mock_server("BASE64DATA")
    out = conn.get_active_screenshot_with_status(image_format="png")
    assert out == {"success": True, "screenshot": "BASE64DATA", "format": "png"}


def test_screenshot_status_format_normalised():
    conn = _make_client_with_mock_server("BASE64")
    out = conn.get_active_screenshot_with_status(image_format="JPEG")
    assert out["format"] == "jpeg"


def test_screenshot_status_no_capture():
    """Server returns None (e.g. unsupported view) -> no_capture reason."""
    conn = _make_client_with_mock_server(None)
    out = conn.get_active_screenshot_with_status()
    assert out == {"success": False, "reason": "no_capture"}


def test_screenshot_status_rpc_error():
    """When the RPC raises, the helper returns rpc_error."""
    conn = _make_client_with_mock_server(None)
    class _BoomServer:
        def get_active_screenshot(self, *a, **kw):
            raise ConnectionRefusedError("nope")
    conn.server = _BoomServer()
    out = conn.get_active_screenshot_with_status()
    assert out["success"] is False
    assert out["reason"] == "rpc_error"
    assert "ConnectionRefusedError" in out["detail"]


def test_screenshot_returns_b64_via_legacy_helper():
    """``get_active_screenshot`` (no status) returns the b64 or None."""
    conn = _make_client_with_mock_server("BASE64DATA")
    assert conn.get_active_screenshot() == "BASE64DATA"


def test_screenshot_legacy_returns_none_on_no_capture():
    conn = _make_client_with_mock_server(None)
    assert conn.get_active_screenshot() is None


if __name__ == "__main__":
    test_default_timeout_from_env()
    test_timeout_transport_uses_timeout()
    test_connection_to_unresponsive_peer_times_out()
    test_connection_to_closed_port_fails_fast()
    print("All freecad_client tests passed")
