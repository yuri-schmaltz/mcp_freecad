"""Unit tests for the safe_operation decorator.

The audit prefix is **off by default** in v0.4.0 (gated on
``FREECAD_MCP_LOAD_GABARITO=1``), so error responses do NOT carry the
prefix unless the operator opted in. The previous test that asserted
the prefix on every text response has been updated accordingly.
"""
import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import freecad_mcp.responses as _responses_mod  # noqa: E402
from freecad_mcp.utils import safe_operation  # noqa: E402


@safe_operation
def boom():
    raise RuntimeError("kaboom")


@safe_operation
def ok():
    return "fine"


def _ensure_prefix_off(monkeypatch):
    """Hard-reset the responses module to the off-by-default state.

    Other test files mutate ``FREECAD_MCP_LOAD_GABARITO`` and
    ``FREECAD_MCP_NO_DIRECTIVE_PREFIX``; without reloading, the cached
    module keeps whatever the last test left in the env. Re-importing
    the module makes the helper functions re-evaluate the env.
    """
    monkeypatch.delenv("FREECAD_MCP_LOAD_GABARITO", raising=False)
    monkeypatch.delenv("FREECAD_MCP_NO_DIRECTIVE_PREFIX", raising=False)
    importlib.reload(_responses_mod)


def test_safe_operation_returns_text_on_exception(monkeypatch):
    """Default (gabarito off): no audit prefix on error responses."""
    _ensure_prefix_off(monkeypatch)
    r = boom()
    assert len(r) == 1
    assert r[0].type == "text"
    assert "Internal server error" in r[0].text
    assert "boom" in r[0].text
    # No prefix by default.
    assert not r[0].text.startswith(
        _responses_mod.SYSTEM_DIRECTIVE_PREFIX
    )


def test_safe_operation_passes_through_on_success(monkeypatch):
    _ensure_prefix_off(monkeypatch)
    r = ok()
    assert r == "fine"


def test_safe_operation_fallback_when_text_response_raises(monkeypatch):
    """If text_response itself fails inside the wrapper, fall back to a
    minimal dict-shaped response."""
    _ensure_prefix_off(monkeypatch)

    def _exploding_text_response(message):
        raise RuntimeError("text_response broken")

    # Monkey-patch the text_response imported into utils.
    import freecad_mcp.utils as utils_mod
    saved = utils_mod.text_response
    utils_mod.text_response = _exploding_text_response
    try:
        r = boom()
    finally:
        utils_mod.text_response = saved
    assert len(r) == 1
    assert r[0]["type"] == "text"
    assert "Internal error" in r[0]["text"]


if __name__ == "__main__":
    test_safe_operation_returns_text_on_exception(None)  # type: ignore[arg-type]
    test_safe_operation_passes_through_on_success(None)  # type: ignore[arg-type]
    print("All utils tests passed")
