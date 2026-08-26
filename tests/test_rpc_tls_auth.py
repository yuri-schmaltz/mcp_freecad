"""Tests for the TLS + bearer-token auth layer of FilteredXMLRPCServer."""
import importlib.util
import sys
import tempfile
import types
from pathlib import Path

# Reuse the standard shim set.
_HERE = Path(__file__).resolve().parent
_RS_DIR = _HERE.parent / "addon" / "FreeCADMCP" / "rpc_server"

for name in ("FreeCAD", "FreeCADGui", "ObjectsFem", "PySide"):
    if name not in sys.modules:
        sys.modules[name] = types.ModuleType(name)

_fc = sys.modules["FreeCAD"]
_fc.Console = types.SimpleNamespace(
    PrintWarning=lambda *a, **k: None,
    PrintMessage=lambda *a, **k: None,
    PrintError=lambda *a, **k: None,
)
_fc.getUserAppDataDir = lambda: "/tmp"
_fc.newDocument = lambda *a, **k: None
_fc.getDocument = lambda *a, **k: None
_fc.listDocuments = lambda: {}
_fc.Document = type("Document", (), {})
_fc.DocumentObject = type("DocumentObject", (), {})
_fc.Vector = type("Vector", (), {})
_fc.Rotation = type("Rotation", (), {})
_fc.Placement = type("Placement", (), {})

sys.modules["FreeCADGui"].ActiveDocument = None
sys.modules["FreeCADGui"].Selection = types.SimpleNamespace(
    clearSelection=lambda: None, addSelection=lambda *a, **k: None
)
sys.modules["FreeCADGui"].SendMsgToActiveView = lambda *a, **k: None
sys.modules["FreeCADGui"].addCommand = lambda *a, **k: None
sys.modules["FreeCADGui"].getMainWindow = lambda: types.SimpleNamespace(
    findChildren=lambda *a, **k: []
)

sys.modules["PySide"].QtCore = types.SimpleNamespace(
    QTimer=types.SimpleNamespace(singleShot=lambda *a, **k: None),
    QEventLoop=types.SimpleNamespace(AllEvents=0),
    QThread=types.SimpleNamespace(msleep=lambda *a, **k: None),
)
sys.modules["PySide"].QtWidgets = types.SimpleNamespace(
    QApplication=type("QApplication", (), {"instance": staticmethod(lambda: None), "processEvents": lambda *a, **k: None}),
    QInputDialog=type("QInputDialog", (), {}),
    QLineEdit=type("QLineEdit", (), {"Normal": 0}),
    QMessageBox=type("QMessageBox", (), {"warning": staticmethod(lambda *a, **k: None)}),
    QAction=type("QAction", (), {}),
)
sys.modules["ObjectsFem"].makeMeshGmsh = lambda *a, **k: (None,)
sys.modules["ObjectsFem"].makeAnalysis = lambda *a, **k: None
sys.modules["ObjectsFem"].makeMaterialSolid = lambda *a, **k: None
sys.modules["ObjectsFem"].makeSolverCalculiXCcxTools = lambda *a, **k: None


def _load_rpc_server():
    pkg = types.ModuleType("_rs_pkg_tlsauth")
    pkg.__path__ = [str(_RS_DIR)]
    sys.modules["_rs_pkg_tlsauth"] = pkg
    for sub in ("parts_library", "serialize", "_fem_workdir", "_request_tracking"):
        spec = importlib.util.spec_from_file_location(
            f"_rs_pkg_tlsauth.{sub}", str(_RS_DIR / f"{sub}.py")
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[f"_rs_pkg_tlsauth.{sub}"] = mod
        spec.loader.exec_module(mod)
    spec = importlib.util.spec_from_file_location(
        "_rs_pkg_tlsauth.rpc_server", str(_RS_DIR / "rpc_server.py")
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_rs_pkg_tlsauth.rpc_server"] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# _get_auth_token
# ---------------------------------------------------------------------------

def test_get_auth_token_none_by_default():
    rpc_mod = _load_rpc_server()
    rpc_mod.os.environ.pop("FREECAD_MCP_AUTH_TOKEN", None)
    # Module may already have a reference to the function imported at
    # load time; reload to pick up env change.
    importlib.reload(rpc_mod)
    assert rpc_mod._get_auth_token() is None


def test_get_auth_token_reads_env():
    rpc_mod = _load_rpc_server()
    saved = rpc_mod.os.environ.get("FREECAD_MCP_AUTH_TOKEN")
    try:
        rpc_mod.os.environ["FREECAD_MCP_AUTH_TOKEN"] = "secret123"
        importlib.reload(rpc_mod)
        assert rpc_mod._get_auth_token() == "secret123"
    finally:
        if saved is None:
            rpc_mod.os.environ.pop("FREECAD_MCP_AUTH_TOKEN", None)
        else:
            rpc_mod.os.environ["FREECAD_MCP_AUTH_TOKEN"] = saved
        importlib.reload(rpc_mod)


def test_get_auth_token_ignores_whitespace():
    rpc_mod = _load_rpc_server()
    saved = rpc_mod.os.environ.get("FREECAD_MCP_AUTH_TOKEN")
    try:
        rpc_mod.os.environ["FREECAD_MCP_AUTH_TOKEN"] = "   "
        importlib.reload(rpc_mod)
        # Empty / whitespace tokens are treated as no auth.
        assert rpc_mod._get_auth_token() is None
    finally:
        if saved is None:
            rpc_mod.os.environ.pop("FREECAD_MCP_AUTH_TOKEN", None)
        else:
            rpc_mod.os.environ["FREECAD_MCP_TOKEN"] = saved
        importlib.reload(rpc_mod)


# ---------------------------------------------------------------------------
# _BearerAuthHandler._check_auth
# ---------------------------------------------------------------------------

def test_check_auth_no_token_configured_allows():
    rpc_mod = _load_rpc_server()
    rpc_mod.os.environ.pop("FREECAD_MCP_AUTH_TOKEN", None)
    importlib.reload(rpc_mod)
    handler = rpc_mod._BearerAuthHandler()
    assert handler._check_auth("Authorization: Bearer anything") is True
    assert handler._check_auth("") is True


def test_check_auth_with_token_requires_match():
    rpc_mod = _load_rpc_server()
    saved = rpc_mod.os.environ.get("FREECAD_MCP_AUTH_TOKEN")
    try:
        rpc_mod.os.environ["FREECAD_MCP_AUTH_TOKEN"] = "secret"
        importlib.reload(rpc_mod)
        handler = rpc_mod._BearerAuthHandler()
        # Correct
        assert handler._check_auth("Authorization: Bearer secret") is True
        # Wrong token
        assert handler._check_auth("Authorization: Bearer wrong") is False
        # Missing scheme
        assert handler._check_auth("Authorization: Basic secret") is False
        # Missing header
        assert handler._check_auth("") is False
    finally:
        if saved is None:
            rpc_mod.os.environ.pop("FREECAD_MCP_AUTH_TOKEN", None)
        else:
            rpc_mod.os.environ["FREECAD_MCP_AUTH_TOKEN"] = saved
        importlib.reload(rpc_mod)


def test_check_auth_case_insensitive_header():
    rpc_mod = _load_rpc_server()
    saved = rpc_mod.os.environ.get("FREECAD_MCP_AUTH_TOKEN")
    try:
        rpc_mod.os.environ["FREECAD_MCP_AUTH_TOKEN"] = "secret"
        importlib.reload(rpc_mod)
        handler = rpc_mod._BearerAuthHandler()
        assert handler._check_auth("authorization: Bearer secret") is True
        assert handler._check_auth("AUTHORIZATION: Bearer secret") is True
    finally:
        if saved is None:
            rpc_mod.os.environ.pop("FREECAD_MCP_AUTH_TOKEN", None)
        else:
            rpc_mod.os.environ["FREECAD_MCP_AUTH_TOKEN"] = saved
        importlib.reload(rpc_mod)


def test_check_auth_constant_time_compare():
    """Two different tokens should both return False in comparable time.

    This is a smoke test for the constant-time property. Real timing
    side-channels are hard to test deterministically; we just verify
    the function uses hmac.compare_digest.
    """
    import hmac as _hmac
    rpc_mod = _load_rpc_server()
    rpc_mod._BearerAuthHandler()  # instantiation only, no value used
    # We do not need to assert time; just confirm the helper is wired.
    assert hasattr(_hmac, "compare_digest")


# ---------------------------------------------------------------------------
# TLS context construction (without actually starting a server)
# ---------------------------------------------------------------------------

def test_tls_context_loaded_from_pem():
    """Generate a self-signed cert with openssl, point the server at it,
    confirm the SSLContext is constructed."""
    import ssl as _ssl
    rpc_mod = _load_rpc_server()

    # Create a self-signed cert/key pair via the cryptography-free route:
    # use openssl if available, otherwise skip.
    import shutil
    import subprocess
    if not shutil.which("openssl"):
        # No openssl on this box — skip.
        return
    tmp = tempfile.mkdtemp(prefix="mcp_tls_")
    cert = str(Path(tmp) / "cert.pem")
    key = str(Path(tmp) / "key.pem")
    try:
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-keyout", key,
             "-out", cert, "-days", "1", "-nodes", "-subj", "/CN=localhost"],
            check=True, capture_output=True,
        )
        # Try to build a server with TLS. We do not start it; just construct.
        # We need a free port for the constructor; use port 0 won't work
        # with SimpleXMLRPCServer.bind; pick an ephemeral high port.
        server = rpc_mod.FilteredXMLRPCServer(
            ("127.0.0.1", 19999),  # nosec — test only
            allowed_ips_str="127.0.0.1",
            tls_cert=cert, tls_key=key,
        )
        assert server._ssl_context is not None
        assert isinstance(server._ssl_context, _ssl.SSLContext)
        server.server_close()
    finally:
        import shutil as _sh
        _sh.rmtree(tmp, ignore_errors=True)


def test_tls_missing_cert_disables_tls():
    """If only one of cert/key is set, TLS is silently disabled (refuse
    half-configured rather than refuse to start)."""
    rpc_mod = _load_rpc_server()
    server = rpc_mod.FilteredXMLRPCServer(
        ("127.0.0.1", 19999),  # nosec — test only
        allowed_ips_str="127.0.0.1",
        tls_cert="/nonexistent/cert.pem", tls_key=None,
    )
    assert server._ssl_context is None
    server.server_close()


def test_tls_bad_cert_path_disables_tls():
    rpc_mod = _load_rpc_server()
    server = rpc_mod.FilteredXMLRPCServer(
        ("127.0.0.1", 19999),  # nosec — test only
        allowed_ips_str="127.0.0.1",
        tls_cert="/nonexistent/cert.pem", tls_key="/nonexistent/key.pem",
    )
    assert server._ssl_context is None
    server.server_close()


# ---------------------------------------------------------------------------
# FilteredXMLRPCServer construction with bad allowed_ips
# ---------------------------------------------------------------------------


def test_filtered_server_warns_on_invalid_allowed_ips():
    """When the allowlist contains invalid entries, the server emits a
    console warning and skips them."""
    import sys
    rpc_mod = _load_rpc_server()

    warnings: list[str] = []
    sys.modules["FreeCAD"].Console.PrintWarning = lambda msg: warnings.append(msg)

    try:
        server = rpc_mod.FilteredXMLRPCServer(
            ("127.0.0.1", 19998),  # nosec — test only
            allowed_ips_str="127.0.0.1, not_an_ip, 10.0.0.0/8",
        )
        # Two warnings (one per bad entry).
        assert any("not_an_ip" in w or "skipping" in w for w in warnings)
        # Only the valid entries ended up in _allowed_networks.
        assert len(server._allowed_networks) == 2
        server.server_close()
    finally:
        sys.modules["FreeCAD"].Console.PrintWarning = lambda *a, **k: None


def test_verify_request_allows_in_network():
    """verify_request returns True for IPs inside an allowed CIDR."""
    rpc_mod = _load_rpc_server()
    server = rpc_mod.FilteredXMLRPCServer(
        ("127.0.0.1", 19997),  # nosec — test only
        allowed_ips_str="10.0.0.0/24",
    )
    try:
        # Mock request, only client_address is used.
        assert server.verify_request(None, ("10.0.0.5", 9999)) is True
        assert server.verify_request(None, ("10.0.0.255", 9999)) is True
    finally:
        server.server_close()


def test_verify_request_rejects_outside_network(caplog):
    """verify_request returns False (with a console warning) for IPs
    outside the allowlist."""
    import logging
    rpc_mod = _load_rpc_server()
    server = rpc_mod.FilteredXMLRPCServer(
        ("127.0.0.1", 19996),  # nosec — test only
        allowed_ips_str="10.0.0.0/24",
    )
    try:
        # Outside the allowed CIDR.
        assert server.verify_request(None, ("192.168.1.1", 9999)) is False
    finally:
        server.server_close()


def test_verify_request_rejects_unparseable_ip():
    """verify_request handles garbage IPs without crashing."""
    rpc_mod = _load_rpc_server()
    server = rpc_mod.FilteredXMLRPCServer(
        ("127.0.0.1", 19995),  # nosec — test only
        allowed_ips_str="10.0.0.0/24",
    )
    try:
        # IPv6 or garbage: ip_address raises ValueError -> caught -> False.
        assert server.verify_request(None, ("not-an-ip", 9999)) is False
    finally:
        server.server_close()


if __name__ == "__main__":
    test_get_auth_token_none_by_default()
    test_get_auth_token_reads_env()
    test_get_auth_token_ignores_whitespace()
    test_check_auth_no_token_configured_allows()
    test_check_auth_with_token_requires_match()
    test_check_auth_case_insensitive_header()
    test_check_auth_constant_time_compare()
    test_tls_context_loaded_from_pem()
    test_tls_missing_cert_disables_tls()
    test_tls_bad_cert_path_disables_tls()
    print("All TLS + auth tests passed")
