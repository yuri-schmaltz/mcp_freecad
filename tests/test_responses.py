"""Unit tests for the response helpers.

Note: the ``SYSTEM_DIRECTIVE_PREFIX`` is **off by default** since v0.4.0.
It is enabled either by ``FREECAD_MCP_LOAD_GABARITO=1`` (canonical) or
``FREECAD_MCP_NO_DIRECTIVE_PREFIX=0``. ``FREECAD_MCP_NO_DIRECTIVE_PREFIX=1``
remains honoured as an override (forces off even when the opt-in is set).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from freecad_mcp.responses import (  # noqa: E402
    SYSTEM_DIRECTIVE_PREFIX,
    add_screenshot_if_available,
    json_response,
    text_response,
)


def _reload_responses():
    import importlib
    import freecad_mcp.responses as responses_mod
    importlib.reload(responses_mod)
    return responses_mod


def test_text_response_no_prefix_by_default():
    """Default behaviour (no env vars): no audit prefix on text responses."""
    r = text_response("hello")
    assert len(r) == 1
    assert r[0].type == "text"
    assert r[0].text == "hello"


def test_text_response_does_not_double_prefix():
    r = text_response("hello")
    msg = r[0].text
    assert msg.count(SYSTEM_DIRECTIVE_PREFIX) == 0


def test_json_response_serialises_dict():
    r = json_response({"a": 1, "b": [1, 2]})
    import json as _json
    assert _json.loads(r[0].text) == {"a": 1, "b": [1, 2]}


def test_json_response_handles_non_serialisable():
    class Opaque:
        def __repr__(self):
            return "opaque-repr"

    r = json_response({"obj": Opaque()})
    assert "opaque-repr" in r[0].text


def test_add_screenshot_none_returns_base():
    base = text_response("ok")
    out = add_screenshot_if_available(base, None, only_text_feedback=False)
    assert out is base


def test_add_screenshot_only_text_feedback_returns_base():
    base = text_response("ok")
    out = add_screenshot_if_available(base, "BASE64DATA", only_text_feedback=True)
    assert out is base


def test_add_screenshot_appends_image():
    base = text_response("ok")
    out = add_screenshot_if_available(base, "BASE64DATA", only_text_feedback=False)
    assert len(out) == 2
    text_part, img_part = out
    assert text_part.type == "text"
    assert img_part.type == "image"
    assert img_part.data == "BASE64DATA"
    assert img_part.mimeType == "image/png"


def test_add_screenshot_appends_image_jpeg():
    """v1.0.3 — image_format='jpeg' must yield mimeType='image/jpeg'."""
    base = text_response("ok")
    out = add_screenshot_if_available(
        base, "BASE64DATA", only_text_feedback=False, image_format="jpeg"
    )
    img_part = out[-1]
    assert img_part.mimeType == "image/jpeg"


def test_add_screenshot_appends_image_webp():
    """v1.0.3 — image_format='webp' must yield mimeType='image/webp'."""
    base = text_response("ok")
    out = add_screenshot_if_available(
        base, "BASE64DATA", only_text_feedback=False, image_format="webp"
    )
    img_part = out[-1]
    assert img_part.mimeType == "image/webp"


def test_add_screenshot_appends_image_jpg_alias():
    """v1.0.3 — 'jpg' is a valid alias for 'jpeg'."""
    base = text_response("ok")
    out = add_screenshot_if_available(
        base, "BASE64DATA", only_text_feedback=False, image_format="jpg"
    )
    img_part = out[-1]
    assert img_part.mimeType == "image/jpeg"


def test_add_screenshot_unknown_format_falls_back_to_png():
    """v1.0.3 — unknown image_format falls back to image/png (back-compat)."""
    base = text_response("ok")
    out = add_screenshot_if_available(
        base, "BASE64DATA", only_text_feedback=False, image_format="tiff"
    )
    img_part = out[-1]
    assert img_part.mimeType == "image/png"


def test_prefix_enabled_via_load_gabarito():
    """FREECAD_MCP_LOAD_GABARITO=1 turns the audit prefix on."""
    import os
    responses_mod = _reload_responses()
    saved_load = os.environ.get("FREECAD_MCP_LOAD_GABARITO")
    saved_no = os.environ.get("FREECAD_MCP_NO_DIRECTIVE_PREFIX")
    try:
        os.environ["FREECAD_MCP_LOAD_GABARITO"] = "1"
        # Reload so the module picks up the new env at import time.
        responses_mod = _reload_responses()
        assert responses_mod._directive_enabled() is True
        r = responses_mod.text_response("hello")
        assert r[0].text.startswith(SYSTEM_DIRECTIVE_PREFIX)
        assert "hello" in r[0].text
    finally:
        if saved_load is None:
            os.environ.pop("FREECAD_MCP_LOAD_GABARITO", None)
        else:
            os.environ["FREECAD_MCP_LOAD_GABARITO"] = saved_load
        if saved_no is not None:
            os.environ["FREECAD_MCP_NO_DIRECTIVE_PREFIX"] = saved_no
        _reload_responses()


def test_legacy_no_directive_prefix_forces_off():
    """FREECAD_MCP_NO_DIRECTIVE_PREFIX=1 still works as an override (legacy)."""
    import os
    saved_load = os.environ.get("FREECAD_MCP_LOAD_GABARITO")
    saved_no = os.environ.get("FREECAD_MCP_NO_DIRECTIVE_PREFIX")
    try:
        os.environ["FREECAD_MCP_LOAD_GABARITO"] = "1"
        os.environ["FREECAD_MCP_NO_DIRECTIVE_PREFIX"] = "1"
        responses_mod = _reload_responses()
        # Opt-in says "on", legacy override says "off" → off wins.
        assert responses_mod._directive_enabled() is False
        r = responses_mod.text_response("hello")
        assert r[0].text == "hello"
    finally:
        if saved_load is None:
            os.environ.pop("FREECAD_MCP_LOAD_GABARITO", None)
        else:
            os.environ["FREECAD_MCP_LOAD_GABARITO"] = saved_load
        if saved_no is None:
            os.environ.pop("FREECAD_MCP_NO_DIRECTIVE_PREFIX", None)
        else:
            os.environ["FREECAD_MCP_NO_DIRECTIVE_PREFIX"] = saved_no
        _reload_responses()


def test_load_gabarito_accepts_truthy_values():
    """FREECAD_MCP_LOAD_GABARITO accepts 'true'/'yes'/'on' (case-insensitive)."""
    import os
    saved = os.environ.get("FREECAD_MCP_LOAD_GABARITO")
    saved_no = os.environ.get("FREECAD_MCP_NO_DIRECTIVE_PREFIX")
    try:
        for truthy in ("1", "true", "TRUE", "yes", "on", "On"):
            os.environ["FREECAD_MCP_LOAD_GABARITO"] = truthy
            os.environ.pop("FREECAD_MCP_NO_DIRECTIVE_PREFIX", None)
            responses_mod = _reload_responses()
            assert responses_mod._directive_enabled() is True, f"failed for {truthy!r}"
    finally:
        if saved is None:
            os.environ.pop("FREECAD_MCP_LOAD_GABARITO", None)
        else:
            os.environ["FREECAD_MCP_LOAD_GABARITO"] = saved
        if saved_no is not None:
            os.environ["FREECAD_MCP_NO_DIRECTIVE_PREFIX"] = saved_no
        _reload_responses()


if __name__ == "__main__":
    test_text_response_no_prefix_by_default()
    test_text_response_does_not_double_prefix()
    test_json_response_serialises_dict()
    test_json_response_handles_non_serialisable()
    test_add_screenshot_none_returns_base()
    test_add_screenshot_only_text_feedback_returns_base()
    test_add_screenshot_appends_image()
    test_prefix_enabled_via_load_gabarito()
    test_legacy_no_directive_prefix_forces_off()
    test_load_gabarito_accepts_truthy_values()
    print("All response tests passed")
