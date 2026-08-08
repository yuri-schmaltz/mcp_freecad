from types import SimpleNamespace

try:
    import FreeCAD as App
except Exception:
    # Running outside FreeCAD — provide a minimal placeholder object so
    # attribute lookups do not raise at import time.
    App = SimpleNamespace()


def _get_optional_app_type(name: str) -> type | tuple[type, ...] | None:
    value = getattr(App, name, None)
    if isinstance(value, type):
        return value
    if isinstance(value, tuple) and all(isinstance(item, type) for item in value):
        return value
    return None


def _is_app_instance(value, name: str) -> bool:
    """Safely check whether ``value`` is an instance of ``App.<name>``.

    This avoids referencing non-existent attributes on the FreeCAD module
    when the code runs outside FreeCAD (e.g. during unit tests).
    """
    typ = getattr(App, name, None)
    if isinstance(typ, type) or (isinstance(typ, tuple) and all(isinstance(t, type) for t in typ)):
        try:
            return isinstance(value, typ)
        except Exception:
            return False
    return False


_COLOR_TYPE = _get_optional_app_type("Color")


def serialize_value(value):
    if isinstance(value, (int, float, str, bool)):
        return value
    elif _is_app_instance(value, "Vector"):
        return {"x": value.x, "y": value.y, "z": value.z}
    elif _is_app_instance(value, "Rotation"):
        return {
            "Axis": {"x": getattr(value.Axis, "x", None), "y": getattr(value.Axis, "y", None), "z": getattr(value.Axis, "z", None)},
            "Angle": getattr(value, "Angle", None),
        }
    elif _is_app_instance(value, "Placement"):
        return {
            "Base": serialize_value(getattr(value, "Base", None)),
            "Rotation": serialize_value(getattr(value, "Rotation", None)),
        }
    elif isinstance(value, (list, tuple)):
        return [serialize_value(v) for v in value]
    elif _COLOR_TYPE is not None and isinstance(value, _COLOR_TYPE):
        return tuple(value)
    else:
        return str(value)


def serialize_shape(shape):
    if shape is None:
        return None
    return {
        "Volume": getattr(shape, "Volume", None),
        "Area": getattr(shape, "Area", None),
        "VertexCount": len(getattr(shape, "Vertexes", []) or []),
        "EdgeCount": len(getattr(shape, "Edges", []) or []),
        "FaceCount": len(getattr(shape, "Faces", []) or []),
    }


def serialize_view_object(view):
    if view is None:
        return None
    return {
        "ShapeColor": serialize_value(getattr(view, "ShapeColor", None)),
        "Transparency": getattr(view, "Transparency", None),
        "Visibility": getattr(view, "Visibility", None),
    }


def serialize_object(obj):
    if isinstance(obj, list):
        return [serialize_object(item) for item in obj]
    # Treat documents by presence of Objects/Name/Label rather than
    # relying solely on App.Document which may not be importable in tests
    if hasattr(obj, "Objects") and hasattr(obj, "Name"):
        return {
            "Name": getattr(obj, "Name", None),
            "Label": getattr(obj, "Label", None),
            "FileName": getattr(obj, "FileName", None),
            "Objects": [serialize_object(child) for child in getattr(obj, "Objects", [])],
        }

    result = {
        "Name": getattr(obj, "Name", None),
        "Label": getattr(obj, "Label", None),
        "TypeId": getattr(obj, "TypeId", None),
        "Properties": {},
        "Placement": serialize_value(getattr(obj, "Placement", None)),
        "Shape": serialize_shape(getattr(obj, "Shape", None)),
        "ViewObject": {},
    }

    for prop in getattr(obj, "PropertiesList", []) or []:
        try:
            result["Properties"][prop] = serialize_value(getattr(obj, prop))
        except Exception as e:
            result["Properties"][prop] = f"<error: {str(e)}>"

    if hasattr(obj, "ViewObject") and obj.ViewObject is not None:
        view = obj.ViewObject
        result["ViewObject"] = serialize_view_object(view)

    return result
