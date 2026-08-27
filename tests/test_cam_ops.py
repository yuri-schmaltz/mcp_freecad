"""Tests for the cam_ops module (v1.1.2).

The Path workbench is not importable in unit tests, so these
tests stub ``FreeCAD`` / ``Path`` / ``PathScripts`` and exercise
the API contract.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

_RPC_DIR = Path(__file__).resolve().parent.parent / "addon/FreeCADMCP/rpc_server"


class _FakeType:
    def __init__(self, name):
        self._name = name

    def __name__(self):
        return self._name


def _install_stubs(monkeypatch):
    """Install minimal FreeCAD / Path stubs."""
    fake_freecad = types.ModuleType("FreeCAD")
    fake_path = types.ModuleType("Path")
    fake_path_scripts = types.ModuleType("PathScripts")
    fake_path_tool = types.ModuleType("PathTool")
    fake_path_tool_ctrl = types.ModuleType("PathToolController")

    fake_path_tools = types.ModuleType("PathScripts.tools")
    fake_post_utils = types.ModuleType("PathScripts.PostUtils")
    fake_path_profile = types.ModuleType("PathScripts.PathProfile")
    fake_path_pocket = types.ModuleType("PathScripts.PathPocket")
    fake_path_face = types.ModuleType("PathScripts.PathFace")
    fake_path_drilling = types.ModuleType("PathScripts.PathDrilling")
    fake_path_adaptive = types.ModuleType("PathScripts.PathAdaptive")

    for mod in (
        fake_freecad, fake_path, fake_path_scripts, fake_path_tool,
        fake_path_tool_ctrl, fake_path_tools, fake_post_utils,
        fake_path_profile, fake_path_pocket, fake_path_face,
        fake_path_drilling, fake_path_adaptive,
    ):
        sys.modules[mod.__name__] = mod

    fake_post_utils.PostProcessor = types.SimpleNamespace
    fake_path_profile.ObjectProfile = type("ObjectProfile", (), {})
    fake_path_pocket.ObjectPocket = type("ObjectPocket", (), {})
    fake_path_face.ObjectFace = type("ObjectFace", (), {})
    fake_path_drilling.ObjectDrilling = type("ObjectDrilling", (), {})
    fake_path_adaptive.ObjectAdaptive = type("ObjectAdaptive", (), {})

    # Build a fake document with addObject/getObject.
    docs = {}

    class FakeFeature:
        def __init__(self, name, type_id, props=None):
            self.Name = name
            self.TypeId = type_id
            self.Operations = []
            self.props = props or {}

        def __setattr__(self, key, value):
            super().__setattr__(key, value)
            if key in ("Base", "Stock", "ToolController", "Tool", "ToolType",
                       "Diameter", "Length", "Material", "SpindleSpeed",
                       "FeedRate", "FeedRateVertical", "Side", "StepDown",
                       "OpType", "Path"):
                self.props[key] = value

        def __getattr__(self, key):
            if key in self.props:
                return self.props[key]
            raise AttributeError(key)

    class FakeDoc:
        def __init__(self, name):
            self.Name = name
            self._objects = {}

        def addObject(self, type_id, name):
            obj = FakeFeature(name, type_id)
            self._objects[name] = obj
            return obj

        def getObject(self, name):
            return self._objects.get(name)

        def recompute(self):
            pass

    fake_freecad.getDocument = lambda name: docs.get(name)
    fake_freecad.ActiveDocument = None
    fake_freecad.Console = types.SimpleNamespace(PrintWarning=lambda *a, **k: None)

    def make_doc(name):
        doc = FakeDoc(name)
        docs[name] = doc
        return doc

    return fake_freecad, docs, make_doc


@pytest.fixture
def cam_mod(monkeypatch):
    fake_freecad, _docs, _make_doc = _install_stubs(monkeypatch)
    spec = importlib.util.spec_from_file_location(
        "_cam_ops_for_test", _RPC_DIR / "cam_ops.py"
    )
    mod = importlib.util.module_from_spec(spec)
    # Pre-stub FreeCAD so the module's top-level import succeeds.
    # Use monkeypatch so the real ``FreeCAD`` (and friends) are restored
    # after the test — otherwise subsequent tests that import the real
    # rpc_server see the broken stub and explode.
    monkeypatch.setitem(sys.modules, "FreeCAD", fake_freecad)
    monkeypatch.setitem(sys.modules, "Path", sys.modules["Path"])
    monkeypatch.setitem(sys.modules, "PathScripts", sys.modules["PathScripts"])
    monkeypatch.setitem(sys.modules, "PathTool", sys.modules["PathTool"])
    monkeypatch.setitem(sys.modules, "PathToolController", sys.modules["PathToolController"])
    monkeypatch.setitem(sys.modules, "PathScripts.tools", sys.modules["PathScripts.tools"])
    sys.modules["_cam_ops_for_test"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    # Re-patch FreeCAD after exec (the module already grabbed it).
    monkeypatch.setattr(mod, "FreeCAD", fake_freecad)
    monkeypatch.setattr(mod, "Path", sys.modules["Path"])
    monkeypatch.setattr(mod, "PathScripts", sys.modules["PathScripts"])
    return mod, fake_freecad


def test_path_unavailable(cam_mod) -> None:
    mod, _ = cam_mod
    mod.FreeCAD = None
    mod.Path = None
    res = mod.cam_create_tool("Doc", "T1")
    assert res["success"] is False


def test_create_tool(cam_mod) -> None:
    mod, fc = cam_mod
    docs = {}

    class ToolObj:
        def __init__(self, name):
            self.Name = name
            self.ToolType = None
            self.Diameter = None
            self.Length = None
            self.Material = None

    class Doc:
        def addObject(self, type_id, name):
            obj = ToolObj(name)
            docs.setdefault("_objs", {})[name] = obj
            return obj

        def getObject(self, name):
            return docs.get("_objs", {}).get(name)

        def recompute(self):
            pass

    fc.getDocument = lambda n: docs.get(n)
    fc.getDocument.cache_clear = lambda: None  # type: ignore[attr-defined]
    docs["D"] = Doc()

    res = mod.cam_create_tool("D", "T1", diameter=10.0, length=80.0)
    assert res["success"] is True
    assert res["tool_name"] == "T1"
    assert res["diameter"] == 10.0


def test_unknown_op_type(cam_mod) -> None:
    mod, fc = cam_mod
    docs = {}

    class Doc:
        def __init__(self):
            self._objs = {}

        def addObject(self, *a, **k):
            raise RuntimeError("should not be called")

        def getObject(self, name):
            return self._objs.get(name)

        def recompute(self):
            pass

    d = Doc()
    docs["D"] = d
    fc.getDocument = lambda n: docs.get(n)
    res = mod.cam_add_operation("D", "Job1", "weird_op", "Op1")
    assert res["success"] is False


def test_simulation_no_job(cam_mod) -> None:
    mod, fc = cam_mod
    fc.getDocument = lambda n: None
    res = mod.cam_simulate_toolpath("D", "Job1")
    assert res["success"] is False


def test_post_process_no_doc(cam_mod) -> None:
    mod, fc = cam_mod
    fc.getDocument = lambda n: None
    res = mod.cam_post_process("D", "Job1")
    assert res["success"] is False


def test_create_job_missing_base(cam_mod) -> None:
    mod, fc = cam_mod
    docs = {}

    class JobObj:
        def __init__(self, name):
            self.Name = name
            self.Base = None
            self.ToolController = None
            self.Stock = None

    class Doc:
        def __init__(self):
            self._objs = {}

        def addObject(self, type_id, name):
            o = JobObj(name)
            self._objs[name] = o
            return o

        def getObject(self, name):
            return self._objs.get(name)

        def recompute(self):
            pass

    d = Doc()
    docs["D"] = d
    fc.getDocument = lambda n: docs.get(n)
    res = mod.cam_create_job("D", "Job1", base_shape="MissingShape")
    assert res["success"] is False


def test_create_job_missing_tool_controller(cam_mod) -> None:
    mod, fc = cam_mod
    docs = {}

    class BaseObj:
        Name = "Base"

    class Doc:
        def __init__(self):
            self._objs = {"Base": BaseObj()}

        def addObject(self, type_id, name):
            o = types.SimpleNamespace(Name=name, Base=None, ToolController=None, Stock=None)
            self._objs[name] = o
            return o

        def getObject(self, name):
            return self._objs.get(name)

        def recompute(self):
            pass

    d = Doc()
    docs["D"] = d
    fc.getDocument = lambda n: docs.get(n)
    res = mod.cam_create_job("D", "Job1", base_shape="Base", tool_controller_name="Nope")
    assert res["success"] is False


def test_valid_ops_constant(cam_mod) -> None:
    mod, _ = cam_mod
    assert "profile" in mod._VALID_OPS
    assert "pocket" in mod._VALID_OPS
    assert "adaptive" in mod._VALID_OPS


def test_simulate_returns_points(cam_mod) -> None:
    mod, fc = cam_mod
    docs = {}

    class Cmd:
        def __init__(self, x, y, z):
            self.x = x
            self.y = y
            self.z = z

    class Op:
        Path = types.SimpleNamespace(Commands=[Cmd(1, 2, 3), Cmd(4, 5, 6)])

    class JobObj:
        def __init__(self):
            self.Operations = [Op()]

    class Doc:
        def __init__(self):
            self._objs = {"Job1": JobObj()}

        def getObject(self, name):
            return self._objs.get(name)

    d = Doc()
    docs["D"] = d
    fc.getDocument = lambda n: docs.get(n)
    res = mod.cam_simulate_toolpath("D", "Job1", max_segments=10)
    assert res["success"] is True
    assert res["point_count"] == 2
    assert res["points"][0] == [1.0, 2.0, 3.0]
