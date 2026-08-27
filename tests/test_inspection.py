"""Tests for the inspection module (v1.1.2).

These tests use stub FreeCAD + Part modules so they run without
a real FreeCAD install.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

_RPC_DIR = Path(__file__).resolve().parent.parent / "addon/FreeCADMCP/rpc_server"


class _FakeBoundBox:
    def __init__(self, x0=0, x1=1, y0=0, y1=1, z0=0, z1=1):
        self.XMin = x0
        self.XMax = x1
        self.YMin = y0
        self.YMax = y1
        self.ZMin = z0
        self.ZMax = z1


class _FakeNormal:
    def __init__(self, x=0, y=0, z=1):
        self.x = x
        self.y = y
        self.z = z


class _FakeSurface:
    """Surface stub whose ``type(...).__name__`` is ``self.name``."""
    def __init__(self, name):
        self._name = name


def _surface_type_name(surface):
    """Helper used by tests: extract the surface name regardless of how
    inspection.py reads it. The current inspection code uses
    ``type(face.Surface).__name__`` so we subclass with a custom name."""
    return surface._name


class _NamedSurfaceMeta(type):
    def __init__(cls, name, bases, ns):
        super().__init__(name, bases, ns)


def _make_surface_class(name):
    return _NamedSurfaceMeta(name, (), {})


class _FakeFace:
    def __init__(self, surface_name="Plane", area=1.0, cx=0, cy=0, cz=0, nx=0, ny=0, nz=1):
        # Use a class with the desired __name__ so type(...).__name__ works.
        self.Surface = _make_surface_class(surface_name)()
        self.Area = area
        self.CenterOfMass = types.SimpleNamespace(x=cx, y=cy, z=cz)
        self._nx, self._ny, self._nz = nx, ny, nz

    def normalAt(self, u, v):
        return _FakeNormal(self._nx, self._ny, self._nz)


class _FakeMatrixOfInertia:
    def __init__(self, elements=None):
        # The 3x3 inertia matrix flattened row-major.
        if elements is None:
            elements = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
        self.A = elements  # xx, xy, xz, yx, yy, yz, zx, zy, zz
        self.B = elements  # alias for code that reads .B
        self.C = elements  # alias for code that reads .C


class _FakeShape:
    def __init__(self, faces=None):
        self.Faces = faces or []
        self.Edges = []
        self.Vertexes = []
        self.Volume = 10.0
        self.Area = 20.0
        self.Length = 5.0
        self.BoundBox = _FakeBoundBox(0, 2, 0, 2, 0, 2)
        self.CenterOfMass = types.SimpleNamespace(x=1.0, y=1.0, z=1.0)
        self.MatrixOfInertia = _FakeMatrixOfInertia()

    def isNull(self):
        return False

    def isValid(self):
        return True

    def common(self, other):
        return _FakeShape()


class _FakeConstraint:
    def __init__(self, conflict=False, redundant=False):
        self.inConflict = conflict
        self.isRedundant = redundant


class _FakeSketch:
    TypeId = "Sketcher::SketchObject"

    def __init__(self, dof=0, conflicts=0, redundancies=0, name="Sketch"):
        self.Name = name
        self.DegreeOfFreedom = dof
        self.Constraints = [
            _FakeConstraint(conflict=(i < conflicts), redundant=(i >= conflicts and i < conflicts + redundancies))
            for i in range(conflicts + redundancies)
        ]
        self.Geometry = [object() for _ in range(3)]


class _FakeFeature:
    def __init__(self, name, type_id, shape=None, sketch=None, with_shape=True):
        self.Name = name
        self.TypeId = type_id
        if with_shape:
            self.Shape = shape
        if sketch is not None:
            self._sketch_attr = sketch


class _FakeDoc:
    def __init__(self, objects):
        self._objects = objects

    def getObject(self, name):
        return self._objects.get(name)

    def recompute(self):
        pass


class _FakeFreeCAD:
    ActiveDocument = None

    @staticmethod
    def getDocument(name):
        return _FAKE_DOCS.get(name)


_FAKE_DOCS: dict[str, _FakeDoc] = {}


@pytest.fixture
def ins_mod(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "_inspection_for_test", _RPC_DIR / "inspection.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_inspection_for_test"] = mod
    # Inject the fake FreeCAD before exec_module (the module imports it).
    # Use monkeypatch so the real ``FreeCAD`` entry in sys.modules is
    # restored after the test — otherwise subsequent tests that import
    # the real rpc_server see a broken FreeCAD stub and explode.
    fake_pkg = types.ModuleType("FreeCAD")
    fake_pkg.ActiveDocument = None
    monkeypatch.setitem(sys.modules, "FreeCAD", fake_pkg)
    # Patch the module-level reference after import.
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    monkeypatch.setattr(mod, "FreeCAD", _FakeFreeCAD)
    _FAKE_DOCS.clear()
    return mod


def test_list_faces_basic(ins_mod) -> None:
    shape = _FakeShape(faces=[
        _FakeFace("Plane", area=2.0),
        _FakeFace("Cylinder", area=3.0, nx=1, ny=0, nz=0),
        _FakeFace("Plane", area=4.0),
    ])
    feat = _FakeFeature("B", "Part::Feature", shape=shape)
    _FAKE_DOCS["Doc"] = _FakeDoc({"B": feat})
    res = ins_mod.list_faces("Doc", "B")
    assert res["success"] is True
    assert res["face_count"] == 3
    assert res["faces"][0]["type"] == "Plane"


def test_list_faces_type_filter(ins_mod) -> None:
    shape = _FakeShape(faces=[
        _FakeFace("Plane"),
        _FakeFace("Cylinder"),
        _FakeFace("Plane"),
    ])
    feat = _FakeFeature("B", "Part::Feature", shape=shape)
    _FAKE_DOCS["Doc"] = _FakeDoc({"B": feat})
    res = ins_mod.list_faces("Doc", "B", type_filter="Cyl")
    assert res["face_count"] == 1
    assert res["faces"][0]["type"] == "Cylinder"


def test_list_faces_limit(ins_mod) -> None:
    shape = _FakeShape(faces=[_FakeFace() for _ in range(50)])
    feat = _FakeFeature("B", "Part::Feature", shape=shape)
    _FAKE_DOCS["Doc"] = _FakeDoc({"B": feat})
    res = ins_mod.list_faces("Doc", "B", limit=10)
    assert res["face_count"] == 10


def test_measure_all(ins_mod) -> None:
    shape = _FakeShape()
    feat = _FakeFeature("B", "Part::Feature", shape=shape)
    _FAKE_DOCS["Doc"] = _FakeDoc({"B": feat})
    res = ins_mod.measure("Doc", "B")
    assert res["success"] is True
    assert res["volume"] == 10.0
    assert res["area"] == 20.0
    assert res["bbox"] is not None
    assert res["center_of_mass"] == [1.0, 1.0, 1.0]


def test_measure_subset(ins_mod) -> None:
    shape = _FakeShape()
    feat = _FakeFeature("B", "Part::Feature", shape=shape)
    _FAKE_DOCS["Doc"] = _FakeDoc({"B": feat})
    res = ins_mod.measure("Doc", "B", properties=["volume"])
    assert "volume" in res
    assert "area" not in res


def test_measure_distance_bbox_fallback(ins_mod) -> None:
    shape_a = _FakeShape()
    shape_b = _FakeShape()
    _FAKE_DOCS["Doc"] = _FakeDoc({
        "A": _FakeFeature("A", "Part::Feature", shape=shape_a),
        "B": _FakeFeature("B", "Part::Feature", shape=shape_b),
    })
    res = ins_mod.measure_distance("Doc", "A", "B")
    # BRepExtrema is unavailable in the stub; fallback is bbox-based.
    assert res["success"] is True
    assert res.get("approximate") is True
    assert res["distance"] == 0.0


def test_geometric_verification_right_handed(ins_mod) -> None:
    shape = _FakeShape(faces=[_FakeFace(nx=1, ny=0, nz=0), _FakeFace(nx=0, ny=1, nz=0)])
    feat = _FakeFeature("B", "Part::Feature", shape=shape)
    _FAKE_DOCS["Doc"] = _FakeDoc({"B": feat})
    res = ins_mod.geometric_verification("Doc", "B")
    assert res["is_valid"] is True
    assert res["is_null"] is False
    assert res["det_approx_one"] is True
    assert res["handedness_sign"] == 1.0
    assert res["normal_consistency"] is not None


def test_geometric_verification_mirrored(ins_mod) -> None:
    shape = _FakeShape()
    # Flip the determinant sign.
    shape.MatrixOfInertia = _FakeMatrixOfInertia(elements=(-1.0, 0, 0, 0, 1, 0, 0, 0, 1))
    feat = _FakeFeature("B", "Part::Feature", shape=shape)
    _FAKE_DOCS["Doc"] = _FakeDoc({"B": feat})
    res = ins_mod.geometric_verification("Doc", "B")
    assert res["det_approx_one"] is False
    assert res["handedness_sign"] < 0


def test_analyze_shape(ins_mod) -> None:
    shape = _FakeShape(faces=[
        _FakeFace("Plane"), _FakeFace("Plane"), _FakeFace("Cylinder"),
        _FakeFace("Sphere"), _FakeFace("Sphere"),
    ])
    feat = _FakeFeature("B", "Part::Feature", shape=shape)
    _FAKE_DOCS["Doc"] = _FakeDoc({"B": feat})
    res = ins_mod.analyze_shape("Doc", "B")
    assert res["success"] is True
    assert res["surface_types"]["Plane"] == 2
    assert res["surface_types"]["Cylinder"] == 1
    assert res["surface_types"]["Sphere"] == 2


def test_spatial_query_interference(ins_mod) -> None:
    shape = _FakeShape()
    _FAKE_DOCS["Doc"] = _FakeDoc({
        "A": _FakeFeature("A", "Part::Feature", shape=shape),
        "B": _FakeFeature("B", "Part::Feature", shape=shape),
    })
    res = ins_mod.spatial_query("Doc", "A", "B", mode="interference")
    assert res["success"] is True
    assert res["intersects"] is True


def test_spatial_query_unknown_mode(ins_mod) -> None:
    shape = _FakeShape()
    _FAKE_DOCS["Doc"] = _FakeDoc({
        "A": _FakeFeature("A", "Part::Feature", shape=shape),
        "B": _FakeFeature("B", "Part::Feature", shape=shape),
    })
    res = ins_mod.spatial_query("Doc", "A", "B", mode="nonsense")
    assert res["success"] is False


def test_spatial_query_clearance(ins_mod) -> None:
    shape = _FakeShape()
    _FAKE_DOCS["Doc"] = _FakeDoc({
        "A": _FakeFeature("A", "Part::Feature", shape=shape),
        "B": _FakeFeature("B", "Part::Feature", shape=shape),
    })
    res = ins_mod.spatial_query("Doc", "A", "B", mode="clearance", clearance_tol=1.0)
    assert res["success"] is True
    assert "below_tolerance" in res


def test_spatial_query_containment(ins_mod) -> None:
    shape_small = _FakeShape()
    shape_small.BoundBox = _FakeBoundBox(0.1, 0.2, 0.1, 0.2, 0.1, 0.2)
    shape_big = _FakeShape()
    shape_big.BoundBox = _FakeBoundBox(0, 1, 0, 1, 0, 1)
    _FAKE_DOCS["Doc"] = _FakeDoc({
        "small": _FakeFeature("small", "Part::Feature", shape=shape_small),
        "big": _FakeFeature("big", "Part::Feature", shape=shape_big),
    })
    res = ins_mod.spatial_query("Doc", "small", "big", mode="containment")
    assert res["contained"] is True


def test_recompute_diff(ins_mod) -> None:
    shape = _FakeShape()
    feat = _FakeFeature("B", "Part::Feature", shape=shape)
    _FAKE_DOCS["Doc"] = _FakeDoc({"B": feat})
    res = ins_mod.recompute_diff("Doc", "B", expected_volume=10.0)
    assert res["success"] is True
    assert res["expected_volume"] == 10.0
    assert res["volume_delta"] == 0.0


def test_sketch_diagnostics_fully_constrained(ins_mod) -> None:
    sketch = _FakeSketch(dof=0)
    sketch.TypeId = "Sketcher::SketchObjectPython"
    sketch.Name = "S"
    _FAKE_DOCS["Doc"] = _FakeDoc({"S": sketch})
    res = ins_mod.sketch_diagnostics("Doc", "S")
    assert res["success"] is True
    assert res["fully_constrained"] is True
    assert res["dof"] == 0


def test_sketch_diagnostics_with_conflicts(ins_mod) -> None:
    sketch = _FakeSketch(dof=2, conflicts=1, redundancies=1)
    sketch.TypeId = "Sketcher::SketchObjectPython"
    sketch.Name = "S"
    _FAKE_DOCS["Doc"] = _FakeDoc({"S": sketch})
    res = ins_mod.sketch_diagnostics("Doc", "S")
    assert res["conflicts"] == 1
    assert res["redundancies"] == 1
    assert res["fully_constrained"] is False


def test_sketch_diagnostics_wrong_type(ins_mod) -> None:
    feat = _FakeFeature("X", "Part::Feature")
    _FAKE_DOCS["Doc"] = _FakeDoc({"X": feat})
    res = ins_mod.sketch_diagnostics("Doc", "X")
    assert res["success"] is False


def test_get_shape_missing_doc(ins_mod) -> None:
    res = ins_mod.list_faces("missing", "X")
    assert res["success"] is False


def test_get_shape_missing_object(ins_mod) -> None:
    _FAKE_DOCS["Doc"] = _FakeDoc({})
    res = ins_mod.measure("Doc", "X")
    assert res["success"] is False


def test_no_shape_attribute(ins_mod) -> None:
    feat = _FakeFeature("Plain", "App::DocumentObject", with_shape=False)
    _FAKE_DOCS["Doc"] = _FakeDoc({"Plain": feat})
    res = ins_mod.measure("Doc", "Plain")
    assert res["success"] is False
    assert "no Shape" in res["reason"]
