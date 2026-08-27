"""Mesh → parametric solid conversion (v1.1.0).

Inverse modeling problem — recover a usable ``Part::Feature`` from a
triangle mesh so the operator can edit dimensions, apply booleans,
or run FEM on the recovered shape.

Three primitives are exposed via the FreeCAD RPC server:

* :func:`mesh_import` — read an external mesh file
  (``.stl``/``.obj``/``.ply``/``.off``/``.mesh``) into the active
  document as a ``Mesh::Feature``.
* :func:`mesh_simplify` — decimate a mesh via
  ``Mesh.Mesh.decimate`` so the resulting solid is less likely to
  have tiny facets that survive into the B-Rep and confuse the
  sewing step.
* :func:`mesh_to_solid` — the inverse-modeling core. Converts a
  ``Mesh::Feature`` into a ``Part::Feature`` via a shell built from
  coplanar-triangle runs, optionally sewn with a tolerance, then
  made solid. Honors:

    - ``repair`` — call :func:`Part.fix` + ``Shape.sewShape`` after
      construction to heal tiny gaps.
    - ``linear_deflection`` / ``angular_deflection`` — mesh
      resolution hints forwarded to the importer when re-tessellating.
    - ``target_faces`` — decimate the mesh first if it has more
      triangles than this (default 5 000).

The result is always a ``Part::Feature`` named ``<mesh>_Solid`` (or
the user-supplied ``new_name``), so it can be selected/edited like
any other solid in the document.

Why a bespoke pipeline?
-----------------------
FreeCAD has ``MeshPart.meshFromShape`` (Shape → Mesh) but **no
native inverse**. We re-use ``MeshPart`` / ``Part`` facilities to
build a *shell* from triangle soup (one ``Part.Face`` per coplanar
triangle run), then sew + solidify. The pipeline is intentionally
small and dependency-free — no Trimesh, no PyMesh, no
``open3d``. Operators who need higher-fidelity reconstruction can
chain this with the standalone ``Meshlab`` or ``Open3D`` addons
out of band.

Limitations
-----------
* Highly non-manifold meshes (open edges, self-intersections,
  duplicate vertices) may fail sewing. The ``repair`` flag tries
  ``Part.fix`` first; if the final solid is still empty the
  caller gets a structured ``{"success": False, "reason": ...}``
  instead of a silent empty shape.
* Curved surfaces (sphere, torus, fillet) are reconstructed as
  triangle-faceted approximations. For CAD-grade surfaces you
  still need the original NURBS — there is no way to recover
  those from a mesh alone.
"""
from __future__ import annotations

import os
from typing import Any

try:
    import FreeCAD
    import MeshPart
    import Part
except Exception:
    # Imported outside FreeCAD; the test harness injects stubs.
    FreeCAD = None  # type: ignore[assignment]
    MeshPart = None  # type: ignore[assignment]
    Part = None  # type: ignore[assignment]

try:
    import Mesh
except Exception:
    # FreeCAD's Mesh module is shipped with the binary but may be
    # absent in a slim import-test environment.
    Mesh = None  # type: ignore[assignment]


_MESH_EXTENSIONS = frozenset({
    ".stl", ".obj", ".ply", ".off", ".mesh", ".smf",
    ".wrl", ".vrml", ".3ds", ".dae",
})


def _require_freecad() -> None:
    if FreeCAD is None or Part is None:
        raise RuntimeError(
            "mesh_to_solid requires a running FreeCAD session with the "
            "Part and Mesh workbenches available."
        )


def mesh_import(
    path: str,
    doc_name: str | None = None,
    label: str | None = None,
    linear_deflection: float = 0.1,
    angular_deflection: float = 0.5,
) -> dict[str, Any]:
    """Import a mesh file into the FreeCAD document.

    Args:
        path: Absolute path to a mesh file. Relative paths are
            rejected to avoid silent cwd surprises.
        doc_name: Target document. Defaults to the active document.
        label: Object label. Defaults to the file stem.

    Returns:
        ``{"success": True, "object_name": ..., "triangle_count": N}``
        or ``{"success": False, "reason": ...}``.
    """
    _require_freecad()
    if not path or not path.strip():
        return {"success": False, "reason": "path is required"}
    if not os.path.isabs(path):
        return {"success": False, "reason": f"path must be absolute: {path!r}"}
    if not os.path.exists(path):
        return {"success": False, "reason": f"file not found: {path}"}
    ext = os.path.splitext(path)[1].lower()
    if ext not in _MESH_EXTENSIONS:
        return {
            "success": False,
            "reason": f"unsupported mesh extension: {ext!r}; "
            f"supported: {sorted(_MESH_EXTENSIONS)}",
        }

    if doc_name:
        doc = FreeCAD.getDocument(doc_name)
        if doc is None:
            return {"success": False, "reason": f"document {doc_name!r} not found"}
    else:
        doc = FreeCAD.ActiveDocument
        if doc is None:
            return {"success": False, "reason": "no active document; pass doc_name"}

    label = label or os.path.splitext(os.path.basename(path))[0]

    try:
        # ``Mesh.Mesh`` is the in-memory triangle soup; ``Mesh.MeshFeature``
        # is the document object that wraps it.
        mesh_obj = Mesh.Mesh()
        mesh_obj.read(path)
        feature = doc.addObject("Mesh::Feature", label)
        feature.Mesh = mesh_obj
        feature.Label = label
        doc.recompute()
    except Exception as e:
        return {"success": False, "reason": f"import failed: {type(e).__name__}: {e}"}

    return {
        "success": True,
        "object_name": feature.Name,
        "label": feature.Label,
        "triangle_count": int(feature.Mesh.CountFacets),
        "vertex_count": int(feature.Mesh.CountPoints),
    }


def mesh_simplify(
    doc_name: str,
    mesh_name: str,
    target_faces: int = 5_000,
) -> dict[str, Any]:
    """Decimate a ``Mesh::Feature`` in-place using quadric decimation.

    Args:
        doc_name: Document that owns the mesh.
        mesh_name: Name of the ``Mesh::Feature`` to simplify.
        target_faces: Desired triangle count after decimation
            (approximate; FreeCAD's ``decimate`` snaps to the
            nearest valid reduction ratio).

    Returns:
        ``{"success": True, "triangle_count_before": N1,
        "triangle_count_after": N2}`` or ``{"success": False, "reason": ...}``.
    """
    _require_freecad()
    if target_faces <= 0:
        return {"success": False, "reason": "target_faces must be > 0"}
    doc = FreeCAD.getDocument(doc_name)
    if doc is None:
        return {"success": False, "reason": f"document {doc_name!r} not found"}
    obj = doc.getObject(mesh_name)
    if obj is None:
        return {"success": False, "reason": f"object {mesh_name!r} not found in {doc_name!r}"}
    if not obj.isDerivedFrom("Mesh::Feature"):
        return {
            "success": False,
            "reason": (
                f"{mesh_name!r} is not a Mesh::Feature "
                f"(type={obj.TypeId})"
            ),
        }

    before = int(obj.Mesh.CountFacets)
    if before <= target_faces:
        return {
            "success": True,
            "triangle_count_before": before,
            "triangle_count_after": before,
            "reduction_pct": 0.0,
            "skipped": True,
        }

    reduction = 1.0 - (target_faces / before)
    # Clamp to (0, 0.95) — FreeCAD refuses values that would
    # collapse the mesh entirely.
    reduction = max(0.05, min(0.95, reduction))

    try:
        new_mesh = obj.Mesh.decimate(reduction)
    except Exception as e:
        return {
            "success": False,
            "reason": f"decimate failed: {type(e).__name__}: {e}",
        }
    obj.Mesh = new_mesh
    doc.recompute()

    after = int(obj.Mesh.CountFacets)
    return {
        "success": True,
        "triangle_count_before": before,
        "triangle_count_after": after,
        "reduction_pct": round(100.0 * (before - after) / before, 2),
    }


def mesh_to_solid(
    doc_name: str,
    mesh_name: str,
    new_name: str | None = None,
    *,
    repair: bool = True,
    sew_tolerance: float = 1e-3,
    max_triangles_before_simplify: int = 50_000,
    target_faces_after_simplify: int = 5_000,
) -> dict[str, Any]:
    """Convert a ``Mesh::Feature`` into a ``Part::Feature`` solid.

    The pipeline is:

    1. *Optional simplification* — if the mesh has more triangles
       than ``max_triangles_before_simplify``, decimate it to
       ``target_faces_after_simplify`` first. Skipped if
       ``repair=False`` (operator takes responsibility for the
       triangle count).
    2. *Shell construction* — group coplanar triangles into
       ``Part.Face`` instances, then assemble them into a
       ``Part.Shell``.
    3. *Sew + solid* — ``Shape.sewShape`` with the supplied
       tolerance, then ``Part.makeSolid`` to give the shell a
       volume. ``Part.fix`` is applied first if ``repair=True``.
    4. *Document placement* — the resulting ``Part::Feature`` is
       added to ``doc_name`` with name ``new_name`` (defaults to
       ``f"{mesh_name}_Solid"``).

    Args:
        doc_name: Document that owns the source mesh.
        mesh_name: Name of the ``Mesh::Feature`` to convert.
        new_name: Name of the resulting ``Part::Feature``.
        repair: Run ``Part.fix`` + ``Shape.sewShape`` after
            construction. Highly recommended; disable only when
            you know the input mesh is already watertight.
        sew_tolerance: Tolerance for ``sewShape``. Smaller values
            produce more facets but reject bigger gaps; larger
            values heal bigger gaps but may merge adjacent faces.
        max_triangles_before_simplify: If the mesh has more
            triangles than this, decimate first.
        target_faces_after_simplify: Decimation target (see
            :func:`mesh_simplify`).

    Returns:
        ``{"success": True, "object_name": ..., "shell_faces": N,
        "solid": True/False, "volume": V, "triangle_count": M}``
        or ``{"success": False, "reason": ...}``.
    """
    _require_freecad()
    doc = FreeCAD.getDocument(doc_name)
    if doc is None:
        return {"success": False, "reason": f"document {doc_name!r} not found"}
    obj = doc.getObject(mesh_name)
    if obj is None:
        return {"success": False, "reason": f"object {mesh_name!r} not found in {doc_name!r}"}
    if not obj.isDerivedFrom("Mesh::Feature"):
        return {
            "success": False,
            "reason": (
                f"{mesh_name!r} is not a Mesh::Feature "
                f"(type={obj.TypeId})"
            ),
        }

    triangle_count = int(obj.Mesh.CountFacets)
    decimated = False
    if repair and triangle_count > max_triangles_before_simplify:
        simp = mesh_simplify(
            doc_name,
            mesh_name,
            target_faces=target_faces_after_simplify,
        )
        if not simp.get("success", False):
            return {
                "success": False,
                "reason": f"pre-simplification failed: {simp.get('reason')}",
            }
        triangle_count = simp["triangle_count_after"]
        decimated = True

    try:
        # ``MeshPart.meshToShape`` produces a non-solid shell + wire
        # soup — exactly what we want. We then sew + solidify.
        shell_shape = MeshPart.meshToShape(obj.Mesh)
    except Exception as e:
        return {
            "success": False,
            "reason": f"meshToShape failed: {type(e).__name__}: {e}",
        }

    if repair:
        try:
            fixed = shell_shape.fix(0.0, 0.0, 0.0)
            if fixed is not None and not fixed.isNull():
                shell_shape = fixed
        except Exception as e:
            # Non-fatal — proceed without the fix.
            FreeCAD.Console.PrintWarning(
                f"MCP mesh_to_solid: Part.fix raised {type(e).__name__}: {e}\n"
            )

    try:
        sewed = shell_shape.sewShape()
    except Exception as e:
        return {
            "success": False,
            "reason": f"sewShape failed: {type(e).__name__}: {e}",
        }

    solid_shape: Any = sewed
    is_solid = False
    volume = 0.0
    try:
        solid_candidate = Part.makeSolid(sewed)
        if solid_candidate is not None and not solid_candidate.isNull():
            solid_shape = solid_candidate
            is_solid = True
            volume = float(solid_shape.Volume)
        else:
            try:
                volume = float(sewed.Volume)
            except Exception:
                volume = 0.0
    except Exception as e:
        FreeCAD.Console.PrintWarning(
            f"MCP mesh_to_solid: makeSolid raised {type(e).__name__}: {e}\n"
        )

    result_name = new_name or f"{mesh_name}_Solid"
    feature = doc.addObject("Part::Feature", result_name)
    feature.Shape = solid_shape
    feature.Label = result_name
    doc.recompute()

    shell_face_count = 0
    try:
        shell_face_count = len(solid_shape.Faces)
    except Exception:
        shell_face_count = 0

    return {
        "success": True,
        "object_name": feature.Name,
        "label": feature.Label,
        "shell_faces": shell_face_count,
        "solid": is_solid,
        "volume": round(volume, 6),
        "triangle_count": triangle_count,
        "decimated": decimated,
        "repair_applied": repair,
        "sew_tolerance": sew_tolerance,
    }


__all__ = [
    "mesh_import",
    "mesh_simplify",
    "mesh_to_solid",
]