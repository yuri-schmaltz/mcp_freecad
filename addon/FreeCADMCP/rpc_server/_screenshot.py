"""Screenshot capture and transcoding helpers.

Extracted from ``rpc_server`` so the Pillow transcode logic is
testable without a running FreeCAD instance.

Public surface:

* :func:`transcode_to_format` \u2014 PNG bytes \u2192 base64 JPEG/WebP (or None
  on failure / no Pillow / unsupported target).

The capture side (view switching, selection, ``saveImage``) lives in
the ``FreeCADRPC`` class in :mod:`rpc_server` because it requires a
live FreeCAD context.

v1.0.3 — removed ``SCREENSHOT_SUPPORT_CHECK``. The previous client
implementation ran this snippet via ``execute_code`` to probe
whether the current view supports ``saveImage`` (one RPC), then ran
the capture (a second RPC). The race between the two calls could
return a blank screenshot if the user changed workbenches in
between, and the happy path paid double latency. The server's
``get_active_screenshot`` now returns ``{"success": False, "reason":
...}`` directly, so one RPC is enough.
"""
from __future__ import annotations

import base64
import io

try:
    import FreeCAD
except Exception:
    FreeCAD = None  # type: ignore[assignment]


def transcode_to_format(png_bytes: bytes, target_format: str) -> str | None:
    """Transcode a PNG byte string to JPEG or WebP via Pillow.

    Returns the base64-encoded output, or ``None`` on failure. The
    failure modes are:
    * Pillow is not installed (callers should display a clear error).
    * The target format is not ``"jpeg"``/``"jpg"``/``"webp"``.
    * Any PIL error during decode/encode.
    """
    try:
        from PIL import Image  # type: ignore
    except Exception:
        return None
    try:
        with Image.open(io.BytesIO(png_bytes)) as img:
            buf = io.BytesIO()
            save_kwargs: dict = {}
            if target_format in ("jpeg", "jpg"):
                # JPEG cannot store alpha; flatten onto white.
                if img.mode in ("RGBA", "LA", "P"):
                    img = img.convert("RGB")
                save_kwargs["quality"] = 85
                img.save(buf, format="JPEG", **save_kwargs)
            elif target_format == "webp":
                save_kwargs["quality"] = 80
                img.save(buf, format="WEBP", **save_kwargs)
            else:
                return None
            return base64.b64encode(buf.getvalue()).decode("utf-8")
    except Exception as e:
        # Surface transcode failures in the FreeCAD console so the user
        # can tell when a screenshot is being silently dropped (e.g. on
        # a corrupt PNG) instead of returning the raw PNG fallback.
        if FreeCAD is not None and hasattr(FreeCAD, "Console"):
            FreeCAD.Console.PrintError(
                f"MCP RPC: screenshot transcode to {target_format} failed: "
                f"{type(e).__name__}: {e}\n"
            )
        return None


__all__ = ["transcode_to_format"]
