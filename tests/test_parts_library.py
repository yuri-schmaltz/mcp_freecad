"""Tests for ``parts_library.insert_part_from_library``.

The path-traversal hardening lives in the pure helper ``_safe_resolve``;
we exercise it directly so we do not need FreeCADGui at runtime.
"""
import importlib.util
import os
import sys
import tempfile
import types
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_LIB_PATH = _HERE.parent / "addon" / "FreeCADMCP" / "rpc_server" / "parts_library.py"

# Stub FreeCAD so the module imports without FreeCADGui.
_fc = types.ModuleType("FreeCAD")
_fc.getUserAppDataDir = lambda: tempfile.gettempdir()
sys.modules["FreeCAD"] = _fc

_fcgui = types.ModuleType("FreeCADGui")
_fcgui.ActiveDocument = None
sys.modules["FreeCADGui"] = _fcgui

spec = importlib.util.spec_from_file_location("_parts_lib_under_test", str(_LIB_PATH))
_lib = importlib.util.module_from_spec(spec)
spec.loader.exec_module(_lib)  # type: ignore[union-attr]


def _make_lib_root() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="parts_lib_test_"))
    (tmp / "parts_library").mkdir()
    return tmp


def test_relative_resolves_inside():
    tmp = _make_lib_root()
    lib = str(tmp / "parts_library")
    p = tmp / "parts_library" / "gear.fcstd"
    p.write_text("x")
    assert _lib._safe_resolve(lib, "gear.fcstd") == str(p.resolve())


def test_nested_relative_resolves_inside():
    tmp = _make_lib_root()
    lib = str(tmp / "parts_library")
    nested = tmp / "parts_library" / "Mechanical" / "Bearings"
    nested.mkdir(parents=True)
    p = nested / "6200.fcstd"
    p.write_text("x")
    assert _lib._safe_resolve(lib, os.path.join("Mechanical", "Bearings", "6200.fcstd")) == str(p.resolve())


def test_absolute_path_rejected():
    tmp = _make_lib_root()
    lib = str(tmp / "parts_library")
    abs_path = str(tmp / "anything.fcstd")
    try:
        _lib._safe_resolve(lib, abs_path)
    except ValueError:
        return
    raise AssertionError("expected ValueError for absolute path")


def test_parent_traversal_rejected():
    tmp = _make_lib_root()
    lib = str(tmp / "parts_library")
    try:
        _lib._safe_resolve(lib, "../../etc/passwd")
    except ValueError:
        return
    raise AssertionError("expected ValueError for ../")


def test_dotdot_in_middle_rejected():
    tmp = _make_lib_root()
    lib = str(tmp / "parts_library")
    try:
        _lib._safe_resolve(lib, "Mechanical/../../etc/passwd")
    except ValueError:
        return
    raise AssertionError("expected ValueError for mid-path ../")


def test_empty_rejected():
    tmp = _make_lib_root()
    lib = str(tmp / "parts_library")
    for v in ("", "   "):
        try:
            _lib._safe_resolve(lib, v)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {v!r}")


def test_root_separator_rejected():
    tmp = _make_lib_root()
    lib = str(tmp / "parts_library")
    try:
        _lib._safe_resolve(lib, "/etc/passwd")
    except ValueError:
        return
    raise AssertionError("expected ValueError for leading /")


def test_symlink_escape_rejected():
    tmp = _make_lib_root()
    lib = str(tmp / "parts_library")
    secret = tmp / "secret.fcstd"
    secret.write_text("x")
    link = tmp / "parts_library" / "leak.fcstd"
    try:
        os.symlink(str(secret), str(link))
    except (OSError, NotImplementedError):
        # Some platforms / FS do not support symlinks — skip.
        return
    try:
        _lib._safe_resolve(lib, "leak.fcstd")
    except ValueError:
        return
    raise AssertionError("expected ValueError for symlink escape")


# ---------------------------------------------------------------------------
# _safe_mtime / insert_part_from_library
# ---------------------------------------------------------------------------


def test_safe_mtime_returns_zero_for_missing_path():
    """OSError (e.g. ENOENT) is swallowed -> 0.0."""
    assert _lib._safe_mtime("/nonexistent/path/that/does/not/exist") == 0.0


def test_insert_part_from_library_calls_merge_project():
    """insert_part_from_library resolves the path and calls mergeProject."""
    tmp = _make_lib_root()
    # The module joins ``Mod/parts_library`` under getUserAppDataDir().
    lib = tmp / "Mod" / "parts_library"
    lib.mkdir(parents=True)
    (lib / "box.fcstd").write_bytes(b"fake-fcstd-bytes")

    calls: list[str] = []

    def _merge(path):
        calls.append(path)

    _fcgui.ActiveDocument = types.SimpleNamespace(mergeProject=_merge)

    saved_dir = _fc.getUserAppDataDir
    _fc.getUserAppDataDir = lambda: str(tmp)
    try:
        _lib.insert_part_from_library("box.fcstd")
        assert len(calls) == 1
        assert calls[0].endswith(os.path.join("parts_library", "box.fcstd"))
    finally:
        _fc.getUserAppDataDir = saved_dir
        _fcgui.ActiveDocument = None


def test_insert_part_from_library_missing_file_raises():
    """insert_part_from_library raises FileNotFoundError for missing parts."""
    tmp = _make_lib_root()
    saved_dir = _fc.getUserAppDataDir
    _fc.getUserAppDataDir = lambda: str(tmp)
    try:
        import pytest
        with pytest.raises(FileNotFoundError, match="Not found"):
            _lib.insert_part_from_library("missing.fcstd")
    finally:
        _fc.getUserAppDataDir = saved_dir


if __name__ == "__main__":
    test_relative_resolves_inside()
    test_nested_relative_resolves_inside()
    test_absolute_path_rejected()
    test_parent_traversal_rejected()
    test_dotdot_in_middle_rejected()
    test_empty_rejected()
    test_root_separator_rejected()
    test_symlink_escape_rejected()
    print("All parts_library tests passed")
