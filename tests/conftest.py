"""Shared pytest fixtures for the mcp-freecad test suite.

Before this conftest existed, every test file (~19 of them) re-defined
its own FreeCAD / PySide / ObjectsFem stubs inline. Inconsistent shims
silently masked bugs (e.g. one test exposing ``PySide.QtWidgets.QFrame``
while another did not). Centralising the shim here gives every test the
same baseline and lets each test focus on its own behaviour.

The fixture installs the stubs at collection time (autouse, function
scope) so even tests that do not import the addon explicitly still run
in a clean environment.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

# ----------------------------------------------------------------------
# Addon source dirs are importable by file path. We do this in
# ``pytest_configure`` (autoload at collection time) so test_*.py files
# can simply ``import rpc_server.rpc_server`` without first setting up
# sys.path themselves.
# ----------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ADDON_DIR = _REPO_ROOT / "addon" / "FreeCADMCP"
_RPC_SERVER_DIR = _ADDON_DIR / "rpc_server"
_SRC_DIR = _REPO_ROOT / "src"

for _p in (str(_REPO_ROOT), str(_ADDON_DIR), str(_SRC_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ----------------------------------------------------------------------
# FreeCAD / FreeCADGui / ObjectsFem / PySide stubs.
# ----------------------------------------------------------------------


def _install_freecad_stub() -> None:
    """Install a rich FreeCAD/PySide stub into ``sys.modules``.

    **Never** overwrites an existing module wholesale — only fills in
    attributes that are missing. This protects tests that monkeypatch
    individual attributes (``test_settings_fallback`` records
    ``FreeCAD.Console.PrintWarning`` calls, e.g.) from being clobbered
    when the autouse fixture runs before each test. The trade-off: if
    a previous test installed a *bare* SimpleNamespace that lacks
    ``QFrame`` etc., this fixture augments it in place instead of
    swapping it out — but the autouse ordering means the rich version
    is always present by the time a test that needs ``QFrame`` runs.
    """
    _NEEDED_QWIDGETS = (
        "QFrame",
        "QWidget",
        "QLabel",
        "QLineEdit",
        "QPlainTextEdit",
        "QPushButton",
        "QCheckBox",
        "QComboBox",
        "QHBoxLayout",
        "QVBoxLayout",
        "QApplication",
        "QAction",
        "QDockWidget",
        "QTextEdit",
        "QInputDialog",
        "QMessageBox",
        "QSizePolicy",
    )

    def _ensure_module(name: str, attrs: dict[str, object]) -> types.ModuleType:
        mod = sys.modules.get(name)
        if mod is None:
            mod = types.ModuleType(name)
            sys.modules[name] = mod
        for k, v in attrs.items():
            if not hasattr(mod, k):
                setattr(mod, k, v)
        return mod

    fc_attrs: dict[str, object] = {
        "Console": types.SimpleNamespace(
            PrintMessage=lambda *a, **k: None,
            PrintWarning=lambda *a, **k: None,
            PrintError=lambda *a, **k: None,
        ),
        "getUserAppDataDir": lambda: "/tmp",
        "newDocument": lambda *a, **k: None,
        "getDocument": lambda *a, **k: None,
        "listDocuments": lambda: {},
        "Document": type("Document", (), {}),
        "DocumentObject": type("DocumentObject", (), {}),
        "Vector": type("Vector", (), {}),
        "Rotation": type("Rotation", (), {}),
        "Placement": type("Placement", (), {}),
    }
    # Special-case Console: tests may have set PrintWarning to a
    # recorder; only populate the whole Console if neither method exists.
    fc = sys.modules.get("FreeCAD")
    if fc is None or not hasattr(fc, "Console"):
        _ensure_module("FreeCAD", fc_attrs)
    else:
        for k in ("PrintMessage", "PrintWarning", "PrintError"):
            if not hasattr(fc.Console, k):
                setattr(fc.Console, k, lambda *a, **k: None)
        for k, v in fc_attrs.items():
            if k == "Console":
                continue
            if not hasattr(fc, k):
                setattr(fc, k, v)

    _ensure_module(
        "FreeCADGui",
        {
            "ActiveDocument": None,
            "Selection": types.SimpleNamespace(
                clearSelection=lambda: None,
                addSelection=lambda *a, **k: None,
            ),
            "SendMsgToActiveView": lambda *a, **k: None,
            "addCommand": lambda *a, **k: None,
            "addWorkbench": lambda *a, **k: None,
            "getMainWindow": lambda: types.SimpleNamespace(
                findChildren=lambda *a, **k: [],
                findChild=lambda *a, **k: None,
                addDockWidget=lambda *a, **k: None,
            ),
            "updateGui": lambda: None,
        },
    )
    _ensure_module(
        "ObjectsFem",
        {
            "makeMeshGmsh": lambda *a, **k: (None,),
            "makeAnalysis": lambda *a, **k: None,
            "makeMaterialSolid": lambda *a, **k: None,
            "makeSolverCalculiXCcxTools": lambda *a, **k: None,
        },
    )

    qc_attrs = {
        "QTimer": types.SimpleNamespace(
            singleShot=lambda *a, **k: None, NotRunning=0
        ),
        "QEventLoop": types.SimpleNamespace(AllEvents=0),
        "QThread": types.SimpleNamespace(msleep=lambda *a, **k: None),
        "Qt": types.SimpleNamespace(
            PointingHandCursor=0,
            LeftDockWidgetArea=0,
            RightDockWidgetArea=0,
            transparent=0,
            AlignVCenter=0,
            End=0,
        ),
    }
    _ensure_module("PySide.QtCore", qc_attrs)

    qg_attrs = {
        n: type(n, (), {"End": 0})
        for n in ("QPixmap", "QPainter", "QColor", "QPen", "QTextCursor")
    }
    _ensure_module("PySide.QtGui", qg_attrs)

    qw_attrs: dict[str, object] = {
        n: type(n, (), {"Normal": 0})
        for n in _NEEDED_QWIDGETS
        if n
        not in (
            "QInputDialog",
            "QMessageBox",
            "QSizePolicy",
        )
    }
    qw_attrs["QSizePolicy"] = type(
        "QSizePolicy",
        (),
        {
            "Normal": 0,
            "Expanding": 1,
            "Fixed": 2,
            "Preferred": 3,
            "Minimum": 4,
            "Maximum": 5,
            "Ignored": 6,
        },
    )
    qw_attrs["QInputDialog"] = type(
        "QInputDialog",
        (),
        {
            "Normal": 0,
            "getText": staticmethod(lambda *a, **k: ("127.0.0.1", True)),
            "getItem": staticmethod(lambda *a, **k: ("", True)),
        },
    )
    qw_attrs["QMessageBox"] = type(
        "QMessageBox",
        (),
        {
            "Normal": 0,
            "critical": staticmethod(lambda *a, **k: None),
            "warning": staticmethod(lambda *a, **k: None),
            "information": staticmethod(lambda *a, **k: None),
            "Yes": 1,
            "No": 0,
        },
    )
    qw = _ensure_module("PySide.QtWidgets", qw_attrs)
    if not hasattr(qw.QApplication, "instance"):
        qw.QApplication.instance = staticmethod(lambda: None)
        qw.QApplication.processEvents = lambda *a, **k: None

    sys.modules.setdefault("PySide", types.ModuleType("PySide"))


@pytest.fixture(autouse=True)
def _install_freecad_stub_fixture() -> None:
    """Autouse fixture: makes sure every test sees a working FreeCAD stub."""
    _install_freecad_stub()
    yield


# ----------------------------------------------------------------------
# Helpers for tests that want a clean, freshly-loaded addon.
# ----------------------------------------------------------------------


@pytest.fixture
def load_rpc_server():
    """Yield a callable that imports the rpc_server module under a synthetic package.

    Each call returns a fresh module instance — useful when a test wants
    to inspect module-level state (``rpc_server_instance``,
    ``rpc_server_thread``) without being polluted by other tests in the
    same process. The synthetic package wires up every submodule so the
    relative ``from ._X import …`` chain inside ``rpc_server.py``
    resolves correctly.

    Re-installs the rich Qt stub before each call so tests that
    previously replaced ``PySide.QtWidgets`` with a plain
    ``SimpleNamespace`` cannot crash the import.
    """
    _SUBMODS = (
        "parts_library",
        "serialize",
        "_fem_workdir",
        "_request_tracking",
        "_ip_allowlist",
        "_settings",
        "_dispatch",
        "_screenshot",
        "_security_gate",
        "_commands",
        "_panel",
        "mesh_to_solid",
        "step_metadata",
        "bom",
        "fem_post_process",
        "inspection",
        "multi_instance",
        "api_introspect",
        "job_runner",
        "cam_ops",
    )

    def _loader() -> types.ModuleType:
        # Re-install the rich shim before importing. Some tests (e.g.
        # test_settings_fallback) overwrite PySide.QtWidgets with a bare
        # SimpleNamespace; without this reset the _panel submodule would
        # fail to import ``QtWidgets.QFrame`` etc.
        _install_freecad_stub()
        # ``test_settings_fallback`` also assigns ``PySide.QtWidgets`` as
        # an *attribute* of the ``PySide`` parent module, so a plain
        # ``from PySide import QtWidgets`` inside _panel.py finds the
        # bare SimpleNamespace instead of the rich sys.modules entry.
        # Remove the parent-module attribute so Python falls back to the
        # rich module in sys.modules.
        for _attr in ("QtCore", "QtGui", "QtWidgets", "QtNetwork"):
            _pyside = sys.modules.get("PySide")
            if _pyside is not None and hasattr(_pyside, _attr):
                try:
                    delattr(_pyside, _attr)
                except AttributeError:
                    pass
        assert hasattr(sys.modules["PySide.QtWidgets"], "QFrame"), (
            "shim install did not restore QtWidgets.QFrame — cannot import _panel"
        )

        pkg_name = "_conftest_rpc_server_pkg"
        # Drop any previously-cached version of this synthetic package
        # so spec_from_file_location re-imports (and re-runs) every
        # submodule from scratch. Without this, a sub that imported
        # earlier under a now-broken PySide.QtWidgets would be reused
        # as-is.
        for cached in list(sys.modules):
            if cached.startswith(pkg_name + ".") or cached == pkg_name:
                sys.modules.pop(cached, None)

        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [str(_RPC_SERVER_DIR)]
        sys.modules[pkg_name] = pkg
        for sub in _SUBMODS:
            spec = importlib.util.spec_from_file_location(
                f"{pkg_name}.{sub}", str(_RPC_SERVER_DIR / f"{sub}.py")
            )
            mod = importlib.util.module_from_spec(spec)
            sys.modules[f"{pkg_name}.{sub}"] = mod
            spec.loader.exec_module(mod)  # type: ignore[union-attr]
        spec = importlib.util.spec_from_file_location(
            f"{pkg_name}.rpc_server", str(_RPC_SERVER_DIR / "rpc_server.py")
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[f"{pkg_name}.rpc_server"] = mod
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod

    return _loader


@pytest.fixture
def free_port():
    """Return a TCP port number that is currently free on localhost.

    The socket is closed before returning the port, so callers may bind
    to it immediately. Race window between close and bind is small
    enough for unit tests.
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ----------------------------------------------------------------------
# pytest_configure: run shim install once at collection time too.
# ----------------------------------------------------------------------


def pytest_configure(config: pytest.Config) -> None:
    _install_freecad_stub()


# Quiet down hypothesis if it shows up.
_hyp = sys.modules.get("hypothesis")
if _hyp is not None:
    _hyp.settings.register_profile(
        "ci",
        deadline=None,
        print_blob=False,
    )
