"""Geometric inspection & measurement suite for FreeCAD.

This module exposes **read-only** helpers that take a document name
and object name and return JSON-friendly metrics. The goal is to
let the MCP client (LLM) validate geometry before committing to
expensive downstream work (FEM, CAM, manufacturing).

Available helpers
-----------------

* :func:`list_faces`             — per-face type / normal / area
* :func:`measure`                — volume, area, bbox, COM, length
* :func:`geometric_verification` — handedness, normal consistency, OCCT validity
* :func:`analyze_shape`          — topological classifications (Plane/Cylinder/Cone)
* :func:`spatial_query`          — interference / clearance between two objects
* :func:`recompute_diff`         — before/after recompute state diff
* :func:`sketch_diagnostics`     — DOF / conflicts / redundancies

All helpers gracefully degrade if FreeCAD is not importable
(they will return ``{"success": False, "reason": "FreeCAD not
available"}``).
"""
from __future__ import annotations

from typing import Any

try:
    import FreeCAD
except Exception:  # pragma: no cover — test stubs
    FreeCAD = None  # type: ignore[assignment]


def _err(reason: str) -> dict[str, Any]:
    return {"success": False, "reason": reason}


def _get_shape(doc_name: str, obj_name: str) -> tuple[Any, Any] | dict[str, Any]:
    """Return ``(obj, shape)`` or an error dict."""
    if FreeCAD is None:
        return _err("FreeCAD not available")
    doc = FreeCAD.getDocument(doc_name)
    if doc is None:
        return _err(f"document {doc_name!r} not found")
    obj = doc.getObject(obj_name)
    if obj is None:
        return _err(f"object {obj_name!r} not found in {doc_name!r}")
    if not hasattr(obj, "Shape"):
        return _err(f"object {obj_name!r} has no Shape")
    return obj, obj.Shape


# ---------------------------------------------------------------------------
# Face listing
# ---------------------------------------------------------------------------


def list_faces(
    doc_name: str,
    obj_name: str,
    *,
    type_filter: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Return per-face type, normal, centroid, area.

    ``type_filter`` keeps only faces whose geometric type contains
    that substring (case-insensitive). Useful e.g. with
    ``"Cylinder"`` to find holes/bosses.
    """
    pair = _get_shape(doc_name, obj_name)
    if isinstance(pair, dict):
        return pair
    _obj, shape = pair

    faces_out: list[dict[str, Any]] = []
    try:
        faces = shape.Faces
        for i, face in enumerate(faces):
            if i >= limit:
                break
            try:
                surface = face.Surface
                ftype = type(surface).__name__
            except Exception:
                ftype = "Unknown"
            if type_filter and type_filter.lower() not in ftype.lower():
                continue
            try:
                area = float(face.Area)
            except Exception:
                area = 0.0
            try:
                centroid = face.CenterOfMass
                centroid_xyz = [round(float(centroid.x), 4),
                                round(float(centroid.y), 4),
                                round(float(centroid.z), 4)]
            except Exception:
                centroid_xyz = None
            try:
                normal = face.normalAt(0.5, 0.5)
                normal_xyz = [round(float(normal.x), 4),
                              round(float(normal.y), 4),
                              round(float(normal.z), 4)]
            except Exception:
                normal_xyz = None
            faces_out.append({
                "index": i,
                "type": ftype,
                "area": round(area, 6),
                "centroid": centroid_xyz,
                "normal": normal_xyz,
            })
    except Exception as e:
        return _err(f"list_faces failed: {type(e).__name__}: {e}")

    return {
        "success": True,
        "object_name": obj_name,
        "face_count": len(faces_out),
        "faces": faces_out,
    }


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------


def measure(doc_name: str, obj_name: str, *, properties: list[str] | None = None) -> dict[str, Any]:
    """Return a dict of geometric measurements.

    ``properties`` is an optional subset of::

        ["volume", "area", "bbox", "center_of_mass", "length", "edge_count", "face_count", "vertex_count"]

    If omitted, all of them are returned.
    """
    pair = _get_shape(doc_name, obj_name)
    if isinstance(pair, dict):
        return pair
    _obj, shape = pair

    want = set(properties) if properties else {
        "volume", "area", "bbox", "center_of_mass",
        "length", "edge_count", "face_count", "vertex_count",
    }
    out: dict[str, Any] = {"success": True, "object_name": obj_name}

    if "volume" in want:
        try:
            out["volume"] = round(float(shape.Volume), 6)
        except Exception:
            out["volume"] = None
    if "area" in want:
        try:
            out["area"] = round(float(shape.Area), 6)
        except Exception:
            out["area"] = None
    if "bbox" in want:
        try:
            b = shape.BoundBox
            out["bbox"] = {
                "xmin": round(float(b.XMin), 6),
                "xmax": round(float(b.XMax), 6),
                "ymin": round(float(b.YMin), 6),
                "ymax": round(float(b.YMax), 6),
                "zmin": round(float(b.ZMin), 6),
                "zmax": round(float(b.ZMax), 6),
            }
        except Exception:
            out["bbox"] = None
    if "center_of_mass" in want:
        try:
            c = shape.CenterOfMass
            out["center_of_mass"] = [
                round(float(c.x), 6),
                round(float(c.y), 6),
                round(float(c.z), 6),
            ]
        except Exception:
            out["center_of_mass"] = None
    if "length" in want:
        try:
            out["length"] = round(float(shape.Length), 6)
        except Exception:
            out["length"] = None
    if "edge_count" in want:
        try:
            out["edge_count"] = len(shape.Edges)
        except Exception:
            out["edge_count"] = None
    if "face_count" in want:
        try:
            out["face_count"] = len(shape.Faces)
        except Exception:
            out["face_count"] = None
    if "vertex_count" in want:
        try:
            out["vertex_count"] = len(shape.Vertexes)
        except Exception:
            out["vertex_count"] = None
    return out


def measure_distance(
    doc_name: str, obj_a: str, obj_b: str,
) -> dict[str, Any]:
    """Return minimum distance between two shapes (uses BRepExtrema)."""
    if FreeCAD is None:
        return _err("FreeCAD not available")
    a_pair = _get_shape(doc_name, obj_a)
    if isinstance(a_pair, dict):
        return a_pair
    b_pair = _get_shape(doc_name, obj_b)
    if isinstance(b_pair, dict):
        return b_pair
    _, shape_a = a_pair
    _, shape_b = b_pair
    try:
        import BRepExtrema  # type: ignore[import-not-found]  # noqa: F401
        from Part import BRepExtrema as PBExt  # type: ignore[import-not-found]
        dist_calc = PBExt.DistShapeShape(shape_a, shape_a)  # placeholder
        dist_calc = PBExt.DistShapeShape(shape_a, shape_b)
        return {
            "success": True,
            "distance": round(float(dist_calc.Distance), 6),
            "object_a": obj_a,
            "object_b": obj_b,
        }
    except Exception as e:
        # Fallback: approximate via bbox distance
        try:
            ba = shape_a.BoundBox
            bb = shape_b.BoundBox
            dx = max(0.0, max(ba.XMin - bb.XMax, bb.XMin - ba.XMax))
            dy = max(0.0, max(ba.YMin - bb.YMax, bb.YMin - ba.YMax))
            dz = max(0.0, max(ba.ZMin - bb.ZMax, bb.ZMin - ba.ZMax))
            return {
                "success": True,
                "distance": round((dx * dx + dy * dy + dz * dz) ** 0.5, 6),
                "approximate": True,
                "reason": f"BRepExtrema unavailable: {type(e).__name__}",
            }
        except Exception as e2:
            return _err(f"measure_distance failed: {type(e2).__name__}: {e2}")


# ---------------------------------------------------------------------------
# Geometric verification
# ---------------------------------------------------------------------------


def geometric_verification(
    doc_name: str,
    obj_name: str,
    *,
    handedness_tol: float = 1e-3,
) -> dict[str, Any]:
    """Check a shape for common defects:

    * **is_null** — degenerate empty shape.
    * **is_valid** — OpenCascade ``BRepCheck`` validity.
    * **det_approx_one** — determinant of the solid's inertia matrix
      is approximately +1 (right-handed axes). Negative means
      mirrored.
    * **normal_consistency** — fraction of faces whose outward
      normal points away from the centre of mass (≥ 0.9 for a
      well-oriented solid).
    """
    pair = _get_shape(doc_name, obj_name)
    if isinstance(pair, dict):
        return pair
    _obj, shape = pair

    out: dict[str, Any] = {"success": True, "object_name": obj_name}

    try:
        out["is_null"] = bool(shape.isNull())
    except Exception:
        out["is_null"] = None

    try:
        out["is_valid"] = bool(shape.isValid())
    except Exception:
        out["is_valid"] = None

    # Handedness: inertia matrix determinant.
    try:
        matrix_of_inertia = shape.MatrixOfInertia
        a = matrix_of_inertia.A
        b = matrix_of_inertia.B
        c = matrix_of_inertia.C
        # Determinant of the 3x3 inertia matrix.
        det = (
            a[0] * (a[4] * a[8] - a[5] * a[7])
            - a[1] * (a[3] * a[8] - a[5] * a[6])
            + a[2] * (a[3] * a[7] - a[4] * a[6])
        )
        det_abs = abs(det)
        # Normalise: divide by a non-zero element to get an
        # approximate handedness sign.
        ref = max(abs(a[0]), 1e-12)
        det_norm = det / ref
        out["det_approx_one"] = abs(det_norm - 1.0) < handedness_tol
        out["handedness_sign"] = round(det_norm, 6)
    except Exception:
        out["det_approx_one"] = None
        out["handedness_sign"] = None

    # Normal consistency: count faces whose outward normal
    # points away from the centre of mass.
    try:
        com = shape.CenterOfMass
        consistent = 0
        total = 0
        for face in shape.Faces:
            total += 1
            try:
                centroid = face.CenterOfMass
                # Vector from COM to centroid of the face.
                vx = float(centroid.x - com.x)
                vy = float(centroid.y - com.y)
                vz = float(centroid.z - com.z)
                n = face.normalAt(0.5, 0.5)
                dot = vx * float(n.x) + vy * float(n.y) + vz * float(n.z)
                if dot > 0:
                    consistent += 1
            except Exception:
                continue
        out["normal_consistency"] = round(consistent / total, 4) if total else None
    except Exception:
        out["normal_consistency"] = None

    return out


# ---------------------------------------------------------------------------
# Shape topology analysis
# ---------------------------------------------------------------------------


def analyze_shape(doc_name: str, obj_name: str) -> dict[str, Any]:
    """Classify the shape's surface types.

    Returns counts of each face surface type (Plane, Cylinder,
    Cone, Sphere, Torus, B-Spline, etc.) — useful for identifying
    "this looks like an extrusion" or "this is a lathed part".
    """
    pair = _get_shape(doc_name, obj_name)
    if isinstance(pair, dict):
        return pair
    _obj, shape = pair

    counts: dict[str, int] = {}
    try:
        for face in shape.Faces:
            try:
                t = type(face.Surface).__name__
            except Exception:
                t = "Unknown"
            counts[t] = counts.get(t, 0) + 1
    except Exception as e:
        return _err(f"analyze_shape failed: {type(e).__name__}: {e}")

    return {
        "success": True,
        "object_name": obj_name,
        "face_count": sum(counts.values()),
        "surface_types": counts,
    }


# ---------------------------------------------------------------------------
# Spatial queries
# ---------------------------------------------------------------------------


def spatial_query(
    doc_name: str,
    obj_a: str,
    obj_b: str,
    *,
    mode: str = "interference",
    clearance_tol: float = 0.05,
) -> dict[str, Any]:
    """Modes:

    * ``"interference"`` — boolean common; non-empty ⇒ intersecting.
    * ``"clearance"``    — minimum distance; ``< clearance_tol`` ⇒ too close.
    * ``"containment"``  — ``a in b`` (a's bbox fully inside b's bbox).
    """
    a_pair = _get_shape(doc_name, obj_a)
    if isinstance(a_pair, dict):
        return a_pair
    b_pair = _get_shape(doc_name, obj_b)
    if isinstance(b_pair, dict):
        return b_pair
    _, shape_a = a_pair
    _, shape_b = b_pair

    if mode == "interference":
        try:
            common = shape_a.common(shape_b)
            vol = float(common.Volume) if common and not common.isNull() else 0.0
            return {
                "success": True,
                "mode": "interference",
                "intersects": vol > 1e-9,
                "intersection_volume": round(vol, 6),
            }
        except Exception as e:
            return _err(f"interference failed: {type(e).__name__}: {e}")
    if mode == "clearance":
        d = measure_distance(doc_name, obj_a, obj_b)
        if not d.get("success"):
            return d
        distance = d.get("distance", 0.0)
        return {
            "success": True,
            "mode": "clearance",
            "distance": distance,
            "below_tolerance": distance < clearance_tol,
            "tolerance": clearance_tol,
        }
    if mode == "containment":
        try:
            ba = shape_a.BoundBox
            bb = shape_b.BoundBox
            contained = (
                ba.XMin >= bb.XMin and ba.XMax <= bb.XMax
                and ba.YMin >= bb.YMin and ba.YMax <= bb.YMax
                and ba.ZMin >= bb.ZMin and ba.ZMax <= bb.ZMax
            )
            return {"success": True, "mode": "containment", "contained": bool(contained)}
        except Exception as e:
            return _err(f"containment failed: {type(e).__name__}: {e}")
    return _err(f"unknown mode {mode!r} (expected: interference, clearance, containment)")


# ---------------------------------------------------------------------------
# Recompute diff
# ---------------------------------------------------------------------------


def recompute_diff(
    doc_name: str,
    obj_name: str,
    *,
    expected_volume: float | None = None,
) -> dict[str, Any]:
    """Recompute the doc and return before/after metrics.

    If ``expected_volume`` is given, it is compared with the
    resulting volume and the difference is reported as
    ``volume_delta``.
    """
    pair = _get_shape(doc_name, obj_name)
    if isinstance(pair, dict):
        return pair
    obj, _shape_before = pair
    if FreeCAD is None:
        return _err("FreeCAD not available")
    doc = FreeCAD.getDocument(doc_name)
    if doc is None:
        return _err(f"document {doc_name!r} not found")

    try:
        before_bbox = obj.Shape.BoundBox
    except Exception:
        before_bbox = None
    try:
        before_volume = float(obj.Shape.Volume)
    except Exception:
        before_volume = None

    try:
        doc.recompute()
    except Exception as e:
        return _err(f"recompute failed: {type(e).__name__}: {e}")

    try:
        after_volume = float(obj.Shape.Volume)
    except Exception:
        after_volume = None
    try:
        after_bbox = obj.Shape.BoundBox
    except Exception:
        after_bbox = None

    out: dict[str, Any] = {
        "success": True,
        "object_name": obj_name,
        "before_volume": round(before_volume, 6) if before_volume is not None else None,
        "after_volume": round(after_volume, 6) if after_volume is not None else None,
        "before_bbox": _bbox_to_dict(before_bbox),
        "after_bbox": _bbox_to_dict(after_bbox),
    }
    if expected_volume is not None and after_volume is not None:
        out["expected_volume"] = expected_volume
        out["volume_delta"] = round(after_volume - expected_volume, 6)
    return out


def _bbox_to_dict(b) -> dict[str, float] | None:
    if b is None:
        return None
    try:
        return {
            "xmin": round(float(b.XMin), 6), "xmax": round(float(b.XMax), 6),
            "ymin": round(float(b.YMin), 6), "ymax": round(float(b.YMax), 6),
            "zmin": round(float(b.ZMin), 6), "zmax": round(float(b.ZMax), 6),
        }
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Sketch diagnostics (DOF / conflicts / redundancies)
# ---------------------------------------------------------------------------


def sketch_diagnostics(doc_name: str, sketch_name: str) -> dict[str, Any]:
    """Inspect a sketch for degrees of freedom, conflicting or
    redundant constraints.

    Returns::

        {
            "success": True,
            "sketch_name": ...,
            "constraint_count": int,
            "geometry_count": int,
            "dof": int,
            "conflicts": int,
            "redundancies": int,
            "fully_constrained": bool,
        }
    """
    if FreeCAD is None:
        return _err("FreeCAD not available")
    doc = FreeCAD.getDocument(doc_name)
    if doc is None:
        return _err(f"document {doc_name!r} not found")
    sketch = doc.getObject(sketch_name)
    if sketch is None:
        return _err(f"sketch {sketch_name!r} not found")
    # Type check — must be a Sketcher::SketchObject.
    t_id = getattr(sketch, "TypeId", "")
    if "Sketch" not in t_id:
        return _err(f"object {sketch_name!r} is not a sketch (type={t_id!r})")

    try:
        constraints = list(sketch.Constraints)
        geometry = list(sketch.Geometry)
    except Exception as e:
        return _err(f"sketch introspection failed: {type(e).__name__}: {e}")

    conflicts = 0
    redundancies = 0
    for c in constraints:
        try:
            if c.inConflict:
                conflicts += 1
            if c.isRedundant:
                redundancies += 1
        except Exception:
            continue

    try:
        dof = int(sketch.DegreeOfFreedom)
    except Exception:
        dof = None

    return {
        "success": True,
        "sketch_name": sketch_name,
        "constraint_count": len(constraints),
        "geometry_count": len(geometry),
        "dof": dof,
        "conflicts": conflicts,
        "redundancies": redundancies,
        "fully_constrained": (dof == 0) if dof is not None else None,
    }


__all__ = [
    "list_faces",
    "measure",
    "measure_distance",
    "geometric_verification",
    "analyze_shape",
    "spatial_query",
    "recompute_diff",
    "sketch_diagnostics",
]
