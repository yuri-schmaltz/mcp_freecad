"""Tests for the pure IP allowlist parser.

The parser lives in ``addon/FreeCADMCP/rpc_server/_ip_allowlist.py``
and was extracted from ``rpc_server.py`` in v1.0.3 so the rules can
be exercised without spinning up FreeCAD / PySide. These tests load
the module directly.
"""
import importlib.util
import ipaddress
import sys
import types
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_RS_DIR = _HERE.parent / "addon" / "FreeCADMCP" / "rpc_server"

# FreeCAD / PySide / ObjectsFem stubs (the parser only imports stdlib
# ``ipaddress`` and ``re`` but the package needs the names present).
for name in ("FreeCAD", "FreeCADGui", "ObjectsFem", "PySide"):
    if name not in sys.modules:
        sys.modules[name] = types.ModuleType(name)


def _load_allowlist_module():
    """Load ``_ip_allowlist.py`` as a free-standing module."""
    # First put it under a synthetic package so ``__future__`` and
    # relative imports behave normally.
    pkg_name = "_test_ip_allowlist_pkg"
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [str(_RS_DIR)]
    sys.modules[pkg_name] = pkg
    spec = importlib.util.spec_from_file_location(
        f"{pkg_name}._ip_allowlist", str(_RS_DIR / "_ip_allowlist.py")
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"{pkg_name}._ip_allowlist"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


mod = _load_allowlist_module()


# ---------------------------------------------------------------------------
# parse_allowlist
# ---------------------------------------------------------------------------

def test_empty_input():
    valid, errors = mod.parse_allowlist("")
    assert valid == []
    assert "empty" in errors[0].lower()


def test_whitespace_only_input():
    valid, errors = mod.parse_allowlist("   \t  ")
    assert valid == []
    assert errors


def test_none_input():
    valid, errors = mod.parse_allowlist(None)
    assert valid == []
    assert errors


def test_single_valid_v4():
    valid, errors = mod.parse_allowlist("127.0.0.1")
    assert valid == ["127.0.0.1"]
    assert errors == []


def test_single_valid_v4_with_cidr():
    valid, errors = mod.parse_allowlist("192.168.0.0/24")
    assert valid == ["192.168.0.0/24"]
    assert errors == []


def test_single_valid_v6():
    valid, errors = mod.parse_allowlist("::1")
    assert valid == ["::1"]
    assert errors == []


def test_multiple_valid():
    valid, errors = mod.parse_allowlist("127.0.0.1, 192.168.0.0/24, 10.0.0.5")
    assert valid == ["127.0.0.1", "192.168.0.0/24", "10.0.0.5"]
    assert errors == []


def test_ipv4_only_entry_host_bits_are_tolerated():
    """``ip_network(strict=False)`` tolerates host bits set; we do too."""
    valid, errors = mod.parse_allowlist("192.168.0.5/24")
    assert valid == ["192.168.0.5/24"]
    assert errors == []


# ---------------------------------------------------------------------------
# malformed-list detection
# ---------------------------------------------------------------------------

def test_leading_comma_rejected():
    valid, errors = mod.parse_allowlist(",127.0.0.1")
    assert valid == []
    assert errors and "malformed" in errors[0].lower()


def test_trailing_comma_rejected():
    valid, errors = mod.parse_allowlist("127.0.0.1,")
    assert valid == []
    assert errors and "malformed" in errors[0].lower()


def test_double_comma_rejected():
    valid, errors = mod.parse_allowlist("127.0.0.1,,192.168.0.1")
    assert valid == []
    assert errors and "malformed" in errors[0].lower()


def test_whitespace_inside_entry_rejected():
    r"""An entry with internal whitespace is not a valid IP; the whole
    list is rejected as malformed (the regex disallows ``\s`` inside
    an entry)."""
    valid, errors = mod.parse_allowlist("127.0.0. 1")
    assert valid == []
    assert errors


def test_mixed_valid_and_malformed_list_reports_malformed():
    """``1.2.3.4, ,5.6.7.7`` — the regex match fails because of the
    double comma; we report malformed-list, not per-entry errors."""
    valid, errors = mod.parse_allowlist("1.2.3.4,,5.6.7.7")
    assert valid == []
    assert errors


# ---------------------------------------------------------------------------
# wildcard rejection
# ---------------------------------------------------------------------------

def test_v4_wildcard_rejected():
    valid, errors = mod.parse_allowlist("0.0.0.0/0")
    assert valid == []
    assert errors and "wildcard" in errors[0].lower()


def test_v6_wildcard_rejected():
    valid, errors = mod.parse_allowlist("::/0")
    assert valid == []
    assert errors and "wildcard" in errors[0].lower()


def test_wildcard_alongside_valid_reports_both():
    valid, errors = mod.parse_allowlist("127.0.0.1, 0.0.0.0/0")
    assert "127.0.0.1" in valid
    assert any("wildcard" in e.lower() for e in errors)


# ---------------------------------------------------------------------------
# per-entry error reporting
# ---------------------------------------------------------------------------

def test_invalid_entry_reported_as_error():
    valid, errors = mod.parse_allowlist("not-an-ip")
    assert valid == []
    assert errors and "invalid" in errors[0].lower()


def test_valid_and_invalid_entries():
    valid, errors = mod.parse_allowlist("127.0.0.1, not-an-ip, 192.168.0.0/24")
    assert valid == ["127.0.0.1", "192.168.0.0/24"]
    assert any("not-an-ip" in e for e in errors)


# ---------------------------------------------------------------------------
# parse_allowlist_to_networks
# ---------------------------------------------------------------------------

def test_networks_returns_ip_network_objects():
    nets = mod.parse_allowlist_to_networks("127.0.0.1, 192.168.0.0/24")
    assert len(nets) == 2
    assert all(isinstance(n, ipaddress._BaseNetwork) for n in nets)
    assert nets[0] == ipaddress.ip_network("127.0.0.1")
    assert nets[1] == ipaddress.ip_network("192.168.0.0/24")


def test_networks_skips_wildcards_with_warning_callback():
    captured: list[str] = []
    nets = mod.parse_allowlist_to_networks(
        "127.0.0.1, 0.0.0.0/0",
        on_warning=captured.append,
    )
    assert len(nets) == 1
    assert nets[0] == ipaddress.ip_network("127.0.0.1")
    assert captured and "wildcard" in captured[0].lower()


def test_networks_no_callback_uses_logger(caplog):
    """When ``on_warning`` is None, the parser logs at WARNING level."""
    import logging
    caplog.set_level(logging.WARNING, logger="FreeCADMCPip_allowlist")
    mod.parse_allowlist_to_networks("0.0.0.0/0")
    assert any("wildcard" in r.getMessage().lower() for r in caplog.records)


def test_backcompat_alias_parses():
    """``_parse_allowed_ips`` is the legacy name; it must still work."""
    nets = mod._parse_allowed_ips("127.0.0.1")
    assert len(nets) == 1
    assert nets[0] == ipaddress.ip_network("127.0.0.1")


# ---------------------------------------------------------------------------
# public surface
# ---------------------------------------------------------------------------

def test_public_exports():
    assert "parse_allowlist" in mod.__all__
    assert "parse_allowlist_to_networks" in mod.__all__
    assert "_parse_allowed_ips" in mod.__all__
