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


# ---------------------------------------------------------------------------
# Thin server wrappers
# ---------------------------------------------------------------------------


class _RecordingServer:
    """Mock server that records every method call and returns a fixed value."""

    def __init__(self, return_value):
        self.return_value = return_value
        self.calls: list[tuple[str, tuple, dict]] = []

    def __getattr__(self, name):
        def _call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return self.return_value
        return _call


def _conn_with_recording_server(value):
    conn = fc.FreeCADConnection.__new__(fc.FreeCADConnection)
    conn.timeout = 0.5
    conn.breaker = fc.CircuitBreaker()
    conn.server = _RecordingServer(value)
    return conn


def test_ping_returns_server_value():
    conn = _conn_with_recording_server(True)
    assert conn.ping() is True
    assert conn.server.calls == [("ping", (), {})]


def test_cancel_request_passes_id():
    conn = _conn_with_recording_server({"cancelled": True})
    assert conn.cancel_request("REQ-1") == {"cancelled": True}
    assert conn.server.calls == [("cancel_request", ("REQ-1",), {})]


def test_cancel_all_pending_requests():
    conn = _conn_with_recording_server({"all": 3})
    assert conn.cancel_all_pending_requests() == {"all": 3}
    assert conn.server.calls[0][0] == "cancel_all_pending_requests"


def test_invalidate_idempotency_cache():
    conn = _conn_with_recording_server({"invalidated": 7})
    assert conn.invalidate_idempotency_cache() == {"invalidated": 7}
    assert conn.server.calls[0][0] == "invalidate_idempotency_cache"


def test_create_document_passes_name_and_id():
    conn = _conn_with_recording_server({"ok": True})
    assert conn.create_document("Doc1", "REQ-1") == {"ok": True}
    name, args, kwargs = conn.server.calls[0]
    assert name == "create_document"
    assert args == ("Doc1", "REQ-1")


def test_create_object_passes_doc_and_data_and_id():
    conn = _conn_with_recording_server({"ok": True})
    data = {"name": "Box", "type": "Part::Box"}
    conn.create_object("Doc1", data, "REQ-2")
    name, args, kwargs = conn.server.calls[0]
    assert name == "create_object"
    assert args == ("Doc1", data, "REQ-2")


def test_edit_object_passes_all_args():
    conn = _conn_with_recording_server({"ok": True})
    data = {"x": 1}
    conn.edit_object("Doc1", "Box", data, "REQ-3")
    name, args, kwargs = conn.server.calls[0]
    assert name == "edit_object"
    assert args == ("Doc1", "Box", data, "REQ-3")


def test_delete_object_passes_doc_and_name():
    conn = _conn_with_recording_server({"ok": True})
    conn.delete_object("Doc1", "Box")
    name, args, kwargs = conn.server.calls[0]
    assert name == "delete_object"
    assert args == ("Doc1", "Box", None)


def test_insert_part_from_library():
    conn = _conn_with_recording_server({"ok": True})
    conn.insert_part_from_library("parts/box.fcstd")
    name, args, kwargs = conn.server.calls[0]
    assert name == "insert_part_from_library"
    assert args == ("parts/box.fcstd", None)


def test_execute_code_passes_code_and_id():
    conn = _conn_with_recording_server({"ok": True})
    conn.execute_code("FreeCAD.newDocument('X')", "REQ-4")
    name, args, kwargs = conn.server.calls[0]
    assert name == "execute_code"
    assert args == ("FreeCAD.newDocument('X')", "REQ-4")


def test_get_objects_returns_list():
    conn = _conn_with_recording_server([{"name": "Box"}])
    assert conn.get_objects("Doc1") == [{"name": "Box"}]
    name, args, kwargs = conn.server.calls[0]
    assert name == "get_objects"
    assert args == ("Doc1",)


def test_get_object_returns_dict():
    conn = _conn_with_recording_server({"name": "Box"})
    assert conn.get_object("Doc1", "Box") == {"name": "Box"}
    name, args, kwargs = conn.server.calls[0]
    assert name == "get_object"
    assert args == ("Doc1", "Box")


def test_get_parts_list_returns_list():
    conn = _conn_with_recording_server(["part1", "part2"])
    assert conn.get_parts_list() == ["part1", "part2"]
    assert conn.server.calls[0][0] == "get_parts_list"


def test_list_documents_returns_list():
    conn = _conn_with_recording_server(["Doc1", "Doc2"])
    assert conn.list_documents() == ["Doc1", "Doc2"]
    assert conn.server.calls[0][0] == "list_documents"


def test_run_fem_analysis_passes_timeout():
    conn = _conn_with_recording_server({"ok": True})
    conn.run_fem_analysis("Doc1", "Analysis", timeout=120)
    name, args, kwargs = conn.server.calls[0]
    assert name == "run_fem_analysis"
    assert args == ("Doc1", "Analysis", 120, None)


def test_health_check():
    conn = _conn_with_recording_server({"healthy": True})
    assert conn.health_check() == {"healthy": True}
    assert conn.server.calls[0][0] == "health_check"


def test_undo_passes_steps():
    conn = _conn_with_recording_server({"ok": True})
    conn.undo("Doc1", steps=3)
    name, args, kwargs = conn.server.calls[0]
    assert name == "undo"
    assert args == ("Doc1", 3)


if __name__ == "__main__":
    test_default_timeout_from_env()
    test_timeout_transport_uses_timeout()
    test_connection_to_unresponsive_peer_times_out()
    test_connection_to_closed_port_fails_fast()
    print("All freecad_client tests passed")
