"""Tests for addon/FreeCADMCP/rpc_server/mesh_to_solid.py.

Loads the module via importlib because the addon directory is not
on sys.path by default. Stubs ``FreeCAD`` / ``MeshPart`` / ``Part`` /
``Mesh`` with minimal stand-ins so the code paths that *don't*
touch FreeCAD (validation, error reporting) can be exercised
without a running FreeCAD session.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ADDON_DIR = Path(__file__).resolve().parents[1] / "addon" / "FreeCADMCP" / "rpc_server"


class _FakeMesh:
    def __init__(self) -> None:
        self.CountFacets = 0
        self.CountPoints = 0

    def read(self, path):  # noqa: ARG002
        self.CountFacets = 12
        self.CountPoints = 8

    def decimate(self, reduction):  # noqa: ARG002
        new = _FakeMesh()
        new.CountFacets = 5_000
        new.CountPoints = 2_500
        return new


class _FakeDocument:
    """Minimal ``Document`` stand-in recording the objects added."""

    def __init__(self, name: str = "Doc") -> None:
        self.Name = name
        self.Label = name
        self.objects: dict[str, "_FakeFeature"] = {}

    def addObject(self, type_id: str, label: str) -> "_FakeFeature":
        feature = _FakeFeature(type_id=type_id, name=label)
        self.objects[label] = feature
        return feature

    def recompute(self) -> None:
        pass

    def getObject(self, name: str):
        return self.objects.get(name)


class _FakeFeature:
    def __init__(self, type_id: str, name: str) -> None:
        self.TypeId = type_id
        self.Name = name
        self.Label = name
        # The mesh's CountFacets depends on the test — default
        # 0 (class attribute); tests that need a non-zero value
        # mutate the instance after construction.
        self.Mesh = _FakeMesh()

    def isDerivedFrom(self, type_id: str) -> bool:
        return self.TypeId == type_id


class _FakeShape:
    def __init__(self) -> None:
        self.Faces = [object(), object(), object()]

    Volume = 123.4

    def isNull(self) -> bool:
        return False

    def fix(self, *args, **kwargs):
        return self

    def sewShape(self):
        return self


class _FakeSolid(_FakeShape):
    pass


@pytest.fixture
def mesh_mod(monkeypatch):
    """Load ``mesh_to_solid`` with FreeCAD / MeshPart / Mesh stubs."""
    stub_freecad = types.SimpleNamespace()
    stub_doc = _FakeDocument()

    # Pre-populate the doc with a Mesh::Feature so most tests have
    # a mesh to operate on.
    stub_doc.objects["MyMesh"] = _FakeFeature(
        type_id="Mesh::Feature", name="MyMesh"
    )

    def _fake_get_document(name: str):
        if name == "MissingDoc":
            return None
        return stub_doc

    stub_freecad.ActiveDocument = stub_doc
    stub_freecad.getDocument = _fake_get_document
    stub_freecad.Console = types.SimpleNamespace(PrintWarning=lambda *a, **k: print("WARN:", a[0] if a else ""))
    stub_freecad.Mesh = types.SimpleNamespace(Feature=_FakeFeature)

    fake_mesh = types.ModuleType("Mesh")
    fake_mesh.Mesh = _FakeMesh
    sys.modules["Mesh"] = fake_mesh

    fake_meshpart = types.ModuleType("MeshPart")
    fake_part = types.ModuleType("Part")
    sys.modules["MeshPart"] = fake_meshpart
    sys.modules["Part"] = fake_part

    spec = importlib.util.spec_from_file_location(
        "_mesh_to_solid_for_test", ADDON_DIR / "mesh_to_solid.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    monkeypatch.setattr(module, "FreeCAD", stub_freecad, raising=False)
    monkeypatch.setattr(module, "MeshPart", fake_meshpart, raising=False)
    monkeypatch.setattr(module, "Part", fake_part, raising=False)
    monkeypatch.setattr(module, "Mesh", fake_mesh, raising=False)

    return module, stub_doc, fake_meshpart, fake_part


def test_mesh_import_rejects_empty_path(mesh_mod) -> None:
    mod, _doc, _mp, _p = mesh_mod
    res = mod.mesh_import(path="")
    assert res["success"] is False
    assert "path is required" in res["reason"]


def test_mesh_import_rejects_relative_path(mesh_mod) -> None:
    mod, _doc, _mp, _p = mesh_mod
    res = mod.mesh_import(path="relative.stl")
    assert res["success"] is False
    assert "absolute" in res["reason"]


def test_mesh_import_rejects_missing_file(mesh_mod) -> None:
    mod, _doc, _mp, _p = mesh_mod
    res = mod.mesh_import(path="/nonexistent/path.stl")
    assert res["success"] is False
    assert "not found" in res["reason"]


def test_mesh_import_rejects_unsupported_extension(mesh_mod) -> None:
    """Force the extension-check branch by stubbing ``os.path.exists``."""
    import os as _real_os
    mod, _doc, _mp, _p = mesh_mod
    real_exists = _real_os.path.exists
    _real_os.path.exists = lambda path: True
    try:
        res = mod.mesh_import(path="/nonexistent/foo.txt")
    finally:
        _real_os.path.exists = real_exists
    assert res["success"] is False
    assert "unsupported mesh extension" in res["reason"]


def test_mesh_import_succeeds(mesh_mod, tmp_path) -> None:
    mod, doc, _mp, _p = mesh_mod
    stl = tmp_path / "demo.stl"
    stl.write_text("solid empty\nendsolid empty\n")
    res = mod.mesh_import(path=str(stl), label="NewMesh")
    assert res["success"] is True
    assert res["object_name"] == "NewMesh"
    assert res["triangle_count"] == 12
    assert res["vertex_count"] == 8
    assert "NewMesh" in doc.objects


def test_mesh_simplify_skips_when_already_small(mesh_mod) -> None:
    mod, _doc, _mp, _p = mesh_mod
    res = mod.mesh_simplify(doc_name="Doc", mesh_name="MyMesh", target_faces=10_000)
    assert res["success"] is True
    assert res["skipped"] is True
    assert res["triangle_count_before"] == res["triangle_count_after"]


def test_mesh_simplify_rejects_non_positive_target(mesh_mod) -> None:
    mod, _doc, _mp, _p = mesh_mod
    res = mod.mesh_simplify(doc_name="Doc", mesh_name="MyMesh", target_faces=0)
    assert res["success"] is False


def test_mesh_simplify_rejects_unknown_doc(mesh_mod) -> None:
    mod, _doc, _mp, _p = mesh_mod
    res = mod.mesh_simplify(doc_name="MissingDoc", mesh_name="MyMesh")
    assert res["success"] is False
    assert "not found" in res["reason"]


def test_mesh_simplify_rejects_non_mesh_object(mesh_mod) -> None:
    mod, doc, _mp, _p = mesh_mod
    doc.objects["Part"] = _FakeFeature(type_id="Part::Feature", name="Part")
    res = mod.mesh_simplify(doc_name="Doc", mesh_name="Part", target_faces=100)
    assert res["success"] is False
    assert "not a Mesh::Feature" in res["reason"]


def test_mesh_simplify_decimates_large_mesh(mesh_mod, monkeypatch) -> None:
    mod, doc, _mp, _p = mesh_mod
    feat = doc.objects["MyMesh"]
    feat.Mesh.CountFacets = 50_000

    captured = {}

    def _fake_decimate(self, reduction):
        captured["reduction"] = reduction
        new = _FakeMesh()
        new.CountFacets = 5_000
        new.CountPoints = 2_500
        return new

    monkeypatch.setattr(type(feat.Mesh), "decimate", _fake_decimate)
    res = mod.mesh_simplify(doc_name="Doc", mesh_name="MyMesh", target_faces=5_000)
    assert res["success"] is True
    assert res["triangle_count_before"] == 50_000
    assert res["triangle_count_after"] == 5_000
    assert captured["reduction"] >= 0.05


def test_mesh_to_solid_missing_doc(mesh_mod) -> None:
    mod, _doc, _mp, _p = mesh_mod
    res = mod.mesh_to_solid(doc_name="MissingDoc", mesh_name="MyMesh")
    assert res["success"] is False
    assert "not found" in res["reason"]


def test_mesh_to_solid_unknown_mesh(mesh_mod) -> None:
    mod, _doc, _mp, _p = mesh_mod
    res = mod.mesh_to_solid(doc_name="Doc", mesh_name="NoSuchMesh")
    assert res["success"] is False
    assert "not found" in res["reason"]


def test_mesh_to_solid_non_mesh_object(mesh_mod) -> None:
    mod, doc, _mp, _p = mesh_mod
    doc.objects["Part"] = _FakeFeature(type_id="Part::Feature", name="Part")
    res = mod.mesh_to_solid(doc_name="Doc", mesh_name="Part")
    assert res["success"] is False
    assert "not a Mesh::Feature" in res["reason"]


def _install_mesh_part_pipeline(mesh_mod, monkeypatch):
    """Install ``meshToShape`` + ``makeSolid`` stubs on the fake modules."""
    mod, doc, _mp, _p = mesh_mod
    shell = _FakeShape()
    sewed = _FakeShape()
    solid = _FakeSolid()
    solid.Volume = 9.9
    monkeypatch.setattr(_mp, "meshToShape", lambda mesh: shell, raising=False)
    monkeypatch.setattr(_p, "makeSolid", lambda shape: solid, raising=False)
    monkeypatch.setattr(type(shell), "fix", lambda self, *a, **k: shell, raising=False)
    monkeypatch.setattr(type(shell), "sewShape", lambda self: sewed, raising=False)
    return mod, doc, _mp, _p, shell, sewed, solid


def test_mesh_to_solid_happy_path(mesh_mod, monkeypatch) -> None:
    import os
    os.environ["MCP_MESH_DEBUG"] = "1"
    mod, doc, _mp, _p, shell, sewed, solid = _install_mesh_part_pipeline(mesh_mod, monkeypatch)
    res = mod.mesh_to_solid(doc_name="Doc", mesh_name="MyMesh")
    assert res["success"] is True
    assert res["solid"] is True
    assert res["volume"] == 9.9
    assert res["shell_faces"] == 3
    assert res["object_name"] == "MyMesh_Solid"


def test_mesh_to_solid_without_repair(mesh_mod, monkeypatch) -> None:
    mod, doc, _mp, _p, shell, sewed, solid = _install_mesh_part_pipeline(mesh_mod, monkeypatch)
    monkeypatch.setattr(_p, "makeSolid", lambda shape: None, raising=False)
    res = mod.mesh_to_solid(doc_name="Doc", mesh_name="MyMesh", repair=False)
    assert res["success"] is True
    assert res["repair_applied"] is False
    assert res["solid"] is False


def test_mesh_to_solid_custom_name(mesh_mod, monkeypatch) -> None:
    mod, doc, _mp, _p, shell, sewed, solid = _install_mesh_part_pipeline(mesh_mod, monkeypatch)
    monkeypatch.setattr(_p, "makeSolid", lambda shape: None, raising=False)
    res = mod.mesh_to_solid(doc_name="Doc", mesh_name="MyMesh", new_name="Custom")
    assert res["success"] is True
    assert res["object_name"] == "Custom"
    assert "Custom" in doc.objects


def test_mesh_to_solid_pre_simplifies_large_mesh(mesh_mod, monkeypatch) -> None:
    mod, doc, _mp, _p, shell, sewed, solid = _install_mesh_part_pipeline(mesh_mod, monkeypatch)
    feat = doc.objects["MyMesh"]
    feat.Mesh.CountFacets = 60_000

    def _decimate(self, reduction):  # noqa: ARG002
        new = _FakeMesh()
        new.CountFacets = 5_000
        return new

    monkeypatch.setattr(type(feat.Mesh), "decimate", _decimate, raising=False)

    res = mod.mesh_to_solid(
        doc_name="Doc",
        mesh_name="MyMesh",
        max_triangles_before_simplify=50_000,
        target_faces_after_simplify=5_000,
    )
    assert res["success"] is True
    assert res["decimated"] is True


def test_module_exports(mesh_mod) -> None:
    mod, _doc, _mp, _p = mesh_mod
    assert hasattr(mod, "mesh_import")
    assert hasattr(mod, "mesh_simplify")
    assert hasattr(mod, "mesh_to_solid")
    assert "mesh_import" in mod.__all__
    assert "mesh_simplify" in mod.__all__
    assert "mesh_to_solid" in mod.__all__