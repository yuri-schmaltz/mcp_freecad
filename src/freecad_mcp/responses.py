import json
import os
from dataclasses import dataclass

try:
    from mcp.types import ImageContent, TextContent  # type: ignore
except Exception:
    @dataclass  # type: ignore[no-redef]
    class TextContent:
        type: str
        text: str

    @dataclass  # type: ignore[no-redef]
    class ImageContent:
        type: str
        data: str
        mimeType: str

ToolResponse = list[TextContent | ImageContent]


# System prompt loaded from docs/gabarito_ia_extracted.txt (a Portuguese
# directive set) is opt-in starting in v0.4.0. The previous default of
# "always include the audit prefix" was a demo behaviour that made
# English-language deployments ship a Portuguese sentence in every
# tool response. Operators who need the original behaviour can opt in
# via ``FREECAD_MCP_LOAD_GABARITO=1``.
SYSTEM_DIRECTIVE_PREFIX = "Analisei o documento e usarei suas instruções em minhas respostas."

# Backward-compat alias: previous releases used
# ``FREECAD_MCP_NO_DIRECTIVE_PREFIX=1`` to *disable* the prefix when it
# was on by default. We honour it as an *override* in case a deployment
# was relying on the old knob to disable, but the canonical way is
# now the inverted ``FREECAD_MCP_LOAD_GABARITO``.
_LEGACY_NO_PREFIX = frozenset({"1", "true", "yes", "on"})


def _directive_enabled() -> bool:
    """Return True if the system-directive prefix should be applied.

    Resolution order (highest priority first):

    1. ``FREECAD_MCP_NO_DIRECTIVE_PREFIX=1`` (legacy opt-out — if set,
       the prefix is *not* applied, regardless of #2).
    2. ``FREECAD_MCP_LOAD_GABARITO=1`` (canonical opt-in).
    3. Default: off (no prefix).

    Operators who need the previous always-on behaviour should set
    ``FREECAD_MCP_LOAD_GABARITO=1``. The function is evaluated on every
    call so a runtime env change is picked up without a restart.
    """
    # Legacy override: if the old opt-out knob is set, the prefix is off
    # no matter what. This preserves backward compat for any deployment
    # that was relying on it.
    if os.environ.get("FREECAD_MCP_NO_DIRECTIVE_PREFIX", "").strip().lower() in _LEGACY_NO_PREFIX:
        return False
    return os.environ.get("FREECAD_MCP_LOAD_GABARITO", "").strip().lower() in _LEGACY_NO_PREFIX


def _ensure_prefix(message: str) -> str:
    if not _directive_enabled():
        return message
    if message.strip().startswith(SYSTEM_DIRECTIVE_PREFIX):
        return message
    return SYSTEM_DIRECTIVE_PREFIX + "\n\n" + message


def text_response(message: str) -> ToolResponse:
    return [TextContent(type="text", text=_ensure_prefix(message))]


def json_response(data: object) -> ToolResponse:
    return text_response(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def add_screenshot_if_available(
    response: ToolResponse,
    screenshot: str | None,
    only_text_feedback: bool,
    image_format: str = "png",
) -> ToolResponse:
    """Append a screenshot to *response* unless text-only feedback is on.

    The screenshot is appended as a single :class:`ImageContent`. The
    ``mimeType`` is derived from *image_format* (``"png"`` → ``image/png``,
    ``"jpeg"``/``"jpg"`` → ``image/jpeg``, ``"webp"`` → ``image/webp``);
    any other value falls back to ``image/png`` for backward compatibility.

    Why pass ``image_format`` instead of inferring from the bytes?
    XML-RPC moves the screenshot as base64, which loses the original
    container's magic bytes. The caller already knows the format it
    requested, so passing it in is the only reliable way to surface
    JPEG/WebP correctly to MCP clients that honour the mime type.
    """
    if only_text_feedback or screenshot is None:
        return response
    fmt = (image_format or "png").lower()
    mime = {
        "png": "image/png",
        "jpeg": "image/jpeg",
        "jpg": "image/jpeg",
        "webp": "image/webp",
    }.get(fmt, "image/png")
    return [*response, ImageContent(type="image", data=screenshot, mimeType=mime)]


__all__ = [
    "ToolResponse",
    "SYSTEM_DIRECTIVE_PREFIX",
    "text_response",
    "json_response",
    "add_screenshot_if_available",
    "_directive_enabled",
]
