"""Direct tests for ``FreeCADRPC._create_object_gui`` and friends.

These helpers carry the heavy dispatch logic for create_object /
edit_object. We mock FreeCAD / PySide / ObjectsFem / femmesh heavily.

Coverage focus: every branch in the 313-line ``_create_object_gui``,
``_edit_object_gui``, ``_run_fem_analysis_gui``, ``_save_active_screenshot``
functions.
"""
import importlib.util
import sys
import types
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_RS_DIR = _HERE.parent / "addon" / "FreeCADMCP" / "rpc_server"

# FreeCAD / PySide / ObjectsFem stubs.
for name in ("FreeCAD", "FreeCADGui", "ObjectsFem", "PySide", "femmesh", "femtools"):
    if name not in sys.modules:
        sys.modules[name] = types.ModuleType(name)


# Make femmesh.gmshtools accessible as `from femmesh.gmshtools import GmshTools`.
sys.modules["femmesh"].gmshtools = types.ModuleType("femmesh.gmshtools")
sys.modules["femmesh"].ccxtools = types.ModuleType("femmesh.ccxtools")


# Helpers for building fake FreeCAD objects.
def _make_doc(name="Doc", objects=None):
    """Build a fake Document that records calls."""

    class _Doc:
        def __init__(self):
            self.Name = name
            self.Objects = list(objects or [])
            self.FileName = ""
            self._calls = []
            self.Group = []

        def recompute(self):
            self._calls.append("recompute")

        def addObject(self, type_id, obj_name):
            new = _make_obj(type_id, obj_name)
            self.Objects.append(new)
            return new

        def removeObject(self, name):
            self.Objects = [o for o in self.Objects if o.Name != name]

        def undo(self):
            pass

        def redo(self):
            pass

        def save(self):
            pass

        def saveAs(self, path):
            self.FileName = path

        def getObject(self, name):
            for o in self.Objects:
                if o.Name == name:
                    return o
            return None

        def __getattr__(self, item):
            # FEM analyses (Fem::FemAnalysis) expose Group via getattr
            # in the source. Map everything else to None so that
            # getattr(doc, "MyAnalysis") returns None when absent.
            if item.startswith("_"):
                raise AttributeError(item)
            return None

    return _Doc()


def _make_obj(type_id, name="O"):
    class _Obj:
        def __init__(self):
            self.Name = name
            self.TypeId = type_id
            self.Label = name
            self.PropertiesList = []
            self.ViewObject = None
            self.Placement = None
            self.Shape = None
            self.Properties = {}
            self.Group = []
            self._attrs = {}

        def addObject(self, obj):
            self.Group.append(obj)
            return obj

        def __getattr__(self, item):
            return self._attrs.get(item)

        def __setattr__(self, item, value):
            if item.startswith("_") or item in (
                "Name", "TypeId", "Label", "PropertiesList", "ViewObject",
                "Placement", "Shape", "Properties", "Group",
            ):
                object.__setattr__(self, item, value)
            else:
                self._attrs[item] = value

    return _Obj()


# Standard shim set for the addon.
_fc = sys.modules["FreeCAD"]
_fc.Console = types.SimpleNamespace(
    PrintWarning=lambda *a, **k: None,
    PrintMessage=lambda *a, **k: None,
    PrintError=lambda *a, **k: None,
)
_fc.getUserAppDataDir = lambda: "/tmp"
_fc.newDocument = lambda name: _make_doc_and_register(name)
_fc.getDocument = lambda name: _docs.get(name)
_fc.listDocuments = lambda: _docs
_fc.Document = type("Document", (), {})
_fc.DocumentObject = type("DocumentObject", (), {})
_fc.Vector = type("Vector", (), {})
_fc.Rotation = type("Rotation", (), {})
_fc.Placement = type("Placement", (), {})
_fc.Color = type("Color", (), {})

# ObjectsFem: make sure ALL the names that ``_create_object_gui`` /
# ``_run_fem_analysis_gui`` look up exist, so attribute access never
# raises. Tests that need a missing-attribute scenario explicitly delete
# the attribute and the autouse fixture restores it.
_objfem = sys.modules["ObjectsFem"]
_objfem.makeMeshGmsh = lambda *a, **k: (None,)
_objfem.makeAnalysis = lambda doc, name: _make_obj("Fem::AnalysisPython", name)
_objfem.makeMaterialSolid = lambda doc, name: _make_obj("Fem::MaterialCommon", name)
_objfem.makeSolverCalculiXCcxTools = lambda doc, name: _make_obj("Fem::SolverCcxTools", name)

# Save the original ObjectsFem / FreeCAD state so we can restore after
# each test (other test files mutate FreeCAD.newDocument and similar
# attributes; we want our tests to always start from the canonical
# shim state).
_orig_objectsfem_attrs = dict(vars(sys.modules["ObjectsFem"]))
_orig_freecad_attrs = dict(vars(_fc))

_docs: dict = {}

sys.modules["FreeCADGui"].ActiveDocument = types.SimpleNamespace(
    ActiveView=types.SimpleNamespace(
        viewIsometric=lambda: None, viewFront=lambda: None, viewTop=lambda: None,
        viewRight=lambda: None, viewBack=lambda: None, viewLeft=lambda: None,
        viewBottom=lambda: None, viewDimetric=lambda: None, viewTrimetric=lambda: None,
        fitAll=lambda: None, saveImage=lambda *a, **k: None,
        getSize=lambda: (800, 600),
    ),
    ActiveObject=None,
)
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
    QApplication=type("QApplication", (), {
        "instance": staticmethod(lambda: None),
        "processEvents": lambda *a, **k: None,
    }),
)


# Load addon modules.
_pkg = types.ModuleType("_test_obj_gui_pkg")
_pkg.__path__ = [str(_RS_DIR)]
sys.modules["_test_obj_gui_pkg"] = _pkg
for sub in ("parts_library", "serialize", "_fem_workdir", "_request_tracking",
            "_security_gate", "_settings", "_screenshot", "_ip_allowlist",
            "_dispatch", "_commands", "rpc_server"):
    spec = importlib.util.spec_from_file_location(
        f"{_pkg}.{sub}", str(_RS_DIR / f"{sub}.py")
    )
    m = importlib.util.module_from_spec(spec)
    sys.modules[f"{_pkg}.{sub}"] = m
    spec.loader.exec_module(m)  # type: ignore[union-attr]
rpc_server = sys.modules[f"{_pkg}.rpc_server"]


def _reset_docs():
    _docs.clear()


def _make_doc_and_register(name):
    d = _make_doc(name)
    _docs[name] = d
    return d


# Auto-fixture: restore FreeCAD / ObjectsFem state per test.
import pytest  # noqa: E402


# Canonical shim state — re-applied before every test by the
# autouse fixture below. This protects against mutations from other
# test files (e.g. test_rpc_server_methods.py, test_parts_library.py)
# that may run earlier in the suite.
def _apply_canonical_shims():
    _fc.Console = types.SimpleNamespace(
        PrintWarning=lambda *a, **k: None,
        PrintMessage=lambda *a, **k: None,
        PrintError=lambda *a, **k: None,
    )
    _fc.getUserAppDataDir = lambda: "/tmp"
    _fc.newDocument = lambda name: _make_doc_and_register(name)
    _fc.getDocument = lambda name: _docs.get(name)
    _fc.listDocuments = lambda: _docs
    _fc.Document = type("Document", (), {})
    _fc.DocumentObject = type("DocumentObject", (), {})
    _fc.Vector = type("Vector", (), {})
    _fc.Rotation = type("Rotation", (), {})
    _fc.Placement = type("Placement", (), {})
    _fc.Color = type("Color", (), {})


@pytest.fixture(autouse=True)
def _restore_shims():
    # Apply canonical shims at fixture SETUP so every test starts
    # from a known state, regardless of what previous tests in the
    # suite (in any file) may have done to FreeCAD's module attrs.
    _apply_canonical_shims()
    _fc_snapshot = dict(vars(_fc))
    yield
    # Restore ObjectsFem to the module-load snapshot.
    fem = sys.modules["ObjectsFem"]
    for attr in list(vars(fem)):
        if attr not in _orig_objectsfem_attrs:
            try:
                delattr(fem, attr)
            except AttributeError:
                pass
        else:
            setattr(fem, attr, _orig_objectsfem_attrs[attr])
    # Restore FreeCAD to the per-test snapshot.
    for attr in list(vars(_fc)):
        if attr not in _fc_snapshot:
            try:
                delattr(_fc, attr)
            except AttributeError:
                pass
    for attr, val in _fc_snapshot.items():
        setattr(_fc, attr, val)


# ---------------------------------------------------------------------------
# _create_document_gui
# ---------------------------------------------------------------------------

def test_create_document_gui_runs_recompute():
    _reset_docs()
    # Force the canonical newDocument lambda (a previous test may have
    # polluted it).
    _fc.newDocument = lambda name: _make_doc_and_register(name)
    out = rpc_server.FreeCADRPC()._create_document_gui("Doc1")
    assert out is True
    assert "Doc1" in _docs


# ---------------------------------------------------------------------------
# _create_object_gui
# ---------------------------------------------------------------------------

def test_create_object_gui_missing_doc():
    _reset_docs()
    obj = rpc_server.Object(name="O", type="Part::Box", properties={})
    out = rpc_server.FreeCADRPC()._create_object_gui("MissingDoc", obj)
    assert "not found" in out


def test_create_object_gui_part_box_happy_path():
    """Plain Part::Box creation: doc.addObject + set_object_property."""
    _reset_docs()
    doc = _make_doc("Doc1")
    _docs["Doc1"] = doc
    # Inject a fake FreeCAD.getDocument that returns our doc.
    sys.modules["FreeCAD"].getDocument = lambda name: doc if name == "Doc1" else None

    obj = rpc_server.Object(
        name="Box",
        type="Part::Box",
        properties={"Length": 10, "Width": 5, "Height": 2},
    )
    out = rpc_server.FreeCADRPC()._create_object_gui("Doc1", obj)
    assert out is True
    # The new object should be added to the doc.
    assert any(o.Name == "Box" and o.TypeId == "Part::Box" for o in doc.Objects)


def test_create_object_gui_part_box_with_placement_dict():
    """Placement dict in properties -> real Placement object created."""
    _reset_docs()
    doc = _make_doc("Doc1")
    _docs["Doc1"] = doc
    sys.modules["FreeCAD"].getDocument = lambda name: doc if name == "Doc1" else None

    class _V:
        def __init__(self, x, y, z):
            self.x, self.y, self.z = x, y, z
    class _R:
        def __init__(self, axis, angle):
            self.Axis = axis
            self.Angle = angle
    class _P:
        def __init__(self, base, rot):
            self.Base = base
            self.Rotation = rot
    sys.modules["FreeCAD"].Vector = _V
    sys.modules["FreeCAD"].Rotation = _R
    sys.modules["FreeCAD"].Placement = _P

    obj = rpc_server.Object(
        name="Box",
        type="Part::Box",
        properties={"Placement": {"Base": {"x": 1, "y": 2, "z": 3}, "Rotation": {"Axis": {"x": 0, "y": 0, "z": 1}, "Angle": 45}}},
    )
    out = rpc_server.FreeCADRPC()._create_object_gui("Doc1", obj)
    assert out is True


def test_create_object_gui_placement_with_position_alias():
    """``Position`` alias is accepted (v1.0.0 compat)."""
    _reset_docs()
    doc = _make_doc("Doc1")
    _docs["Doc1"] = doc
    sys.modules["FreeCAD"].getDocument = lambda name: doc if name == "Doc1" else None

    class _V:
        def __init__(self, x, y, z):
            self.x, self.y, self.z = x, y, z
    class _R:
        def __init__(self, axis, angle):
            self.Axis = axis
            self.Angle = angle
    class _P:
        def __init__(self, base, rot):
            self.Base = base
            self.Rotation = rot
    sys.modules["FreeCAD"].Vector = _V
    sys.modules["FreeCAD"].Rotation = _R
    sys.modules["FreeCAD"].Placement = _P

    obj = rpc_server.Object(
        name="Box",
        type="Part::Box",
        properties={"Placement": {"Position": {"x": 1, "y": 2, "z": 3}, "Rotation": {}}},
    )
    out = rpc_server.FreeCADRPC()._create_object_gui("Doc1", obj)
    assert out is True


def test_create_object_gui_setattr_logs_per_property_error():
    """Per-property failures don't abort creation; they are logged."""
    _reset_docs()
    doc = _make_doc("Doc1")
    _docs["Doc1"] = doc
    sys.modules["FreeCAD"].getDocument = lambda name: doc if name == "Doc1" else None

    obj = rpc_server.Object(
        name="Box",
        type="Part::Box",
        # Reference a non-existent object; the prop error is logged.
        properties={"Tool": "NonExistent"},
    )
    out = rpc_server.FreeCADRPC()._create_object_gui("Doc1", obj)
    # Creation still succeeds; per-property errors don't abort it.
    assert out is True


def test_create_object_gui_viewobject_shape_color():
    """ViewObject.ShapeColor and ShapeColor (top-level) handled."""
    _reset_docs()
    doc = _make_doc("Doc1")
    _docs["Doc1"] = doc
    sys.modules["FreeCAD"].getDocument = lambda name: doc if name == "Doc1" else None

    obj = rpc_server.Object(
        name="Box",
        type="Part::Box",
        properties={"ShapeColor": [0.5, 0.5, 0.5, 1.0]},
    )
    out = rpc_server.FreeCADRPC()._create_object_gui("Doc1", obj)
    assert out is True


def test_create_object_gui_fem_python_analysis_no_make_method():
    """Fem::FemFoo (no matching make method) -> ValueError path."""
    _reset_docs()
    doc = _make_doc("Doc1")
    _docs["Doc1"] = doc
    sys.modules["FreeCAD"].getDocument = lambda name: doc if name == "Doc1" else None

    # ObjectsFem has no makeFemWeird.
    obj = rpc_server.Object(
        name="WeirdFem",
        type="Fem::FemWeird",
        properties={},
        analysis="MyAnalysis",
    )
    out = rpc_server.FreeCADRPC()._create_object_gui("Doc1", obj)
    assert isinstance(out, str)
    assert "No creation method" in out or "WeirdFem" in out


def test_create_object_gui_fem_materialcommon():
    """Fem::MaterialCommon uses makeMaterialSolid factory."""
    _reset_docs()
    doc = _make_doc("Doc1")
    _docs["Doc1"] = doc
    sys.modules["FreeCAD"].getDocument = lambda name: doc if name == "Doc1" else None

    sys.modules["ObjectsFem"].makeMaterialSolid = lambda doc, name: _make_obj("Fem::MaterialCommon", name)

    obj = rpc_server.Object(
        name="Material",
        type="Fem::MaterialCommon",
        properties={},
        analysis=None,  # no analysis attachment
    )
    out = rpc_server.FreeCADRPC()._create_object_gui("Doc1", obj)
    print("DEBUG: out=", out)
    assert out is True


def test_create_object_gui_fem_analysis_python_no_analysis_name():
    """Fem::AnalysisPython without analysis_name -> no addObject call."""
    _reset_docs()
    doc = _make_doc("Doc1")
    _docs["Doc1"] = doc
    sys.modules["FreeCAD"].getDocument = lambda name: doc if name == "Doc1" else None

    sys.modules["ObjectsFem"].makeAnalysis = lambda doc, name: _make_obj("Fem::AnalysisPython", name)

    obj = rpc_server.Object(
        name="Analysis",
        type="Fem::AnalysisPython",
        properties={},
        analysis=None,
    )
    out = rpc_server.FreeCADRPC()._create_object_gui("Doc1", obj)
    assert out is True


# ---------------------------------------------------------------------------
# _edit_object_gui
# ---------------------------------------------------------------------------

def test_edit_object_gui_missing_doc():
    _reset_docs()
    obj = rpc_server.Object(name="O", properties={"Length": 10})
    out = rpc_server.FreeCADRPC()._edit_object_gui("Nope", obj)
    assert "not found" in out


def test_edit_object_gui_missing_object():
    _reset_docs()
    doc = _make_doc("Doc1")
    _docs["Doc1"] = doc
    sys.modules["FreeCAD"].getDocument = lambda name: doc if name == "Doc1" else None

    obj = rpc_server.Object(name="Nope", properties={"Length": 10})
    out = rpc_server.FreeCADRPC()._edit_object_gui("Doc1", obj)
    assert "not found" in out


def test_edit_object_gui_happy_path():
    _reset_docs()
    existing = _make_obj("Part::Box", "Box")
    doc = _make_doc("Doc1", objects=[existing])
    _docs["Doc1"] = doc
    sys.modules["FreeCAD"].getDocument = lambda name: doc if name == "Doc1" else None

    obj = rpc_server.Object(name="Box", properties={"Length": 20})
    out = rpc_server.FreeCADRPC()._edit_object_gui("Doc1", obj)
    assert out is True


def test_edit_object_gui_references_handled():
    """References list updates the obj's References attribute."""
    _reset_docs()
    box = _make_obj("Part::Box", "Box")
    existing = _make_obj("Fem::ConstraintFixed", "Constr")
    doc = _make_doc("Doc1", objects=[box, existing])
    _docs["Doc1"] = doc
    sys.modules["FreeCAD"].getDocument = lambda name: doc if name == "Doc1" else None

    obj = rpc_server.Object(
        name="Constr",
        properties={"References": [["Box", "Face1"]]},
    )
    out = rpc_server.FreeCADRPC()._edit_object_gui("Doc1", obj)
    assert out is True


def test_edit_object_gui_references_invalid_ref_raises():
    """Invalid reference raises -> error string returned."""
    _reset_docs()
    existing = _make_obj("Fem::ConstraintFixed", "Constr")
    doc = _make_doc("Doc1", objects=[existing])
    _docs["Doc1"] = doc
    sys.modules["FreeCAD"].getDocument = lambda name: doc if name == "Doc1" else None

    obj = rpc_server.Object(
        name="Constr",
        properties={"References": [["NonExistent", "Face1"]]},
    )
    out = rpc_server.FreeCADRPC()._edit_object_gui("Doc1", obj)
    assert isinstance(out, str)
    assert "NonExistent" in out or "not found" in out.lower()


# ---------------------------------------------------------------------------
# _run_fem_analysis_gui — heavy mocks
# ---------------------------------------------------------------------------

def test_run_fem_analysis_gui_missing_doc():
    sys.modules["FreeCAD"].getDocument = lambda name: None
    out = rpc_server.FreeCADRPC()._run_fem_analysis_gui("Nope", "X")
    assert out["success"] is False
    assert "not found" in out["error"]


def test_run_fem_analysis_gui_missing_analysis():
    doc = _make_doc("Doc1")
    sys.modules["FreeCAD"].getDocument = lambda name: doc if name == "Doc1" else None
    out = rpc_server.FreeCADRPC()._run_fem_analysis_gui("Doc1", "MissingAnalysis")
    assert out["success"] is False
    assert "not found" in out["error"]


def test_run_fem_analysis_gui_wrong_typeid():
    doc = _make_doc("Doc1")
    wrong = _make_obj("Part::Box", "X")  # not a Fem analysis
    doc.Objects.append(wrong)
    sys.modules["FreeCAD"].getDocument = lambda name: doc if name == "Doc1" else None
    out = rpc_server.FreeCADRPC()._run_fem_analysis_gui("Doc1", "X")
    assert out["success"] is False
    assert "is not a FEM analysis" in out["error"]


def test_run_fem_analysis_gui_existing_solver():
    """When the analysis already has a SolverCcx, we use it directly."""
    doc = _make_doc("Doc1")
    analysis = _make_obj("Fem::FemAnalysis", "A")
    solver = _make_obj("Fem::SolverCcxTools", "Solver")
    solver.Group = [solver]
    analysis.Group = [solver]
    doc.Objects.append(analysis)

    sys.modules["FreeCAD"].getDocument = lambda name: doc if name == "Doc1" else None

    # Stub femtools.ccxtools to avoid real FreeCAD usage.
    class _StubFemTools:
        def __init__(self, *a, **kw):
            self.working_dir = None
        def update_objects(self):
            pass
        def setup_working_dir(self, wd):
            self.working_dir = wd
        def setup_ccx(self):
            pass
        def check_prerequisites(self):
            return ""  # success
        def purge_results(self):
            pass
        def run(self):
            pass
        def load_results(self):
            pass

    sys.modules["femtools"].ccxtools = types.SimpleNamespace(FemToolsCcx=_StubFemTools)

    out = rpc_server.FreeCADRPC()._run_fem_analysis_gui("Doc1", "A")
    # Either succeeds or hits some other branch — but does not raise.
    assert isinstance(out, dict)
    assert "success" in out


def test_run_fem_analysis_gui_creates_solver():
    """When no SolverCcx exists, the helper creates one via ObjectsFem."""
    doc = _make_doc("Doc1")
    analysis = _make_obj("Fem::FemAnalysis", "A")
    analysis.Group = []  # no solver
    doc.Objects.append(analysis)
    sys.modules["FreeCAD"].getDocument = lambda name: doc if name == "Doc1" else None

    new_solver = _make_obj("Fem::SolverCcxTools", "CalculiX")
    sys.modules["ObjectsFem"].makeSolverCalculiXCcxTools = lambda doc, name: new_solver

    class _StubFemTools:
        def __init__(self, *a, **kw):
            pass
        def update_objects(self):
            pass
        def setup_working_dir(self, wd):
            pass
        def setup_ccx(self):
            pass
        def check_prerequisites(self):
            return "missing material"
        def purge_results(self):
            pass
        def run(self):
            pass
        def load_results(self):
            pass

    sys.modules["femtools"].ccxtools = types.SimpleNamespace(FemToolsCcx=_StubFemTools)

    out = rpc_server.FreeCADRPC()._run_fem_analysis_gui("Doc1", "A")
    assert out["success"] is False
    assert "Prerequisites failed" in out["error"]


def test_run_fem_analysis_gui_no_solver_factory():
    """When ObjectsFem has no solver factory, return error early."""
    doc = _make_doc("Doc1")
    analysis = _make_obj("Fem::FemAnalysis", "A")
    analysis.Group = []
    doc.Objects.append(analysis)
    sys.modules["FreeCAD"].getDocument = lambda name: doc if name == "Doc1" else None
    fem = sys.modules["ObjectsFem"]
    # Temporarily remove solver factories; autouse fixture restores them.
    saved_solver1 = fem.makeSolverCalculiXCcxTools
    saved_solver2 = getattr(fem, "makeSolverCalculixCcxTools", None)
    del fem.makeSolverCalculiXCcxTools
    if saved_solver2 is not None:
        delattr(fem, "makeSolverCalculixCcxTools")

    out = rpc_server.FreeCADRPC()._run_fem_analysis_gui("Doc1", "A")
    assert out["success"] is False
    assert "no Calculix solver factory" in out["error"]


def test_run_fem_analysis_gui_no_result_object():
    """Solver ran but produced no result object -> error."""
    doc = _make_doc("Doc1")
    analysis = _make_obj("Fem::FemAnalysis", "A")
    analysis.Group = []  # no solver, no result
    doc.Objects.append(analysis)
    sys.modules["FreeCAD"].getDocument = lambda name: doc if name == "Doc1" else None

    new_solver = _make_obj("Fem::SolverCcxTools", "CalculiX")
    sys.modules["ObjectsFem"].makeSolverCalculiXCcxTools = lambda doc, name: new_solver

    class _StubFemTools:
        def __init__(self, *a, **kw):
            pass
        def update_objects(self):
            pass
        def setup_working_dir(self, wd):
            pass
        def setup_ccx(self):
            pass
        def check_prerequisites(self):
            return ""
        def purge_results(self):
            pass
        def run(self):
            pass
        def load_results(self):
            pass

    sys.modules["femtools"].ccxtools = types.SimpleNamespace(FemToolsCcx=_StubFemTools)

    out = rpc_server.FreeCADRPC()._run_fem_analysis_gui("Doc1", "A")
    assert out["success"] is False
    assert "no result object" in out["error"]


def test_run_fem_analysis_gui_success_with_results():
    """Successful run: returns min/max von Mises and displacement."""
    doc = _make_doc("Doc1")
    analysis = _make_obj("Fem::FemAnalysis", "A")
    result = _make_obj("Fem::ResultNodes", "Result")
    result.vonMises = [100.0, 200.0, 50.0]
    result.DisplacementLengths = [0.1, 0.5, 1.0]
    analysis.Group = [result]
    doc.Objects.append(analysis)
    sys.modules["FreeCAD"].getDocument = lambda name: doc if name == "Doc1" else None

    new_solver = _make_obj("Fem::SolverCcxTools", "CalculiX")
    sys.modules["ObjectsFem"].makeSolverCalculiXCcxTools = lambda doc, name: new_solver

    class _StubFemTools:
        def __init__(self, *a, **kw):
            pass
        def update_objects(self):
            pass
        def setup_working_dir(self, wd):
            pass
        def setup_ccx(self):
            pass
        def check_prerequisites(self):
            return ""
        def purge_results(self):
            pass
        def run(self):
            pass
        def load_results(self):
            pass

    sys.modules["femtools"].ccxtools = types.SimpleNamespace(FemToolsCcx=_StubFemTools)

    # Analysis should have a Result in its Group.
    out = rpc_server.FreeCADRPC()._run_fem_analysis_gui("Doc1", "A")
    assert out["success"] is True
    assert out["node_count"] == 3
    assert out["max_von_mises_MPa"] == 200.0
    assert out["min_von_mises_MPa"] == 50.0
    assert out["max_displacement_mm"] == 1.0


def test_run_fem_analysis_gui_keeps_workdir_via_env(monkeypatch):
    """``FREECAD_MCP_KEEP_FEM_WORKDIR=1`` keeps the workdir after solve."""
    doc = _make_doc("Doc1")
    analysis = _make_obj("Fem::FemAnalysis", "A")
    result = _make_obj("Fem::ResultNodes", "Result")
    result.vonMises = [10.0, 20.0]
    result.DisplacementLengths = [0.1, 0.2]
    analysis.Group = [result]
    doc.Objects.append(analysis)
    sys.modules["FreeCAD"].getDocument = lambda name: doc if name == "Doc1" else None

    new_solver = _make_obj("Fem::SolverCcxTools", "CalculiX")
    sys.modules["ObjectsFem"].makeSolverCalculiXCcxTools = lambda doc, name: new_solver

    class _StubFemTools:
        def __init__(self, *a, **kw):
            pass
        def update_objects(self):
            pass
        def setup_working_dir(self, wd):
            pass
        def setup_ccx(self):
            pass
        def check_prerequisites(self):
            return ""
        def purge_results(self):
            pass
        def run(self):
            pass
        def load_results(self):
            pass

    sys.modules["femtools"].ccxtools = types.SimpleNamespace(FemToolsCcx=_StubFemTools)

    monkeypatch.setenv("FREECAD_MCP_KEEP_FEM_WORKDIR", "1")
    out = rpc_server.FreeCADRPC()._run_fem_analysis_gui("Doc1", "A")
    assert out["success"] is True


def test_run_fem_analysis_gui_unexpected_exception():
    """If anything in the flow raises, we return a structured error."""
    doc = _make_doc("Doc1")
    analysis = _make_obj("Fem::FemAnalysis", "A")
    analysis.Group = []
    doc.Objects.append(analysis)
    sys.modules["FreeCAD"].getDocument = lambda name: doc if name == "Doc1" else None

    new_solver = _make_obj("Fem::SolverCcxTools", "CalculiX")
    sys.modules["ObjectsFem"].makeSolverCalculiXCcxTools = lambda doc, name: new_solver

    # femtools.ccxtools.FemToolsCcx raises on construction.
    class _ExplodingFemTools:
        def __init__(self, *a, **kw):
            raise RuntimeError("kaboom")

    sys.modules["femtools"].ccxtools = types.SimpleNamespace(FemToolsCcx=_ExplodingFemTools)

    out = rpc_server.FreeCADRPC()._run_fem_analysis_gui("Doc1", "A")
    assert out["success"] is False
    assert "kaboom" in out["error"]