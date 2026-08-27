"""Tests for the settings-path fallback chain."""
import importlib.util
import os
import sys
import tempfile
import types
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_RS_DIR = _HERE.parent / "addon" / "FreeCADMCP" / "rpc_server"

# Standard shims.
for name in ("FreeCAD", "FreeCADGui", "ObjectsFem", "PySide"):
    if name not in sys.modules:
        sys.modules[name] = types.ModuleType(name)

# ``_fc`` is a thin proxy over ``sys.modules['FreeCAD']`` so tests can
# keep using ``_fc.foo = bar`` even when an earlier-imported test file
# replaces the sys.modules entry at import time. Without this proxy,
# the module-level reference would be stale and subsequent tests would
# silently miss the patched attribute.
class _FreeCADProxy:
    def __getattr__(self, name):
        return getattr(sys.modules["FreeCAD"], name)

    def __setattr__(self, name, value):
        setattr(sys.modules["FreeCAD"], name, value)


_fc = _FreeCADProxy()
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
    pkg = types.ModuleType("_rs_pkg_sf")
    pkg.__path__ = [str(_RS_DIR)]
    sys.modules["_rs_pkg_sf"] = pkg
    for sub in ("parts_library", "serialize", "_fem_workdir", "_request_tracking"):
        spec = importlib.util.spec_from_file_location(
            f"_rs_pkg_sf.{sub}", str(_RS_DIR / f"{sub}.py")
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[f"_rs_pkg_sf.{sub}"] = mod
        spec.loader.exec_module(mod)
    spec = importlib.util.spec_from_file_location(
        "_rs_pkg_sf.rpc_server", str(_RS_DIR / "rpc_server.py")
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_rs_pkg_sf.rpc_server"] = mod
    spec.loader.exec_module(mod)
    # Force the rpc_server's FreeCAD reference to the live sys.modules
    # entry — earlier test modules (e.g. test_parts_list_cache) replace
    # ``sys.modules['FreeCAD']`` at module-import time so the reference
    # captured during ``exec_module`` may be stale.
    mod.FreeCAD = sys.modules["FreeCAD"]
    return mod


def _with_env(env: dict[str, str]):
    """Set env vars and return (saved, restore_cb)."""
    saved = {k: os.environ.get(k) for k in env}
    for k, v in env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

    def restore():
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    return restore


# ---------------------------------------------------------------------------
# _writable_dir / _ensure_dir
# ---------------------------------------------------------------------------

def test_writable_dir_true_for_writable():
    rpc_mod = _load_rpc_server()
    d = tempfile.mkdtemp(prefix="writable_test_")
    assert rpc_mod._writable_dir(d) is True


def test_writable_dir_false_for_nonexistent():
    rpc_mod = _load_rpc_server()
    assert rpc_mod._writable_dir("/nonexistent/path/xyz") is False


def test_writable_dir_false_for_empty():
    rpc_mod = _load_rpc_server()
    assert rpc_mod._writable_dir("") is False


def test_ensure_dir_creates_missing():
    rpc_mod = _load_rpc_server()
    parent = tempfile.mkdtemp(prefix="ensure_dir_")
    target = os.path.join(parent, "sub1", "sub2")
    assert rpc_mod._ensure_dir(target) is True
    assert os.path.isdir(target)


def test_ensure_dir_idempotent():
    rpc_mod = _load_rpc_server()
    d = tempfile.mkdtemp(prefix="ensure_dir_idem_")
    assert rpc_mod._ensure_dir(d) is True
    assert rpc_mod._ensure_dir(d) is True


# ---------------------------------------------------------------------------
# _resolve_settings_dir / _get_settings_path
# ---------------------------------------------------------------------------

def test_resolve_uses_freecad_dir_when_writable():
    rpc_mod = _load_rpc_server()
    fc_dir = tempfile.mkdtemp(prefix="fc_user_")
    _fc.getUserAppDataDir = lambda: fc_dir
    try:
        assert rpc_mod._resolve_settings_dir() == fc_dir
    finally:
        import shutil
        shutil.rmtree(fc_dir, ignore_errors=True)


def test_resolve_falls_back_to_home_when_freecad_unwritable():
    """If the FreeCAD user dir is read-only, we fall back to HOME/.config."""
    import sys
    if sys.platform == "win32":
        # /proc/1 is a POSIX construct; on Windows we have no equivalent
        # path that's guaranteed to be unwritable. The skip matches the
        # pattern already used in test_resolve_uses_legacy_dir_when_present.
        import pytest
        pytest.skip("POSIX-only test: uses /proc/1 as the unwritable path")
    rpc_mod = _load_rpc_server()
    home = tempfile.mkdtemp(prefix="home_for_fb_")
    restore = _with_env({"HOME": home, "XDG_CONFIG_HOME": ""})

    try:
        # /proc/1/xyz cannot be created by an unprivileged user; the
        # primary path will fail both _ensure_dir and _writable_dir.
        _fc.getUserAppDataDir = lambda: "/proc/1/mcp_test_unwritable"
        chosen = rpc_mod._resolve_settings_dir()
        # HOME fallback is <home>/.config/mcp-freecad (created).
        assert chosen == os.path.join(home, ".config", "mcp-freecad")
        assert os.path.isdir(chosen)
    finally:
        restore()
        import shutil
        shutil.rmtree(home, ignore_errors=True)


def test_resolve_xdg_takes_priority_over_home():
    import sys
    if sys.platform == "win32":
        import pytest
        pytest.skip("POSIX-only test: uses /proc/1 as the unwritable path")
    rpc_mod = _load_rpc_server()
    xdg = tempfile.mkdtemp(prefix="xdg_for_fb_")
    home = tempfile.mkdtemp(prefix="home_for_xdg_")
    restore = _with_env({"XDG_CONFIG_HOME": xdg, "HOME": home})

    try:
        _fc.getUserAppDataDir = lambda: "/proc/1/mcp_test_unwritable"
        chosen = rpc_mod._resolve_settings_dir()
        assert chosen == os.path.join(xdg, "mcp-freecad")
    finally:
        restore()
        import shutil
        shutil.rmtree(xdg, ignore_errors=True)
        shutil.rmtree(home, ignore_errors=True)


def test_resolve_falls_back_to_temp_when_nothing_writable():
    import sys
    if sys.platform == "win32":
        import pytest
        pytest.skip("POSIX-only test: uses /proc/1 as the unwritable path")
    rpc_mod = _load_rpc_server()
    # HOME and XDG point at unwritable locations; only /tmp is left.
    restore = _with_env({"XDG_CONFIG_HOME": "/proc/1/xdg_unwritable", "HOME": "/proc/1/home_unwritable"})

    try:
        _fc.getUserAppDataDir = lambda: "/proc/1/mcp_test_unwritable"
        chosen = rpc_mod._resolve_settings_dir()
        # Last resort is tempdir/mcp-freecad.
        assert chosen == os.path.join(tempfile.gettempdir(), "mcp-freecad")
        assert os.path.isdir(chosen)
    finally:
        restore()


def test_resolve_handles_freecad_raising():
    rpc_mod = _load_rpc_server()
    def boom():
        raise RuntimeError("no App")
    _fc.getUserAppDataDir = boom
    home = tempfile.mkdtemp(prefix="home_for_boom_")
    restore = _with_env({"HOME": home, "XDG_CONFIG_HOME": ""})
    try:
        # Should not raise; should fall back to HOME.
        chosen = rpc_mod._resolve_settings_dir()
        assert chosen == os.path.join(home, ".config", "mcp-freecad")
    finally:
        restore()
        import shutil
        shutil.rmtree(home, ignore_errors=True)


def test_resolve_uses_legacy_dir_when_present():
    """If a pre-1.0.0 ``freecad-mcp`` dir already exists, keep using it.

    This is the backward-compat path for users upgrading from 0.x.
    """
    import sys

    if sys.platform == "win32":
        # The /proc/1 trick used by the other tests below is not portable:
        # on Windows that path resolves to a writable location and the test
        # does not exercise the fallback path at all. Skip on Windows rather
        # than introduce a Windows-specific unwritable path.
        import pytest
        pytest.skip("uses POSIX-only /proc/1 unwritable path; see other tests' pre-existing portability issue")

    rpc_mod = _load_rpc_server()
    home = tempfile.mkdtemp(prefix="home_for_legacy_")
    legacy = os.path.join(home, ".config", "freecad-mcp")
    os.makedirs(legacy, exist_ok=True)
    restore = _with_env({"HOME": home, "XDG_CONFIG_HOME": ""})

    try:
        _fc.getUserAppDataDir = lambda: "/proc/1/mcp_test_unwritable"
        chosen = rpc_mod._resolve_settings_dir()
        # Legacy dir is preferred only if it pre-exists.
        assert chosen == legacy
    finally:
        restore()
        import shutil
        shutil.rmtree(home, ignore_errors=True)


# ---------------------------------------------------------------------------
# load_settings / save_settings round-trip
# ---------------------------------------------------------------------------

def test_save_then_load_round_trip():
    rpc_mod = _load_rpc_server()
    fc_dir = tempfile.mkdtemp(prefix="fc_round_")
    _fc.getUserAppDataDir = lambda: fc_dir
    try:
        rpc_mod.save_settings({"remote_enabled": True, "allowed_ips": "10.0.0.0/8, 127.0.0.1", "auto_start_rpc": True})
        loaded = rpc_mod.load_settings()
        assert loaded["remote_enabled"] is True
        assert loaded["allowed_ips"] == "10.0.0.0/8, 127.0.0.1"
        assert loaded["auto_start_rpc"] is True
    finally:
        import shutil
        shutil.rmtree(fc_dir, ignore_errors=True)


def test_load_returns_defaults_when_no_file():
    rpc_mod = _load_rpc_server()
    fc_dir = tempfile.mkdtemp(prefix="fc_defaults_")
    _fc.getUserAppDataDir = lambda: fc_dir
    try:
        loaded = rpc_mod.load_settings()
        assert loaded == {
            "remote_enabled": False,
            "allowed_ips": "127.0.0.1",
            "auto_start_rpc": False,
        }
    finally:
        import shutil
        shutil.rmtree(fc_dir, ignore_errors=True)


def test_load_fills_missing_keys_with_defaults():
    rpc_mod = _load_rpc_server()
    fc_dir = tempfile.mkdtemp(prefix="fc_partial_")
    _fc.getUserAppDataDir = lambda: fc_dir
    try:
        rpc_mod.save_settings({"remote_enabled": True})  # only one key
        loaded = rpc_mod.load_settings()
        assert loaded["remote_enabled"] is True
        # Missing keys backfilled.
        assert loaded["allowed_ips"] == "127.0.0.1"
        assert loaded["auto_start_rpc"] is False
    finally:
        import shutil
        shutil.rmtree(fc_dir, ignore_errors=True)


def test_load_handles_corrupt_file():
    rpc_mod = _load_rpc_server()
    fc_dir = tempfile.mkdtemp(prefix="fc_corrupt_")
    _fc.getUserAppDataDir = lambda: fc_dir
    try:
        path = os.path.join(fc_dir, "freecad_mcp_settings.json")
        with open(path, "w") as f:
            f.write("not valid json {{{")
        # Falls back to defaults on parse error.
        loaded = rpc_mod.load_settings()
        assert loaded == {
            "remote_enabled": False,
            "allowed_ips": "127.0.0.1",
            "auto_start_rpc": False,
        }
    finally:
        import shutil
        shutil.rmtree(fc_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# _writable_dir / save_settings error paths
# ---------------------------------------------------------------------------


def test_writable_dir_returns_false_on_permission_error(monkeypatch, tmp_path):
    """When mkstemp raises PermissionError, _writable_dir returns False."""
    rpc_mod = _load_rpc_server()
    import tempfile as _tempfile
    original = _tempfile.mkstemp

    def _boom(*args, **kwargs):
        raise PermissionError("nope")

    monkeypatch.setattr(_tempfile, "mkstemp", _boom)
    try:
        assert rpc_mod._writable_dir(str(tmp_path)) is False
    finally:
        monkeypatch.setattr(_tempfile, "mkstemp", original)


def test_writable_dir_returns_false_on_oserror(monkeypatch, tmp_path):
    """When mkstemp raises OSError, _writable_dir returns False."""
    rpc_mod = _load_rpc_server()
    import tempfile as _tempfile
    original = _tempfile.mkstemp

    def _boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(_tempfile, "mkstemp", _boom)
    try:
        assert rpc_mod._writable_dir(str(tmp_path)) is False
    finally:
        monkeypatch.setattr(_tempfile, "mkstemp", original)


def test_save_settings_logs_error_when_write_fails(monkeypatch, tmp_path):
    """If open() raises during save, save_settings logs to the FreeCAD
    console and does not propagate."""
    rpc_mod = _load_rpc_server()
    fc_dir = str(tmp_path)
    _fc.getUserAppDataDir = lambda: fc_dir

    printed: list[str] = []
    _fc.Console = types.SimpleNamespace(
        PrintError=lambda *args, **kw: printed.append(args[0] if args else ""),
        PrintWarning=lambda *a, **k: None,
        PrintMessage=lambda *a, **k: None,
    )

    # Patch open to raise — easiest via a builtin swap.
    import builtins
    real_open = builtins.open

    def _bad_open(path, *args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(builtins, "open", _bad_open)
    try:
        # Should NOT raise.
        rpc_mod.save_settings({"remote_enabled": True})
    finally:
        monkeypatch.setattr(builtins, "open", real_open)

    assert any("Failed to save" in m for m in printed)


if __name__ == "__main__":
    test_writable_dir_true_for_writable()
    test_writable_dir_false_for_nonexistent()
    test_writable_dir_false_for_empty()
    test_ensure_dir_creates_missing()
    test_ensure_dir_idempotent()
    test_resolve_uses_freecad_dir_when_writable()
    test_resolve_falls_back_to_home_when_freecad_unwritable()
    test_resolve_xdg_takes_priority_over_home()
    test_resolve_falls_back_to_temp_when_nothing_writable()
    test_resolve_handles_freecad_raising()
    test_resolve_uses_legacy_dir_when_present()
    test_save_then_load_round_trip()
    test_load_returns_defaults_when_no_file()
    test_load_fills_missing_keys_with_defaults()
    test_load_handles_corrupt_file()
    print("All settings fallback tests passed")
