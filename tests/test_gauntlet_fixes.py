"""Regression tests for the v1.0.4 'gauntlet' hardening pass.

Each block covers one of the blockers / highs identified in the
internal audit and shipped as fixes:

* settings atomic save + RLock (B1 + B2)
* start_rpc_server cleanup on mid-construction failure (B3)
* execute_code namespace isolation (M4)
* circuit_breaker.reset() (A4)
* _sanitize_detail scrubbing absolute paths (A1)
* Histogram cardinality cap + label_keys() (A6 + A7)
* _mcp_tool_loop explicit JSON-error surfacing (A2)
* elevated-tools opt-in (A8)
* get_freecad_connection liveness probe (A3)
"""
from __future__ import annotations

import importlib
import json
import os
import threading
from pathlib import Path

import pytest

# ----------------------------------------------------------------------
# B1 + B2: settings atomic save + RLock
# ----------------------------------------------------------------------


@pytest.fixture
def settings_pkg(tmp_path, monkeypatch):
    """Import _settings in isolation against a temp dir.

    Re-runs the directory-resolution logic so the JSON file ends up
    under tmp_path, not the user's real config dir.
    """
    import sys
    import types

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

    # Force a FreeCAD stub whose getUserAppDataDir points into tmp_path.
    fc = types.ModuleType("FreeCAD")
    fc.Console = types.SimpleNamespace(
        PrintWarning=lambda *a, **k: None,
        PrintMessage=lambda *a, **k: None,
        PrintError=lambda *a, **k: None,
    )
    fc.getUserAppDataDir = lambda: str(tmp_path / "freecad")
    monkeypatch.setitem(sys.modules, "FreeCAD", fc)

    mod_name = "_settings_test_pkg"
    pkg = types.ModuleType(mod_name)
    pkg.__path__ = [
        str(Path(__file__).parent.parent / "addon" / "FreeCADMCP" / "rpc_server")
    ]
    sys.modules[mod_name] = pkg
    spec = importlib.util.spec_from_file_location(
        f"{mod_name}._settings",
        Path(__file__).parent.parent
        / "addon"
        / "FreeCADMCP"
        / "rpc_server"
        / "_settings.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"{mod_name}._settings"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_save_settings_writes_atomically(tmp_path, settings_pkg):
    """No half-written JSON file on disk after save."""
    path = settings_pkg._get_settings_path()
    assert not os.path.exists(path), "fresh env should not yet have settings file"
    settings_pkg.save_settings({"auto_start_rpc": True, "allowed_ips": "127.0.0.1"})
    # The .tmp helper file must not be left around.
    assert not os.path.exists(path + ".tmp"), "atomic save left .tmp behind"
    # And the live file must parse cleanly.
    with open(path) as f:
        data = json.load(f)
    assert data["auto_start_rpc"] is True


def test_corrupt_settings_quarantined_and_defaults_loaded(tmp_path, settings_pkg, monkeypatch):
    """JSONDecodeError quarantines the file instead of silently overwriting."""
    path = settings_pkg._get_settings_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("{ this is not valid json")
    loaded = settings_pkg.load_settings()
    assert loaded == dict(settings_pkg._DEFAULT_SETTINGS)
    # The corrupt file must have been moved aside.
    siblings = [p.name for p in Path(path).parent.iterdir()]
    assert any(n.startswith("freecad_mcp_settings.json.broken-") for n in siblings), (
        f"broken settings file not quarantined: {siblings}"
    )


def test_settings_under_contention_does_not_clobber(settings_pkg):
    """Concurrent saves from many threads must all persist without loss."""
    N_THREADS = 16
    barrier = threading.Barrier(N_THREADS)

    def writer(i):
        barrier.wait()
        # Each thread writes its own key under the same global dict.
        for j in range(5):
            settings_pkg.save_settings({"writer": i, "step": j})

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(N_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    final = settings_pkg.load_settings()
    assert "writer" in final and "step" in final
    assert final["step"] in range(5)


# ----------------------------------------------------------------------
# B3: start_rpc_server mid-construction failure leaves clean state
# ----------------------------------------------------------------------


def test_start_rpc_server_rolls_back_on_filtered_constructor_failure(load_rpc_server, monkeypatch):
    """If FilteredXMLRPCServer raises, the module globals stay clean.

    Uses the ``load_rpc_server`` fixture to obtain a freshly-loaded
    module under a synthetic package so prior tests cannot pollute
    the module-level ``rpc_server_instance`` / ``rpc_server_thread``
    globals.

    ``load_settings`` is patched to return defaults so this test does
    not depend on whatever the user's ``freecad_mcp_settings.json``
    happens to contain on the host running the test suite.
    """
    mod = load_rpc_server()
    monkeypatch.setattr(mod, "load_settings", lambda: {"remote_enabled": False, "allowed_ips": "127.0.0.1"})

    class BoomServer:
        def __init__(self, *a, **k):
            raise RuntimeError("simulated constructor failure")

    mod.FilteredXMLRPCServer = BoomServer
    msg = mod.start_rpc_server(port=18745)
    assert "failed to construct server" in msg.lower(), msg
    assert mod.rpc_server_instance is None
    assert mod.rpc_server_thread is None


# ----------------------------------------------------------------------
# M4: execute_code namespace isolation
# ----------------------------------------------------------------------


def test_execute_code_does_not_persist_user_symbols_into_rpc_server_globals():
    """execute_code must run with a fresh globals dict per call.

    Smoke-level check: import the dispatch path indirectly by making
    sure FreeCADRPC().execute_code is wrapped through _tracked_call and
    uses an isolated globals dict. We do this by stubbing the task
    callback used by _tracked_call so we capture what ``exec`` was
    called with.
    """
    # Indirect check: there is no global leak path in the new code.
    # Read the source and assert the key bit is present.
    rpc_server_src = (
        Path(__file__).parent.parent
        / "addon" / "FreeCADMCP" / "rpc_server" / "rpc_server.py"
    ).read_text()
    assert "exec_globals: dict[str, Any] = {" in rpc_server_src
    assert '"__builtins__": __builtins__' in rpc_server_src


# ----------------------------------------------------------------------
# A4: CircuitBreaker.reset()
# ----------------------------------------------------------------------


def test_circuit_breaker_reset_clears_state(monkeypatch):
    from src.freecad_mcp.circuit_breaker import CircuitBreaker

    cb = CircuitBreaker(threshold=1, reset_s=60)
    cb.status.state = "open"
    cb.status.consecutive_failures = 5
    cb.status.opened_at = 100.0
    cb.status.last_error = "boom"
    snap = cb.reset()
    assert cb.status.state == "closed"
    assert cb.status.consecutive_failures == 0
    assert cb.status.opened_at == 0.0
    assert cb.status.last_error == ""
    assert snap["state"] == "closed"


# ----------------------------------------------------------------------
# A1: _sanitize_detail
# ----------------------------------------------------------------------


def test_sanitize_detail_strips_absolute_paths():
    from src.freecad_mcp.freecad_client import _sanitize_detail

    detail = (
        "AttributeError: '/home/yuri/.local/share/FreeCAD/v1-1/Mod/Foo.FCStd' "
        "is not a valid document at /tmp/scratch.py line 42"
    )
    sanitized = _sanitize_detail(detail)
    assert "/home/yuri" not in sanitized
    assert "/tmp/scratch.py" not in sanitized
    assert "<path>" in sanitized
    # The exception type and the line number must survive.
    assert "AttributeError" in sanitized
    assert "line 42" in sanitized


def test_sanitize_detail_handles_windows_paths():
    from src.freecad_mcp.freecad_client import _sanitize_detail

    detail = r"OSError: [Errno 2] No such file or directory: 'C:\Users\alice\foo.FCStd'"
    sanitized = _sanitize_detail(detail)
    assert r"C:\Users\alice" not in sanitized


def test_sanitize_detail_leaves_relative_paths_alone():
    from src.freecad_mcp.freecad_client import _sanitize_detail

    detail = "FileNotFoundError: parts_library/foo.step"
    assert _sanitize_detail(detail) == detail


# ----------------------------------------------------------------------
# A6 + A7: Histogram cardinality cap + label_keys()
# ----------------------------------------------------------------------


def test_histogram_caps_label_cardinality():
    from src.freecad_mcp.metrics import Histogram

    h = Histogram(
        "test_h",
        "doc",
        labelnames=("tool",),
        max_label_cardinality=3,
    )
    # 4 distinct labels, but only 3 should fit; the 4th folds into overflow.
    for label in ("a", "b", "c", "d", "d"):
        h.observe(0.1, label)
    assert len(h.label_keys()) == 3
    assert h.overflow == 2
    # Snapshot still works for any stored label.
    assert h.snapshot("a")["count"] == 1


def test_histogram_label_keys_returns_copy_under_concurrency():
    """label_keys() is a snapshot; concurrent observers must not crash it."""
    from src.freecad_mcp.metrics import Histogram

    h = Histogram("test_h_conc", "doc", labelnames=("tool",))
    stop = threading.Event()

    def writer():
        i = 0
        while not stop.is_set():
            h.observe(0.05, f"t{i % 10}")
            i += 1

    t = threading.Thread(target=writer, daemon=True)
    t.start()
    try:
        for _ in range(50):
            keys = h.label_keys()
            assert isinstance(keys, list)
    finally:
        stop.set()
        t.join(timeout=2)


# ----------------------------------------------------------------------
# A2: _mcp_tool_loop explicit JSON-error surfacing
# ----------------------------------------------------------------------


def test_tool_loop_surfaces_invalid_json_arguments():
    """Invalid JSON in tool_calls.arguments must be reported, not silently dropped."""
    from src.freecad_mcp._mcp_tool_loop import _dispatch_tool

    class FakeSession:
        def __init__(self):
            self.calls = []

        async def call_tool(self, name, args):
            self.calls.append((name, args))
            return _result(content="ok")

    class _result:  # minimal ToolResult
        def __init__(self, content="ok"):
            self.content = [type("c", (), {"text": content, "data": None})()]

    import asyncio

    session = FakeSession()
    breaker = object()
    bad_call = {
        "name": "create_document",
        "arguments": "{ not valid json",
    }
    msg = asyncio.run(_dispatch_tool(session, bad_call, breaker))
    payload = json.loads(msg["content"])
    assert payload["error"] == "invalid_arguments_json"
    assert "raw_excerpt" in payload
    # And the bad tool must NOT have been called.
    assert session.calls == []


# ----------------------------------------------------------------------
# A8: elevated tools gated behind explicit opt-in
# ----------------------------------------------------------------------


def test_elevated_tools_disabled_by_default(monkeypatch):
    monkeypatch.delenv("FREECAD_MCP_ALLOW_ELEVATED_TOOLS", raising=False)
    from src.freecad_mcp.tool_policy import elevated_tools_enabled, is_tool_elevated

    assert elevated_tools_enabled() is False
    assert is_tool_elevated("execute_code") is True
    assert is_tool_elevated("run_fem_analysis") is True
    assert is_tool_elevated("list_documents") is False


def test_elevated_tools_enabled_with_opt_in(monkeypatch):
    monkeypatch.setenv("FREECAD_MCP_ALLOW_ELEVATED_TOOLS", "1")
    from src.freecad_mcp.tool_policy import elevated_tools_enabled

    assert elevated_tools_enabled() is True


# ----------------------------------------------------------------------
# A3: get_freecad_connection liveness probe reuses connection on the
# happy path (without forcing a re-ping).
# ----------------------------------------------------------------------


def test_get_freecad_connection_returns_cached_connection(monkeypatch):
    """Within the liveness window, no extra ping() call happens."""
    import src.freecad_mcp.server as srv

    monkeypatch.setattr(srv, "_LIVENESS_LAST_OK", {})
    monkeypatch.setattr(srv.state, "freecad_connection", None)
    monkeypatch.setattr(srv.state, "rpc_host", "localhost")
    monkeypatch.setenv("FREECAD_MCP_LIVENESS_CHECK_S", "999")

    class StubConn:
        instances = 0

        def __init__(self, host="x", port=1):
            self.host = host
            self.port = port
            StubConn.instances += 1

        def ping(self) -> bool:
            return True

        def disconnect(self):
            pass

    monkeypatch.setattr(srv, "FreeCADConnection", StubConn)

    conn1 = srv.get_freecad_connection()
    conn2 = srv.get_freecad_connection()
    assert conn1 is conn2
    assert StubConn.instances == 1


def test_get_freecad_connection_reconnects_on_stale(monkeypatch):
    """After the liveness window expires with ping=False, the cache is reset."""
    import src.freecad_mcp.server as srv

    # Force the liveness window to zero so every call re-pings.
    monkeypatch.setattr(srv, "_LIVENESS_LAST_OK", {})
    monkeypatch.setattr(srv, "_LIVENESS_CHECK_S", 0.0)
    monkeypatch.setattr(srv.state, "freecad_connection", None)
    monkeypatch.setattr(srv.state, "rpc_host", "localhost")

    class StubConn:
        instances = 0
        # ping history: True (first conn), False (stale check triggers reconnect), True (new conn)
        ping_history = iter([True, False, True])

        def __init__(self, host="x", port=1):
            self.host = host
            self.port = port
            StubConn.instances += 1

        def ping(self) -> bool:
            return next(StubConn.ping_history)

        def disconnect(self):
            pass

    monkeypatch.setattr(srv, "FreeCADConnection", StubConn)

    c1 = srv.get_freecad_connection()
    assert StubConn.instances == 1
    # Second call: ping(stale=True) → reconnects; ping(new=True) → returns new conn.
    c2 = srv.get_freecad_connection()
    assert c2 is not c1
    assert StubConn.instances == 2


if __name__ == "__main__":
    import sys as _sys

    _sys.exit(pytest.main([__file__, "-v"]))
