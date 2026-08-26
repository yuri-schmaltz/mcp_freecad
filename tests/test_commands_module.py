"""Direct tests for the FreeCAD toolbar command classes.

v1.0.3 coverage push — ``_commands.py`` was 19 % covered. These tests
exercise every command's GetResources / Activated / IsActive and the
``_sync_toggle_states`` helper. FreeCAD / PySide / FreeCADGui are
stubbed so the module imports cleanly outside FreeCAD.
"""
import importlib.util
import sys
import types
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_RS_DIR = _HERE.parent / "addon" / "FreeCADMCP" / "rpc_server"

# Save and restore: tests like ``test_parts_library`` replace
# ``sys.modules["FreeCAD"]`` with a minimal stub that lacks the
# ``Console`` attribute, which our command classes depend on. We
# re-install the full stub on every fixture so the commands module
# always sees a usable ``FreeCAD``.
import pytest


@pytest.fixture(autouse=True)
def _restore_freecad_shim():
    """Re-install the FreeCAD / PySide shim set before each test.

    Several other test files replace ``sys.modules["FreeCAD"]`` with
    a partial stub (e.g. ``test_parts_library`` only sets
    ``getUserAppDataDir``). Without this fixture, those tests pollute
    our environment and break commands that call
    ``FreeCAD.Console.PrintError``.
    """
    saved_fc = sys.modules.get("FreeCAD")
    saved_fcgui = sys.modules.get("FreeCADGui")
    saved_pyside = sys.modules.get("PySide")
    saved_objfem = sys.modules.get("ObjectsFem")

    fc = types.ModuleType("FreeCAD")
    fc.Console = types.SimpleNamespace(
        PrintWarning=lambda *a, **k: None,
        PrintMessage=lambda *a, **k: None,
        PrintError=lambda *a, **k: None,
    )
    fc.getUserAppDataDir = lambda: "/tmp"
    fc.newDocument = lambda *a, **k: None
    fc.getDocument = lambda *a, **k: None
    fc.listDocuments = lambda: {}
    fc.Document = type("Document", (), {})
    fc.DocumentObject = type("DocumentObject", (), {})
    fc.Vector = type("Vector", (), {})
    fc.Rotation = type("Rotation", (), {})
    fc.Placement = type("Placement", (), {})
    sys.modules["FreeCAD"] = fc

    fcgui = types.ModuleType("FreeCADGui")
    fcgui.ActiveDocument = None
    fcgui.Selection = types.SimpleNamespace(
        clearSelection=lambda: None, addSelection=lambda *a, **k: None
    )
    fcgui.SendMsgToActiveView = lambda *a, **k: None
    fcgui.addCommand = lambda *a, **k: None
    fcgui.getMainWindow = lambda: types.SimpleNamespace(findChildren=lambda *a, **k: [])
    sys.modules["FreeCADGui"] = fcgui

    pyside = types.ModuleType("PySide")
    pyside.QtCore = types.SimpleNamespace(
        QTimer=types.SimpleNamespace(singleShot=lambda *a, **k: None),
        QEventLoop=types.SimpleNamespace(AllEvents=0),
        QThread=types.SimpleNamespace(msleep=lambda *a, **k: None),
    )
    pyside.QtWidgets = types.SimpleNamespace(
        QApplication=type("QApplication", (), {
            "instance": staticmethod(lambda: None),
            "processEvents": lambda *a, **k: None,
        }),
        QInputDialog=type("QInputDialog", (), {}),
        QLineEdit=type("QLineEdit", (), {"Normal": 0}),
        QMessageBox=type("QMessageBox", (), {
            "warning": staticmethod(lambda *a, **k: None),
            "critical": staticmethod(lambda *a, **k: None),
        }),
        QAction=type("QAction", (), {}),
    )
    sys.modules["PySide"] = pyside

    objfem = types.ModuleType("ObjectsFem")
    objfem.makeMeshGmsh = lambda *a, **k: (None,)
    objfem.makeAnalysis = lambda *a, **k: None
    objfem.makeMaterialSolid = lambda *a, **k: None
    objfem.makeSolverCalculiXCcxTools = lambda *a, **k: None
    sys.modules["ObjectsFem"] = objfem

    # Reload the addon modules so they pick up the fresh shims.
    pkg_name = "_test_cmd_pkg"
    if pkg_name not in sys.modules:
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [str(_RS_DIR)]
        sys.modules[pkg_name] = pkg
    for sub in ("parts_library", "serialize", "_fem_workdir", "_request_tracking",
                "_security_gate", "_settings", "_screenshot", "_ip_allowlist",
                "_dispatch", "rpc_server"):
        spec = importlib.util.spec_from_file_location(
            f"{pkg_name}.{sub}", str(_RS_DIR / f"{sub}.py")
        )
        m = importlib.util.module_from_spec(spec)
        sys.modules[f"{pkg_name}.{sub}"] = m
        spec.loader.exec_module(m)  # type: ignore[union-attr]
    global commands
    commands = sys.modules[f"{pkg_name}._commands"]

    yield

    # Restore.
    if saved_fc is not None:
        sys.modules["FreeCAD"] = saved_fc
    if saved_fcgui is not None:
        sys.modules["FreeCADGui"] = saved_fcgui
    if saved_pyside is not None:
        sys.modules["PySide"] = saved_pyside
    if saved_objfem is not None:
        sys.modules["ObjectsFem"] = saved_objfem


# ---------------------------------------------------------------------------
# StartRPCServerCommand
# ---------------------------------------------------------------------------

def test_start_rpc_command_resources():
    cmd = commands.StartRPCServerCommand()
    r = cmd.GetResources()
    assert "MenuText" in r and r["MenuText"] == "Start RPC Server"


def test_start_rpc_command_activated_calls_rpc():
    """Activated() delegates to rpc_server.start_rpc_server()."""
    called = []
    rpc_mod = sys.modules["_test_cmd_pkg.rpc_server"]
    real = rpc_mod.start_rpc_server
    rpc_mod.start_rpc_server = lambda: called.append("start") or "started"
    try:
        commands.StartRPCServerCommand().Activated()
        assert called == ["start"]
    finally:
        rpc_mod.start_rpc_server = real


def test_start_rpc_command_is_active():
    assert commands.StartRPCServerCommand().IsActive() is True


# ---------------------------------------------------------------------------
# StopRPCServerCommand
# ---------------------------------------------------------------------------

def test_stop_rpc_command_resources():
    cmd = commands.StopRPCServerCommand()
    r = cmd.GetResources()
    assert r["MenuText"] == "Stop RPC Server"


def test_stop_rpc_command_activated_calls_rpc():
    called = []
    rpc_mod = sys.modules["_test_cmd_pkg.rpc_server"]
    real = rpc_mod.stop_rpc_server
    rpc_mod.stop_rpc_server = lambda: called.append("stop") or "stopped"
    try:
        commands.StopRPCServerCommand().Activated()
        assert called == ["stop"]
    finally:
        rpc_mod.stop_rpc_server = real


def test_stop_rpc_command_is_active():
    assert commands.StopRPCServerCommand().IsActive() is True


# ---------------------------------------------------------------------------
# ToggleRemoteConnectionsCommand — the security gate test
# ---------------------------------------------------------------------------

def test_toggle_remote_command_resources():
    cmd = commands.ToggleRemoteConnectionsCommand()
    r = cmd.GetResources()
    assert r["MenuText"] == "Remote Connections"
    assert r.get("Checkable") is True


def test_toggle_remote_command_unchecked_saves():
    """unchecked=False path saves remote_enabled=False."""
    captured: dict = {}
    real_load = commands.load_settings
    real_save = commands.save_settings
    commands.load_settings = lambda: {"remote_enabled": True, "allowed_ips": "127.0.0.1"}
    commands.save_settings = lambda s: captured.update(s)
    try:
        commands.ToggleRemoteConnectionsCommand().Activated(checked=0)
        assert captured["remote_enabled"] is False
    finally:
        commands.load_settings = real_load
        commands.save_settings = real_save


def test_toggle_remote_command_checked_security_gate_blocks():
    """checked=True with missing env vars -> refuses and shows dialog."""
    real_load = commands.load_settings
    real_save = commands.save_settings
    commands.load_settings = lambda: {"remote_enabled": False, "allowed_ips": "127.0.0.1"}
    commands.save_settings = lambda s: None
    saved_env = {}
    os_mod = sys.modules["os"]
    for var in ("FREECAD_MCP_TLS_CERT", "FREECAD_MCP_TLS_KEY", "FREECAD_MCP_AUTH_TOKEN"):
        saved_env[var] = os_mod.environ.pop(var, None)
    try:
        # Should not raise even though the gate denies.
        commands.ToggleRemoteConnectionsCommand().Activated(checked=1)
    finally:
        for var, val in saved_env.items():
            if val is not None:
                os_mod.environ[var] = val
        commands.load_settings = real_load
        commands.save_settings = real_save


def test_toggle_remote_command_checked_with_tls_succeeds():
    """checked=True with TLS + auth env vars -> remote_enabled is saved."""
    captured: dict = {}
    real_load = commands.load_settings
    real_save = commands.save_settings
    commands.load_settings = lambda: {"remote_enabled": False, "allowed_ips": "127.0.0.1"}
    commands.save_settings = lambda s: captured.update(s)
    os_mod = sys.modules["os"]
    saved_env = {v: os_mod.environ.get(v) for v in
                 ("FREECAD_MCP_TLS_CERT", "FREECAD_MCP_TLS_KEY", "FREECAD_MCP_AUTH_TOKEN")}
    os_mod.environ["FREECAD_MCP_TLS_CERT"] = "/tmp/cert.pem"
    os_mod.environ["FREECAD_MCP_TLS_KEY"] = "/tmp/key.pem"
    os_mod.environ["FREECAD_MCP_AUTH_TOKEN"] = "secret"
    try:
        commands.ToggleRemoteConnectionsCommand().Activated(checked=1)
        assert captured["remote_enabled"] is True
    finally:
        for v, val in saved_env.items():
            if val is None:
                os_mod.environ.pop(v, None)
            else:
                os_mod.environ[v] = val
        commands.load_settings = real_load
        commands.save_settings = real_save


def test_toggle_remote_command_saves_and_notifies_when_server_running():
    """When enabling remote and the RPC server is already running, the
    command prints a 'restart for changes to take effect' hint."""
    real_load = commands.load_settings
    real_save = commands.save_settings
    dispatch_mod = sys.modules["_test_cmd_pkg._dispatch"]
    saved_inst = dispatch_mod.rpc_server_instance
    dispatch_mod.rpc_server_instance = types.SimpleNamespace()  # truthy
    commands.load_settings = lambda: {"remote_enabled": False, "allowed_ips": "127.0.0.1"}
    commands.save_settings = lambda s: None
    os_mod = sys.modules["os"]
    saved_env = {v: os_mod.environ.get(v) for v in
                 ("FREECAD_MCP_TLS_CERT", "FREECAD_MCP_TLS_KEY", "FREECAD_MCP_AUTH_TOKEN")}
    os_mod.environ["FREECAD_MCP_TLS_CERT"] = "/tmp/cert.pem"
    os_mod.environ["FREECAD_MCP_TLS_KEY"] = "/tmp/key.pem"
    os_mod.environ["FREECAD_MCP_AUTH_TOKEN"] = "secret"
    try:
        commands.ToggleRemoteConnectionsCommand().Activated(checked=1)
    finally:
        for v, val in saved_env.items():
            if val is None:
                os_mod.environ.pop(v, None)
            else:
                os_mod.environ[v] = val
        commands.load_settings = real_load
        commands.save_settings = real_save
        dispatch_mod.rpc_server_instance = saved_inst


def test_toggle_remote_command_is_active():
    assert commands.ToggleRemoteConnectionsCommand().IsActive() is True


# ---------------------------------------------------------------------------
# ConfigureAllowedIPsCommand
# ---------------------------------------------------------------------------

def test_configure_allowed_ips_resources():
    cmd = commands.ConfigureAllowedIPsCommand()
    r = cmd.GetResources()
    assert "MenuText" in r
    assert r["MenuText"] == "Configure Allowed IPs"


def test_configure_allowed_ips_no_qt_does_nothing():
    """If PySide.QtWidgets is unavailable, log error and return."""
    real_load = commands.load_settings
    commands.load_settings = lambda: {"allowed_ips": "127.0.0.1"}
    saved_widgets = sys.modules["PySide"].QtWidgets
    sys.modules["PySide"].QtWidgets = None
    try:
        commands.ConfigureAllowedIPsCommand().Activated()
    finally:
        sys.modules["PySide"].QtWidgets = saved_widgets
        commands.load_settings = real_load


def test_configure_allowed_ips_cancelled_does_not_save():
    """User cancels the dialog -> settings not saved."""
    saved_save_called = []
    real_load = commands.load_settings
    real_save = commands.save_settings
    commands.load_settings = lambda: {"allowed_ips": "127.0.0.1"}
    commands.save_settings = lambda s: saved_save_called.append(s)

    class _StubInputDialog:
        Normal = 0
        @staticmethod
        def getText(*a, **kw):
            return ("", False)

    saved_widgets = sys.modules["PySide"].QtWidgets
    sys.modules["PySide"].QtWidgets = types.SimpleNamespace(
        QInputDialog=_StubInputDialog,
        QLineEdit=types.SimpleNamespace(Normal=0),
        QMessageBox=types.SimpleNamespace(warning=staticmethod(lambda *a, **k: None)),
    )
    try:
        commands.ConfigureAllowedIPsCommand().Activated()
        assert saved_save_called == []
    finally:
        sys.modules["PySide"].QtWidgets = saved_widgets
        commands.load_settings = real_load
        commands.save_settings = real_save


def test_configure_allowed_ips_valid_input_saved():
    """User enters a valid IP -> settings saved with normalized form."""
    captured: dict = {}
    real_load = commands.load_settings
    real_save = commands.save_settings
    commands.load_settings = lambda: {"allowed_ips": "127.0.0.1"}
    commands.save_settings = lambda s: captured.update(s)

    saved_widgets = sys.modules["PySide"].QtWidgets
    sys.modules["PySide"].QtWidgets = types.SimpleNamespace(
        QInputDialog=type("QInputDialog", (), {
            "Normal": 0,
            "getText": staticmethod(lambda *a, **kw: ("192.168.1.0/24, 10.0.0.0/8", True)),
        }),
        QLineEdit=type("QLineEdit", (), {"Normal": 0}),
        QMessageBox=type("QMessageBox", (), {"warning": staticmethod(lambda *a, **k: None)}),
    )
    try:
        commands.ConfigureAllowedIPsCommand().Activated()
        assert captured["allowed_ips"] == "192.168.1.0/24, 10.0.0.0/8"
    finally:
        sys.modules["PySide"].QtWidgets = saved_widgets
        commands.load_settings = real_load
        commands.save_settings = real_save


def test_configure_allowed_ips_invalid_input_warns_and_skips():
    """User enters all invalid entries -> settings not changed."""
    real_load = commands.load_settings
    real_save = commands.save_settings
    commands.load_settings = lambda: {"allowed_ips": "127.0.0.1"}
    commands.save_settings = lambda s: None
    saved_widgets = sys.modules["PySide"].QtWidgets
    sys.modules["PySide"].QtWidgets = types.SimpleNamespace(
        QInputDialog=type("QInputDialog", (), {
            "Normal": 0,
            "getText": staticmethod(lambda *a, **kw: ("garbage, also-bad", True)),
        }),
        QLineEdit=type("QLineEdit", (), {"Normal": 0}),
        QMessageBox=type("QMessageBox", (), {"warning": staticmethod(lambda *a, **k: None)}),
    )
    try:
        commands.ConfigureAllowedIPsCommand().Activated()
    finally:
        sys.modules["PySide"].QtWidgets = saved_widgets
        commands.load_settings = real_load
        commands.save_settings = real_save


def test_configure_allowed_ips_is_active():
    assert commands.ConfigureAllowedIPsCommand().IsActive() is True


# ---------------------------------------------------------------------------
# ToggleAutoStartCommand
# ---------------------------------------------------------------------------

def test_toggle_auto_start_resources():
    cmd = commands.ToggleAutoStartCommand()
    r = cmd.GetResources()
    assert r["MenuText"] == "Auto-Start Server"
    assert r.get("Checkable") is True


def test_toggle_auto_start_unchecked():
    captured: dict = {}
    real_load = commands.load_settings
    real_save = commands.save_settings
    commands.load_settings = lambda: {"auto_start_rpc": True}
    commands.save_settings = lambda s: captured.update(s)
    try:
        commands.ToggleAutoStartCommand().Activated(checked=0)
        assert captured["auto_start_rpc"] is False
    finally:
        commands.load_settings = real_load
        commands.save_settings = real_save


def test_toggle_auto_start_checked_without_tls_blocks():
    """Auto-start with non-loopback would need TLS; without it, refuse."""
    real_load = commands.load_settings
    real_save = commands.save_settings
    commands.load_settings = lambda: {"auto_start_rpc": False}
    commands.save_settings = lambda s: None
    saved_env = {}
    os_mod = sys.modules["os"]
    for var in ("FREECAD_MCP_TLS_CERT", "FREECAD_MCP_TLS_KEY", "FREECAD_MCP_AUTH_TOKEN"):
        saved_env[var] = os_mod.environ.pop(var, None)
    try:
        commands.ToggleAutoStartCommand().Activated(checked=1)
    finally:
        for var, val in saved_env.items():
            if val is not None:
                os_mod.environ[var] = val
        commands.load_settings = real_load
        commands.save_settings = real_save


def test_toggle_auto_start_checked_with_tls_succeeds():
    captured: dict = {}
    real_load = commands.load_settings
    real_save = commands.save_settings
    commands.load_settings = lambda: {"auto_start_rpc": False}
    commands.save_settings = lambda s: captured.update(s)
    os_mod = sys.modules["os"]
    saved_env = {v: os_mod.environ.get(v) for v in
                 ("FREECAD_MCP_TLS_CERT", "FREECAD_MCP_TLS_KEY", "FREECAD_MCP_AUTH_TOKEN")}
    os_mod.environ["FREECAD_MCP_TLS_CERT"] = "/tmp/cert.pem"
    os_mod.environ["FREECAD_MCP_TLS_KEY"] = "/tmp/key.pem"
    os_mod.environ["FREECAD_MCP_AUTH_TOKEN"] = "secret"
    try:
        commands.ToggleAutoStartCommand().Activated(checked=1)
        assert captured["auto_start_rpc"] is True
    finally:
        for v, val in saved_env.items():
            if val is None:
                os_mod.environ.pop(v, None)
            else:
                os_mod.environ[v] = val
        commands.load_settings = real_load
        commands.save_settings = real_save


def test_toggle_auto_start_is_active():
    assert commands.ToggleAutoStartCommand().IsActive() is True


# ---------------------------------------------------------------------------
# _sync_toggle_states
# ---------------------------------------------------------------------------

def test_sync_toggle_states_no_freecadgui_no_op():
    """If FreeCADGui is not importable, the helper silently returns."""
    commands._sync_toggle_states()


def test_sync_toggle_states_with_qt_runs_sync():
    """When the menu is found, the helper sets the QAction checked state."""
    real_load = commands.load_settings
    commands.load_settings = lambda: {"remote_enabled": True, "auto_start_rpc": False}

    actions_found: list[tuple[str, bool]] = []
    class _Action:
        def __init__(self, name):
            self._name = name
            self._checked = False
        def text(self):
            return self._name
        def setChecked(self, value):
            self._checked = value
            actions_found.append((self._name, value))

    action_remote = _Action("Remote Connections")
    action_auto = _Action("Auto-Start Server")
    action_other = _Action("Other")

    saved_gui = sys.modules["FreeCADGui"]
    sys.modules["FreeCADGui"].getMainWindow = lambda: types.SimpleNamespace(
        findChildren=lambda *a, **kw: [action_other, action_remote, action_auto],
    )
    saved_qt_widgets = sys.modules["PySide"].QtWidgets
    saved_qt_core = sys.modules["PySide"].QtCore
    sys.modules["PySide"].QtWidgets = types.SimpleNamespace(
        QApplication=type("QApplication", (), {"instance": staticmethod(lambda: None)}),
        QAction=type("QAction", (), {}),
    )
    sys.modules["PySide"].QtCore = types.SimpleNamespace(
        QTimer=types.SimpleNamespace(singleShot=lambda *a, **k: None),
    )
    try:
        commands._sync_toggle_states()
        assert ("Remote Connections", True) in actions_found
        assert ("Auto-Start Server", False) in actions_found
    finally:
        sys.modules["FreeCADGui"] = saved_gui
        sys.modules["PySide"].QtWidgets = saved_qt_widgets
        sys.modules["PySide"].QtCore = saved_qt_core
        commands.load_settings = real_load


def test_sync_toggle_states_partial_match_retries():
    """When not all toggles are found, the helper reschedules itself."""
    real_load = commands.load_settings
    commands.load_settings = lambda: {"remote_enabled": True, "auto_start_rpc": True}

    class _Action:
        def __init__(self, name):
            self._name = name
            self._checked = False
        def text(self):
            return self._name
        def setChecked(self, value):
            self._checked = value

    only_remote = _Action("Remote Connections")
    saved_gui = sys.modules["FreeCADGui"]
    sys.modules["FreeCADGui"].getMainWindow = lambda: types.SimpleNamespace(
        findChildren=lambda *a, **kw: [only_remote],
    )
    saved_qt_widgets = sys.modules["PySide"].QtWidgets
    saved_qt_core = sys.modules["PySide"].QtCore
    sys.modules["PySide"].QtWidgets = types.SimpleNamespace(
        QApplication=type("QApplication", (), {"instance": staticmethod(lambda: None)}),
        QAction=type("QAction", (), {}),
    )
    scheduled: list = []
    sys.modules["PySide"].QtCore = types.SimpleNamespace(
        QTimer=types.SimpleNamespace(singleShot=lambda ms, fn: scheduled.append((ms, fn))),
    )
    try:
        commands._sync_toggle_states()
        # We didn't find Auto-Start Server -> rescheduled after 2 s.
        assert any(ms == 2000 for ms, _ in scheduled)
    finally:
        sys.modules["FreeCADGui"] = saved_gui
        sys.modules["PySide"].QtWidgets = saved_qt_widgets
        sys.modules["PySide"].QtCore = saved_qt_core
        commands.load_settings = real_load


def test_sync_toggle_states_qt_missing_returns():
    """When the `from PySide import QtCore, QtWidgets` line raises
    ImportError, the helper returns silently."""
    real_load = commands.load_settings
    commands.load_settings = lambda: {}
    # Patch PySide so the inner `from PySide import QtCore, QtWidgets` fails.
    saved_pyside = sys.modules["PySide"]
    sys.modules["PySide"] = types.ModuleType("PySideNoQt")
    try:
        commands._sync_toggle_states()
    finally:
        sys.modules["PySide"] = saved_pyside
        commands.load_settings = real_load


def test_sync_toggle_states_exception_in_getmainwindow():
    """If getMainWindow raises, the helper reschedules itself."""
    real_load = commands.load_settings
    commands.load_settings = lambda: {}
    saved_gui = sys.modules["FreeCADGui"]
    sys.modules["FreeCADGui"].getMainWindow = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    saved_widgets = sys.modules["PySide"].QtWidgets
    saved_core = sys.modules["PySide"].QtCore
    sys.modules["PySide"].QtWidgets = types.SimpleNamespace(
        QApplication=type("QApplication", (), {"instance": staticmethod(lambda: None)}),
        QAction=type("QAction", (), {}),
    )
    scheduled: list = []
    sys.modules["PySide"].QtCore = types.SimpleNamespace(
        QTimer=types.SimpleNamespace(singleShot=lambda ms, fn: scheduled.append((ms, fn))),
    )
    try:
        commands._sync_toggle_states()
        assert any(ms == 2000 for ms, _ in scheduled)
    finally:
        sys.modules["FreeCADGui"] = saved_gui
        sys.modules["PySide"].QtWidgets = saved_widgets
        sys.modules["PySide"].QtCore = saved_core
        commands.load_settings = real_load