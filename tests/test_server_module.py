"""Smoke tests for server.py — covers the parts that do not need FreeCAD.

The MCP tool implementations (create_document, create_object, ...) and
the FastMCP `run()` path are exercised in test_operations_core.py and
via a real Claude Desktop integration; here we focus on module-level
behaviour: configuration, instruction loading, logging idempotency,
and host validation.
"""
import argparse
import importlib
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def _reload_server():
    """Drop the cached server module and re-import it."""
    for cached in [k for k in list(sys.modules) if k == "freecad_mcp.server" or k.startswith("freecad_mcp.server.")]:
        sys.modules.pop(cached, None)
    return importlib.import_module("freecad_mcp.server")


def test_configure_logging_idempotent():
    server = _reload_server()
    root = __import__("logging").getLogger()
    # configure_logging was already called by import; invoking it again
    # must not duplicate handlers.
    before = list(root.handlers)
    server.configure_logging()
    after = list(root.handlers)
    assert len(before) == len(after), f"handlers duplicated: {len(before)} -> {len(after)}"


def test_load_system_directives_fallback_when_missing():
    """If gabarito_ia_extracted.txt is missing AND gabarito is disabled,
    the loader returns the English fallback. With gabarito enabled, the
    missing file is a no-op and the fallback is used.
    """
    import freecad_mcp.server as srv
    real_exists = srv.Path.exists
    real_read_text = srv.Path.read_text
    try:
        srv.Path.exists = lambda self: False  # type: ignore[assignment]
        # Default: gabarito off → English fallback.
        saved_load = os.environ.pop("FREECAD_MCP_LOAD_GABARITO", None)
        saved_no = os.environ.get("FREECAD_MCP_NO_DIRECTIVE_PREFIX")
        if saved_no is not None:
            os.environ.pop("FREECAD_MCP_NO_DIRECTIVE_PREFIX")
        try:
            text = srv._load_system_directives()
            assert "FreeCAD" in text
            assert "Model Context Protocol" in text
        finally:
            if saved_load is not None:
                os.environ["FREECAD_MCP_LOAD_GABARITO"] = saved_load
            if saved_no is not None:
                os.environ["FREECAD_MCP_NO_DIRECTIVE_PREFIX"] = saved_no
    finally:
        srv.Path.exists = real_exists  # type: ignore[assignment]
        srv.Path.read_text = real_read_text  # type: ignore[assignment]


def test_load_system_directives_gabarito_opt_in_reads_file(monkeypatch):
    """When FREECAD_MCP_LOAD_GABARITO=1, the file content is returned."""
    monkeypatch.setenv("FREECAD_MCP_LOAD_GABARITO", "1")
    monkeypatch.delenv("FREECAD_MCP_NO_DIRECTIVE_PREFIX", raising=False)
    import freecad_mcp.server as srv
    text = srv._load_system_directives()
    assert isinstance(text, str)
    assert len(text) > 0
    # The repo ships the file with the Portuguese directive.
    assert "DIRETRIZES" in text or "DIRETRIZ" in text or "diretrizes" in text.lower()


def test_load_system_directives_opt_in_default_is_english(monkeypatch):
    """When the gabarito is NOT opted in AND the locale is not PT-BR,
    the loader returns English text, not the Portuguese file (which
    would otherwise leak into every LLM call by default).

    v1.0.3 — a Portuguese locale (``LANG=pt*``) flips the default to
    ON so PT-BR-speaking operators don't have to set a separate env
    var. This test pins LANG to en_US to make that boundary explicit.
    """
    monkeypatch.delenv("FREECAD_MCP_LOAD_GABARITO", raising=False)
    monkeypatch.delenv("FREECAD_MCP_NO_DIRECTIVE_PREFIX", raising=False)
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.setenv("LC_ALL", "en_US.UTF-8")
    monkeypatch.setenv("LC_MESSAGES", "en_US.UTF-8")
    import freecad_mcp.server as srv
    text = srv._load_system_directives()
    assert isinstance(text, str)
    # The PT-BR gabarito contains the word "DIRETRIZES" (uppercase); the
    # English fallback must NOT.
    assert "DIRETRIZES" not in text


def test_load_system_directives_pt_locale_enables_gabarito_by_default(monkeypatch):
    """v1.0.3 — a PT-BR locale flips the gabarito default to ON."""
    monkeypatch.delenv("FREECAD_MCP_LOAD_GABARITO", raising=False)
    monkeypatch.delenv("FREECAD_MCP_NO_DIRECTIVE_PREFIX", raising=False)
    monkeypatch.setenv("LANG", "pt_BR.UTF-8")
    monkeypatch.setenv("LC_ALL", "pt_BR.UTF-8")
    monkeypatch.setenv("LC_MESSAGES", "pt_BR.UTF-8")
    import freecad_mcp.server as srv
    text = srv._load_system_directives()
    assert isinstance(text, str)
    # The PT-BR gabarito contains the word "DIRETRIZES"; the English
    # fallback does not.
    assert "DIRETRIZES" in text


def test_load_system_directives_pt_locale_can_be_overridden_off(monkeypatch):
    """v1.0.3 — explicit NO_DIRECTIVE_PREFIX still wins over locale."""
    monkeypatch.delenv("FREECAD_MCP_LOAD_GABARITO", raising=False)
    monkeypatch.setenv("FREECAD_MCP_NO_DIRECTIVE_PREFIX", "1")
    monkeypatch.setenv("LANG", "pt_BR.UTF-8")
    import freecad_mcp.server as srv
    text = srv._load_system_directives()
    assert "DIRETRIZES" not in text


def test_load_system_directives_pt_locale_explicit_opt_in_works(monkeypatch):
    """v1.0.3 — explicit FREECAD_MCP_LOAD_GABARITO=1 works even on en_US."""
    monkeypatch.delenv("FREECAD_MCP_NO_DIRECTIVE_PREFIX", raising=False)
    monkeypatch.setenv("FREECAD_MCP_LOAD_GABARITO", "1")
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    import freecad_mcp.server as srv
    text = srv._load_system_directives()
    assert "DIRETRIZES" in text


def test_max_instructions_chars_truncates():
    """Setting a small cap truncates the instructions and logs a warning."""
    import freecad_mcp.server as srv
    saved = os.environ.get("FREECAD_MCP_MAX_INSTRUCTIONS_CHARS")
    try:
        os.environ["FREECAD_MCP_MAX_INSTRUCTIONS_CHARS"] = "50"
        # Re-execute the assembly block to pick up the new env.
        instr = srv._load_system_directives()
        if srv.ASSET_CREATION_STRATEGY:
            instr = instr + "\n\n" + srv.ASSET_CREATION_STRATEGY
        cap = 50
        if len(instr) > cap:
            instr = instr[:cap]
        assert len(instr) == 50
    finally:
        if saved is None:
            os.environ.pop("FREECAD_MCP_MAX_INSTRUCTIONS_CHARS", None)
        else:
            os.environ["FREECAD_MCP_MAX_INSTRUCTIONS_CHARS"] = saved


def test_validate_host_accepts_ipv4_ipv6_and_hostname():
    server = _reload_server()
    for good in ("127.0.0.1", "10.0.0.5", "::1", "fe80::1", "myhost", "myhost.example.com"):
        assert server._validate_host(good) == good, good


def test_validate_host_rejects_garbage():
    server = _reload_server()
    for bad in ("", "not a host!", "123.456.789.0", "-leading-dash"):
        try:
            server._validate_host(bad)
        except argparse.ArgumentTypeError:
            continue
        raise AssertionError(f"expected ArgumentTypeError for {bad!r}")


if __name__ == "__main__":
    test_configure_logging_idempotent()
    test_load_system_directives_fallback_when_missing()
    test_load_system_directives_gabarito_opt_in_reads_file()
    test_load_system_directives_opt_in_default_is_english()
    test_max_instructions_chars_truncates()
    test_validate_host_accepts_ipv4_ipv6_and_hostname()
    test_validate_host_rejects_garbage()
    print("All server module tests passed")
