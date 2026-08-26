"""Tests for the PNG -> JPEG/WebP transcoding helper."""
import base64
import importlib.util
import sys
import types
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_RS_DIR = _HERE.parent / "addon" / "FreeCADMCP" / "rpc_server"

# Standard shim set.
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
    pkg = types.ModuleType("_rs_pkg_tcode")
    pkg.__path__ = [str(_RS_DIR)]
    sys.modules["_rs_pkg_tcode"] = pkg
    for sub in ("parts_library", "serialize", "_fem_workdir", "_request_tracking"):
        spec = importlib.util.spec_from_file_location(
            f"_rs_pkg_tcode.{sub}", str(_RS_DIR / f"{sub}.py")
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[f"_rs_pkg_tcode.{sub}"] = mod
        spec.loader.exec_module(mod)
    spec = importlib.util.spec_from_file_location(
        "_rs_pkg_tcode.rpc_server", str(_RS_DIR / "rpc_server.py")
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_rs_pkg_tcode.rpc_server"] = mod
    spec.loader.exec_module(mod)
    return mod


# A 4x4 red PNG, used to exercise the transcoding path. Built once at
# import time so we don't pay the Pillow cost on every test.
import io as _io
from PIL import Image as _Image
_buf = _io.BytesIO()
_Image.new("RGB", (4, 4), "red").save(_buf, format="PNG")
_TINY_PNG = _buf.getvalue()


def test_transcode_returns_none_when_pillow_missing():
    """If Pillow is not importable, transcoding returns None.

    We patch ``__import__`` rather than popping PIL from ``sys.modules``
    because popping breaks Pillow's plugin registry (the PNG/WebP/JPEG
    plugins are not re-registered on re-import).
    """
    rpc_mod = _load_rpc_server()
    import builtins
    sentinel = ImportError("simulated missing Pillow")

    def _raiser(name, *args, **kwargs):
        if name == "PIL" or name.startswith("PIL."):
            raise sentinel
        return builtins.__import__(name, *args, **kwargs)

    real_import = builtins.__import__
    builtins.__import__ = _raiser
    try:
        result = rpc_mod._transcode_screenshot(_TINY_PNG, "jpeg")
        assert result is None
    finally:
        builtins.__import__ = real_import


def test_transcode_unknown_format_returns_none():
    rpc_mod = _load_rpc_server()
    assert rpc_mod._transcode_screenshot(_TINY_PNG, "tiff") is None
    assert rpc_mod._transcode_screenshot(_TINY_PNG, "") is None


def test_transcode_passthrough_not_supported():
    """_transcode_screenshot only handles jpeg/jpg/webp; 'png' should return None
    (caller should use the raw PNG path)."""
    rpc_mod = _load_rpc_server()
    assert rpc_mod._transcode_screenshot(_TINY_PNG, "png") is None


def test_transcode_with_pillow_when_available():
    """If Pillow is importable, transcoding produces a valid base64 string."""
    try:
        import PIL  # type: ignore  # noqa: F401 — existence check
    except Exception:
        return  # Pillow not installed — skip silently (test still passes)

    rpc_mod = _load_rpc_server()
    jpeg_b64 = rpc_mod._transcode_screenshot(_TINY_PNG, "jpeg")
    assert jpeg_b64 is not None
    decoded = base64.b64decode(jpeg_b64)
    # JPEG magic: FF D8 FF
    assert decoded[:3] == b"\xff\xd8\xff", f"unexpected magic: {decoded[:3]!r}"

    webp_b64 = rpc_mod._transcode_screenshot(_TINY_PNG, "webp")
    assert webp_b64 is not None
    decoded_webp = base64.b64decode(webp_b64)
    # WebP magic: 'RIFF' ... 'WEBP'
    assert decoded_webp[:4] == b"RIFF", f"unexpected magic: {decoded_webp[:4]!r}"
    assert decoded_webp[8:12] == b"WEBP", f"unexpected format: {decoded_webp[8:12]!r}"


if __name__ == "__main__":
    test_transcode_returns_none_when_pillow_missing()
    test_transcode_unknown_format_returns_none()
    test_transcode_with_pillow_when_available()
    print("All screenshot transcode tests passed")
