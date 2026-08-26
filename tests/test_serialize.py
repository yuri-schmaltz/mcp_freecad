import importlib.util
from pathlib import Path

import pytest


def load_serialize_module():
    p = Path(__file__).resolve().parents[1] / "addon" / "FreeCADMCP" / "rpc_server" / "serialize.py"
    spec = importlib.util.spec_from_file_location("serialize_mod", str(p))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod():
    """Yield a freshly loaded serialize module and reset the App patches
    after each test so leaks do not affect other tests."""
    m = load_serialize_module()
    yield m
    # Clean up: clear App so the next fixture starts fresh.
    for attr in ("Vector", "Rotation", "Placement", "Color"):
        if hasattr(m.App, attr):
            try:
                delattr(m.App, attr)
            except AttributeError:
                pass


def test_serialize_vector_rotation_placement_and_object(mod):
    # Define fake FreeCAD-like classes and register them on the module's App
    class V:
        def __init__(self, x, y, z):
            self.x = x
            self.y = y
            self.z = z

    class Axis:
        def __init__(self, x, y, z):
            self.x = x
            self.y = y
            self.z = z

    class R:
        def __init__(self, axis, angle):
            self.Axis = axis
            self.Angle = angle

    class P:
        def __init__(self, base, rotation):
            self.Base = base
            self.Rotation = rotation

    # Patch the module's App so serialize recognizes the types
    mod.App.Vector = V
    mod.App.Rotation = R
    mod.App.Placement = P

    v = V(1, 2, 3)
    assert mod.serialize_value(v) == {"x": 1, "y": 2, "z": 3}

    axis = Axis(0, 0, 1)
    r = R(axis, 45)
    rv = mod.serialize_value(r)
    assert rv["Angle"] == 45
    assert rv["Axis"]["z"] == 1

    p = P(v, r)
    pv = mod.serialize_value(p)
    assert pv["Base"] == {"x": 1, "y": 2, "z": 3}
    assert pv["Rotation"]["Angle"] == 45

    # Fake object
    class FakeObj:
        Name = "Box1"
        Label = "B1"
        TypeId = "Part::Box"
        PropertiesList = ["Height"]
        Height = 10
        Placement = None
        Shape = None
        ViewObject = None

    fo = FakeObj()
    so = mod.serialize_object(fo)
    assert so["Name"] == "Box1"
    assert so["Properties"]["Height"] == 10


# ---------------------------------------------------------------------------
# v1.0.3 — coverage push
# ---------------------------------------------------------------------------

def test_serialize_value_primitives(mod):
    """int/float/str/bool pass through verbatim."""
    assert mod.serialize_value(42) == 42
    assert mod.serialize_value(3.14) == 3.14
    assert mod.serialize_value("hello") == "hello"
    assert mod.serialize_value(True) is True
    assert mod.serialize_value(False) is False


def test_serialize_value_none(mod):
    """None falls through to str(value) -> 'None'."""
    assert mod.serialize_value(None) == "None"


def test_serialize_value_list(mod):
    """Lists are mapped element-wise."""
    assert mod.serialize_value([1, 2, "three"]) == [1, 2, "three"]


def test_serialize_value_tuple(mod):
    """Tuples are also mapped element-wise."""
    assert mod.serialize_value((1, 2)) == [1, 2]


def test_serialize_value_nested(mod):
    """Nested lists are recursively serialized."""
    assert mod.serialize_value([[1, 2], [3, 4]]) == [[1, 2], [3, 4]]


def test_serialize_value_unknown_object(mod):
    """Unknown objects fall back to str()."""
    class Weird:
        def __str__(self):
            return "weird"
    assert mod.serialize_value(Weird()) == "weird"


def test_serialize_value_color_branch_unreachable_for_tuples(mod):
    """Pin the documented behaviour: tuples hit the list branch first,
    so even when ``App.Color`` is registered, a tuple ``ShapeColor``
    serialises as a list. The Color branch only fires for
    non-list/non-tuple classes that match ``_COLOR_TYPE`` via
    ``isinstance``. With free-form custom classes, the user must
    subclass ``App.Color`` (in real FreeCAD) to hit that branch.
    """
    # When _COLOR_TYPE is None, the branch is skipped regardless.
    mod._COLOR_TYPE = None
    obj = object()
    out = mod.serialize_value(obj)
    assert out == str(obj)
    assert out == repr(obj) or "object" in out  # cpython repr matches str


def test_serialize_vector_partial_coords(mod):
    """Vector with missing coords still serializes (x/y/z → None)."""
    class V:
        pass
    v = V()
    v.x = 1
    v.y = None
    v.z = 3
    mod.App.Vector = V
    out = mod.serialize_value(v)
    assert out == {"x": 1, "y": None, "z": 3}


def test_serialize_rotation_axis_uses_getattr(mod):
    """Rotation.Axis may not have x/y/z — getattr returns None safely."""
    class RotAxis:
        pass
    class Rot:
        Angle = 90
        Axis = RotAxis()
    mod.App.Rotation = Rot
    out = mod.serialize_value(Rot())
    assert out["Angle"] == 90
    assert out["Axis"] == {"x": None, "y": None, "z": None}


def test_serialize_rotation_missing_angle(mod):
    """Rotation without Angle attribute falls back to None."""
    class RotAxis2:
        x = y = z = 0
    class Rot2:
        Axis = RotAxis2()
    # No Angle attribute defined.
    mod.App.Rotation = Rot2
    out = mod.serialize_value(Rot2())
    assert out["Angle"] is None


def test_serialize_placement_missing_base(mod):
    """Placement without Base attribute -> serialize_value(getattr(...,None))
    falls through to str(None) = 'None'."""
    class P:
        Rotation = None
    mod.App.Placement = P
    out = mod.serialize_value(P())
    assert out["Base"] == "None"
    assert out["Rotation"] == "None"


def test_serialize_value_isinstance_raises(mod):
    """If App.Vector is a class whose __instancecheck__ raises, we
    gracefully return False rather than propagating."""

    class _BrokenType(type):
        def __instancecheck__(cls, instance):
            raise RuntimeError("boom")

    class _BrokenVec(metaclass=_BrokenType):
        pass

    mod.App.Vector = _BrokenVec
    # serialize_value should NOT raise.
    out = mod.serialize_value(object())
    assert out == str(object())


# ---------------------------------------------------------------------------
# serialize_shape
# ---------------------------------------------------------------------------

def test_serialize_shape_none(mod):
    assert mod.serialize_shape(None) is None


def test_serialize_shape_with_all_attrs(mod):
    class Shape:
        Volume = 12.5
        Area = 50.0
        Vertexes = [1, 2, 3]
        Edges = [1, 2, 3, 4]
        Faces = [1, 2]
    s = mod.serialize_shape(Shape())
    assert s["Volume"] == 12.5
    assert s["Area"] == 50.0
    assert s["VertexCount"] == 3
    assert s["EdgeCount"] == 4
    assert s["FaceCount"] == 2


def test_serialize_shape_missing_attrs(mod):
    """If Vertexes/Edges/Faces are missing, len([]) -> 0."""
    class Shape:
        Volume = 1.0
        Area = 1.0
    s = mod.serialize_shape(Shape())
    assert s["VertexCount"] == 0
    assert s["EdgeCount"] == 0
    assert s["FaceCount"] == 0


def test_serialize_shape_none_collections(mod):
    """If Vertexes/Edges/Faces are None, the `or []` fallback yields 0."""
    class Shape:
        Volume = 1.0
        Area = 1.0
        Vertexes = None
        Edges = None
        Faces = None
    s = mod.serialize_shape(Shape())
    assert s["VertexCount"] == 0
    assert s["EdgeCount"] == 0
    assert s["FaceCount"] == 0


# ---------------------------------------------------------------------------
# serialize_view_object
# ---------------------------------------------------------------------------

def test_serialize_view_object_none(mod):
    assert mod.serialize_view_object(None) is None


def test_serialize_view_object_with_attrs(mod):
    """ShapeColor is a tuple — serialize_value recurses on it (no Color
    type registered), producing a list of serialised floats."""
    class View:
        ShapeColor = (0.5, 0.5, 0.5, 1.0)
        Transparency = 20
        Visibility = True
    v = mod.serialize_view_object(View())
    # Without App.Color registered, the tuple goes through the list branch.
    assert v["ShapeColor"] == [0.5, 0.5, 0.5, 1.0]
    assert v["Transparency"] == 20
    assert v["Visibility"] is True


def test_serialize_view_object_color_registered(mod):
    """Tuples hit the list branch BEFORE the _COLOR_TYPE branch, so
    ShapeColor always becomes a list of float-equal values, never a
    tuple, even when App.Color is registered.

    This is the actual behaviour; the Color branch in ``serialize_value``
    is reachable only by non-tuple Color subclasses (which FreeCAD's
    ``App.Color`` is not). We pin the current behaviour so a future
    change to the ordering would be visible as a failure.
    """
    class _Color(tuple):
        pass
    mod.App.Color = _Color
    mod._COLOR_TYPE = _Color

    class View:
        ShapeColor = _Color((0.5, 0.6, 0.7, 1.0))
        Transparency = 0
        Visibility = True
    v = mod.serialize_view_object(View())
    assert v["ShapeColor"] == [0.5, 0.6, 0.7, 1.0]


def test_serialize_view_object_missing_attrs(mod):
    """When the view object has no ShapeColor/Transparency/Visibility,
    getattr returns None which serialize_value turns into the string 'None'."""
    class View:
        pass
    v = mod.serialize_view_object(View())
    assert v["ShapeColor"] == "None"
    assert v["Transparency"] is None  # raw getattr default
    assert v["Visibility"] is None


# ---------------------------------------------------------------------------
# serialize_object — extra branches
# ---------------------------------------------------------------------------

def test_serialize_object_list(mod):
    """A list is recursively serialized."""
    class Obj:
        Name = "x"
        Label = "X"
        TypeId = "Part::Box"
        PropertiesList = []
        Placement = None
        Shape = None
        ViewObject = None
    out = mod.serialize_object([Obj(), Obj()])
    assert isinstance(out, list)
    assert len(out) == 2
    assert out[0]["Name"] == "x"


def test_serialize_object_document(mod):
    """Object with .Objects + .Name is treated as a Document."""
    class Child:
        Name = "Child1"
        Label = "C1"
        TypeId = "Part::Box"
        PropertiesList = []
        Placement = None
        Shape = None
        ViewObject = None
    class Doc:
        Name = "Doc1"
        Label = "Document 1"
        FileName = "/tmp/foo.FCStd"
        Objects = [Child()]
    out = mod.serialize_object(Doc())
    assert out["Name"] == "Doc1"
    assert out["Label"] == "Document 1"
    assert out["FileName"] == "/tmp/foo.FCStd"
    assert len(out["Objects"]) == 1
    assert out["Objects"][0]["Name"] == "Child1"


def test_serialize_object_property_serialization_error(mod):
    """If accessing a property raises, the value is '<error: ...>'."""
    class Obj:
        Name = "X"
        Label = "X"
        TypeId = "T"
        PropertiesList = ["Broken"]
        def __getattr__(self, name):
            if name == "Broken":
                raise RuntimeError("can't read this")
            raise AttributeError(name)
        Placement = None
        Shape = None
        ViewObject = None
    out = mod.serialize_object(Obj())
    assert "Broken" in out["Properties"]
    assert "error" in out["Properties"]["Broken"].lower()


def test_serialize_object_with_view_object(mod):
    """Object with ViewObject present -> serialized in the result."""
    class View:
        ShapeColor = (1, 0, 0, 1)
        Transparency = 10
        Visibility = True

    class Obj:
        Name = "O"
        Label = "O"
        TypeId = "Part::Box"
        PropertiesList = []
        Placement = None
        Shape = None
        ViewObject = View()

    out = mod.serialize_object(Obj())
    # ShapeColor tuple goes through serialize_value → list
    assert out["ViewObject"]["ShapeColor"] == [1, 0, 0, 1]
    assert out["ViewObject"]["Transparency"] == 10


def test_serialize_object_no_view_object_attribute(mod):
    """Object that does not even expose ViewObject -> result has empty dict."""
    class Obj:
        Name = "O"
        Label = "O"
        TypeId = "Part::Box"
        PropertiesList = []
        Placement = None
        Shape = None
        # No ViewObject attribute at all
    # hasattr would raise if the attribute is missing
    assert not hasattr(Obj(), "ViewObject")
    out = mod.serialize_object(Obj())
    assert out["ViewObject"] == {}


def test_serialize_object_with_shape(mod):
    """Object with Shape -> Shape section populated."""
    class ObjShape:
        Volume = 1.0
        Area = 6.0
        Vertexes = [1, 2, 3, 4]
        Edges = [1]
        Faces = [1, 2]
    class Obj:
        Name = "O"
        Label = "O"
        TypeId = "T"
        PropertiesList = []
        Placement = None
        Shape = ObjShape()
        ViewObject = None
    out = mod.serialize_object(Obj())
    assert out["Shape"]["Volume"] == 1.0
    assert out["Shape"]["VertexCount"] == 4
    assert out["Shape"]["EdgeCount"] == 1
    assert out["Shape"]["FaceCount"] == 2


def test_serialize_object_shape_none(mod):
    """Object with Shape=None -> Shape section is None."""
    class Obj:
        Name = "O"
        Label = "O"
        TypeId = "T"
        PropertiesList = []
        Placement = None
        Shape = None
        ViewObject = None
    out = mod.serialize_object(Obj())
    assert out["Shape"] is None


# ---------------------------------------------------------------------------
# _get_optional_app_type
# ---------------------------------------------------------------------------

def test_get_optional_app_type_returns_class(mod):
    class T:
        pass
    mod.App.SomeType = T
    assert mod._get_optional_app_type("SomeType") is T


def test_get_optional_app_type_returns_tuple(mod):
    class A:
        pass
    class B:
        pass
    mod.App.MultiType = (A, B)
    assert mod._get_optional_app_type("MultiType") == (A, B)


def test_get_optional_app_type_returns_none_for_missing(mod):
    assert mod._get_optional_app_type("DoesNotExist") is None


def test_get_optional_app_type_returns_none_for_non_type(mod):
    """A non-type, non-tuple attribute returns None."""
    mod.App.NotAType = "hello"
    assert mod._get_optional_app_type("NotAType") is None


def test_get_optional_app_type_returns_none_for_partial_tuple(mod):
    """A tuple with a non-type element returns None."""
    mod.App.Mixed = (int, "not-a-type")
    assert mod._get_optional_app_type("Mixed") is None


def test_is_app_instance_safe_returns_false(mod):
    """If App.SomeType is not a class/tuple, _is_app_instance returns False."""
    mod.App.NotAType = "string"
    assert mod._is_app_instance(object(), "NotAType") is False


# ---------------------------------------------------------------------------
# _is_app_instance
# ---------------------------------------------------------------------------

def test_is_app_instance_matches_class(mod):
    class T:
        pass
    mod.App.SomeType = T
    assert mod._is_app_instance(T(), "SomeType") is True
    assert mod._is_app_instance("not a T", "SomeType") is False


def test_is_app_instance_matches_tuple(mod):
    class A:
        pass
    class B(A):
        pass
    mod.App.MultiType = (A,)
    assert mod._is_app_instance(B(), "MultiType") is True


def test_is_app_instance_swallows_exception(mod):
    """If isinstance raises, we return False."""

    class _BrokenType(type):
        def __instancecheck__(cls, instance):
            raise RuntimeError("boom")

    class _T(metaclass=_BrokenType):
        pass

    mod.App.BrokenType = _T
    # Should not raise.
    assert mod._is_app_instance(object(), "BrokenType") is False
