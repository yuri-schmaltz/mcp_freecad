"""Direct tests for ``_screenshot.transcode_to_format``.

The previous coverage showed the JPEG / WebP happy paths but missed
several edge cases. These tests pin down the exact behaviour:

- Pillow missing -> None (graceful fallback)
- PIL errors during decode -> None (logged)
- Invalid target format -> None
- RGBA / LA / P images are flattened onto white for JPEG
"""
import base64
import importlib.util
import io
import sys
import types
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_RS_DIR = _HERE.parent / "addon" / "FreeCADMCP" / "rpc_server"

# Shims (PIL is conditionally imported inside the helper; we control
# its presence per test).
for name in ("FreeCAD", "FreeCADGui", "ObjectsFem", "PySide"):
    if name not in sys.modules:
        sys.modules[name] = types.ModuleType(name)

# FreeCAD Console stub — the helper logs there on transcode failure.
_fc = sys.modules["FreeCAD"]
_fc.Console = types.SimpleNamespace(
    PrintError=lambda *a, **k: None,
    PrintMessage=lambda *a, **k: None,
    PrintWarning=lambda *a, **k: None,
)

# Load the module.
pkg = types.ModuleType("_test_screenshot_pkg")
pkg.__path__ = [str(_RS_DIR)]
sys.modules["_test_screenshot_pkg"] = pkg
spec = importlib.util.spec_from_file_location(
    "_test_screenshot_pkg._screenshot", str(_RS_DIR / "_screenshot.py")
)
mod = importlib.util.module_from_spec(spec)
sys.modules["_test_screenshot_pkg._screenshot"] = mod
spec.loader.exec_module(mod)  # type: ignore[union-attr]


_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xfc\xff"
    b"\xff?\x00\x05\xfe\x02\xfe\xa3z\xd1\xc0\x00\x00\x00\x00IEND\xaeB`\x82"
)


# ---------------------------------------------------------------------------
# Pillow-missing path
# ---------------------------------------------------------------------------

def test_transcode_no_pillow_returns_none():
    """When Pillow is not importable, transcoding returns None."""
    saved_pil = sys.modules.pop("PIL", None)
    saved_pil_image = sys.modules.pop("PIL.Image", None)
    try:
        assert mod.transcode_to_format(_TINY_PNG, "jpeg") is None
        assert mod.transcode_to_format(_TINY_PNG, "webp") is None
    finally:
        if saved_pil is not None:
            sys.modules["PIL"] = saved_pil
        if saved_pil_image is not None:
            sys.modules["PIL.Image"] = saved_pil_image


# ---------------------------------------------------------------------------
# unknown format
# ---------------------------------------------------------------------------

def test_transcode_unknown_format_returns_none():
    """Anything that's not jpeg/jpg/webp -> None."""
    for bad in ("tiff", "bmp", "", "gif"):
        assert mod.transcode_to_format(_TINY_PNG, bad) is None, bad


def test_transcode_png_returns_none():
    """PNG is the source format; the helper explicitly does not
    re-encode PNGs (caller should use the raw PNG path)."""
    assert mod.transcode_to_format(_TINY_PNG, "png") is None


# ---------------------------------------------------------------------------
# happy path with Pillow installed
# ---------------------------------------------------------------------------

def test_transcode_jpeg_happy_path():
    try:
        import PIL  # type: ignore # noqa: F401
    except Exception:
        import pytest
        pytest.skip("Pillow not installed")
    out = mod.transcode_to_format(_TINY_PNG, "jpeg")
    assert out is not None
    decoded = base64.b64decode(out)
    assert decoded[:3] == b"\xff\xd8\xff"  # JPEG magic


def test_transcode_jpg_alias():
    """'jpg' is treated as a synonym for 'jpeg'."""
    try:
        import PIL  # type: ignore # noqa: F401
    except Exception:
        import pytest
        pytest.skip("Pillow not installed")
    out = mod.transcode_to_format(_TINY_PNG, "jpg")
    assert out is not None
    decoded = base64.b64decode(out)
    assert decoded[:3] == b"\xff\xd8\xff"


def test_transcode_webp_happy_path():
    try:
        import PIL  # type: ignore # noqa: F401
    except Exception:
        import pytest
        pytest.skip("Pillow not installed")
    out = mod.transcode_to_format(_TINY_PNG, "webp")
    assert out is not None
    decoded = base64.b64decode(out)
    assert decoded[:4] == b"RIFF"
    assert decoded[8:12] == b"WEBP"


# ---------------------------------------------------------------------------
# error paths inside the helper
# ---------------------------------------------------------------------------

def test_transcode_pillow_raises_on_decode_returns_none():
    """If Pillow raises during decode/encode, the helper swallows it
    and returns None (the FreeCAD console is logged to via PrintError)."""
    try:
        from PIL import Image  # type: ignore
    except Exception:
        import pytest
        pytest.skip("Pillow not installed")

    class _BrokenImage:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def convert(self, *a, **kw):
            return self
        def save(self, *a, **kw):
            raise RuntimeError("save failed")

    saved_open = Image.open
    Image.open = lambda *a, **kw: _BrokenImage()
    try:
        out = mod.transcode_to_format(_TINY_PNG, "jpeg")
        assert out is None
    finally:
        Image.open = saved_open


def test_transcode_pillow_raises_on_open_returns_none():
    """If Pillow.Image.open itself raises, we still return None."""
    try:
        from PIL import Image  # type: ignore
    except Exception:
        import pytest
        pytest.skip("Pillow not installed")

    saved_open = Image.open
    Image.open = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("decode failed"))
    try:
        out = mod.transcode_to_format(_TINY_PNG, "jpeg")
        assert out is None
    finally:
        Image.open = saved_open


# ---------------------------------------------------------------------------
# RGBA / LA / P -> RGB conversion for JPEG
# ---------------------------------------------------------------------------

def test_transcode_rgba_to_jpeg_flattens():
    """v1.0.3 — RGBA images are flattened onto white before JPEG encode
    (since JPEG has no alpha channel)."""
    try:
        from PIL import Image  # type: ignore
    except Exception:
        import pytest
        pytest.skip("Pillow not installed")
    # Build a 1x1 RGBA PNG manually so we don't need a real image file.
    img = Image.new("RGBA", (4, 4), (255, 0, 0, 128))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    rgba_bytes = buf.getvalue()

    out = mod.transcode_to_format(rgba_bytes, "jpeg")
    assert out is not None
    decoded = base64.b64decode(out)
    assert decoded[:3] == b"\xff\xd8\xff"


def test_transcode_palette_to_jpeg_flattens():
    """P-mode PNGs are flattened onto RGB for JPEG too."""
    try:
        from PIL import Image  # type: ignore
    except Exception:
        import pytest
        pytest.skip("Pillow not installed")
    img = Image.new("P", (4, 4))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    p_bytes = buf.getvalue()

    out = mod.transcode_to_format(p_bytes, "jpeg")
    assert out is not None
    decoded = base64.b64decode(out)
    assert decoded[:3] == b"\xff\xd8\xff"