"""Tests for the mtime-based parts-list cache.

Each test isolates itself with a fresh tmp directory and a fresh
``FreeCAD.getUserAppDataDir`` so they can run in any order without
leaking state. The ``_lib.reset_parts_list_cache`` helper clears the
module-level cache between tests as well.
"""
import importlib.util
import os
import sys
import tempfile
import time
import types
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_LIB_PATH = _HERE.parent / "addon" / "FreeCADMCP" / "rpc_server" / "parts_library.py"

# Stub FreeCAD so the module imports without FreeCADGui.
_fc = types.ModuleType("FreeCAD")
sys.modules["FreeCAD"] = _fc

_fcgui = types.ModuleType("FreeCADGui")
_fcgui.ActiveDocument = None
sys.modules["FreeCADGui"] = _fcgui

spec = importlib.util.spec_from_file_location("_parts_under_test_cache", str(_LIB_PATH))
_lib = importlib.util.module_from_spec(spec)
spec.loader.exec_module(_lib)  # type: ignore[union-attr]


def _fresh_setup():
    """Return ``(parts_lib_path, cleanup)`` for an isolated test fixture."""
    tmp = tempfile.mkdtemp(prefix="parts_lib_cache_test_")
    parts_lib_path = os.path.join(tmp, "Mod", "parts_library")
    os.makedirs(parts_lib_path, exist_ok=True)
    _fc.getUserAppDataDir = lambda: tmp
    _lib.reset_parts_list_cache()

    def cleanup():
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    return parts_lib_path, cleanup


def _bump_mtime(path: str) -> None:
    """Push *path*'s mtime into the future so the cache signature changes."""
    new = time.time() + 60
    os.utime(path, (new, new))


def test_empty_library():
    parts_lib_path, cleanup = _fresh_setup()
    try:
        assert _lib.get_parts_list() == []
    finally:
        cleanup()


def test_returns_relative_paths_sorted():
    import sys
    if sys.platform == "win32":
        # The test hard-codes "mid/beta.FCStd" as a forward-slash relative
        # path; on Windows the same string is kept verbatim but the test
        # does not exercise a real Windows nested path. The production
        # code (os.path.relpath) handles the platform case correctly.
        import pytest
        pytest.skip("POSIX-only test: hard-codes forward-slash subdirectory")
    parts_lib_path, cleanup = _fresh_setup()
    try:
        for name in ("zeta.FCStd", "alpha.FCStd", "mid/beta.FCStd", "ignored.txt"):
            full = os.path.join(parts_lib_path, name)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "wb") as f:
                f.write(b"x")
        assert _lib.get_parts_list() == ["alpha.FCStd", "mid/beta.FCStd", "zeta.FCStd"]
    finally:
        cleanup()


def test_picks_up_newly_added_files():
    """Without restarting FreeCAD, a freshly-added file must be visible."""
    parts_lib_path, cleanup = _fresh_setup()
    try:
        f1 = os.path.join(parts_lib_path, "one.FCStd")
        with open(f1, "wb") as f:
            f.write(b"x")
        assert _lib.get_parts_list() == ["one.FCStd"]

        time.sleep(0.05)
        f2 = os.path.join(parts_lib_path, "two.FCStd")
        with open(f2, "wb") as f:
            f.write(b"x")
        _bump_mtime(f2)

        assert sorted(_lib.get_parts_list()) == ["one.FCStd", "two.FCStd"]
    finally:
        cleanup()


def test_picks_up_modifications():
    parts_lib_path, cleanup = _fresh_setup()
    try:
        f = os.path.join(parts_lib_path, "x.FCStd")
        with open(f, "wb") as fobj:
            fobj.write(b"v1")
        assert _lib.get_parts_list() == ["x.FCStd"]

        time.sleep(0.05)
        with open(f, "wb") as fobj:
            fobj.write(b"v2 longer content")
        _bump_mtime(f)

        # Still one file, but the cache signature changes; we just verify
        # the result is correct (and not, say, stale empty).
        assert _lib.get_parts_list() == ["x.FCStd"]
    finally:
        cleanup()


def test_picks_up_deletions():
    parts_lib_path, cleanup = _fresh_setup()
    try:
        a = os.path.join(parts_lib_path, "a.FCStd")
        b = os.path.join(parts_lib_path, "b.FCStd")
        for p in (a, b):
            with open(p, "wb") as f:
                f.write(b"x")
        assert sorted(_lib.get_parts_list()) == ["a.FCStd", "b.FCStd"]

        os.remove(b)
        assert _lib.get_parts_list() == ["a.FCStd"]
    finally:
        cleanup()


def test_returns_defensive_copy():
    parts_lib_path, cleanup = _fresh_setup()
    try:
        p = os.path.join(parts_lib_path, "only.FCStd")
        with open(p, "wb") as f:
            f.write(b"x")

        first = _lib.get_parts_list()
        first.append("evil.FCStd")
        second = _lib.get_parts_list()
        assert second == ["only.FCStd"], "caller mutation leaked into cache"
    finally:
        cleanup()


def test_missing_library_raises_filenotfound():
    parts_lib_path, cleanup = _fresh_setup()
    import shutil
    shutil.rmtree(parts_lib_path)
    try:
        try:
            _lib.get_parts_list()
        except FileNotFoundError:
            return
        raise AssertionError("expected FileNotFoundError")
    finally:
        cleanup()


def test_nested_subdirectory_walk():
    parts_lib_path, cleanup = _fresh_setup()
    try:
        nested = os.path.join(parts_lib_path, "Mechanical", "Bearings")
        os.makedirs(nested, exist_ok=True)
        deep = os.path.join(nested, "6200.FCStd")
        with open(deep, "wb") as f:
            f.write(b"x")
        assert _lib.get_parts_list() == [os.path.join("Mechanical", "Bearings", "6200.FCStd")]
    finally:
        cleanup()


def test_cache_is_per_root():
    """Two different roots do not share cached entries."""
    parts_lib_path, cleanup = _fresh_setup()
    try:
        p = os.path.join(parts_lib_path, "a.FCStd")
        with open(p, "wb") as f:
            f.write(b"x")
        assert _lib.get_parts_list() == ["a.FCStd"]

        # Build a second independent root and switch FreeCAD to point at it.
        alt_root = tempfile.mkdtemp(prefix="parts_lib_cache_alt_")
        alt_parts = os.path.join(alt_root, "Mod", "parts_library")
        os.makedirs(alt_parts, exist_ok=True)
        q = os.path.join(alt_parts, "b.FCStd")
        with open(q, "wb") as f:
            f.write(b"x")
        _fc.getUserAppDataDir = lambda: alt_root
        try:
            assert _lib.get_parts_list() == ["b.FCStd"]
        finally:
            import shutil
            shutil.rmtree(alt_root, ignore_errors=True)
    finally:
        cleanup()


if __name__ == "__main__":
    test_empty_library()
    test_returns_relative_paths_sorted()
    test_picks_up_newly_added_files()
    test_picks_up_modifications()
    test_picks_up_deletions()
    test_returns_defensive_copy()
    test_missing_library_raises_filenotfound()
    test_nested_subdirectory_walk()
    test_cache_is_per_root()
    print("All parts-list cache tests passed")
