"""Bill of Materials (BOM) export (v1.1.1).

Given a FreeCAD document, produce a structured BOM listing every
object with its type, label, name, and key dimensions. Output can
be JSON (machine-readable, consumed by ERP/MES) or CSV (human-
readable, paste-able into Excel).

The algorithm is intentionally simple — recurse through every
object, capture the identifying properties (typically
``TypeId`` + ``Properties.Label`` + a few geometric dimensions),
and dedupe by ``(TypeId, dim_signature)``.

Limitations
-----------
* Fasteners and other catalog parts (DIN/ISO/ANSI screws) are NOT
  auto-classified. The operator marks them via the
  ``standard`` property on the part file. We surface ``standard``
  when present so the LLM can decide.
* Quantity is always 1 unless the assembly tooling (Assembly4 /
  A2plus) sets a ``Count`` property. We surface ``Count`` when
  present, otherwise default to 1.
* Sub-assemblies are listed at every level; the operator or the
  LLM groups them downstream.
"""
from __future__ import annotations

import csv
import io
import json
from typing import Any

try:
    import FreeCAD
except Exception:
    FreeCAD = None  # type: ignore[assignment]


def _require_freecad() -> None:
    if FreeCAD is None:
        raise RuntimeError(
            "bom_export requires a running FreeCAD session."
        )


# Properties whose value we surface as a *dimension* in the BOM.
# Order matters — the first 4 are listed in the CSV header in this
# order; others go in a JSON-only ``extra`` dict.
_DEFAULT_DIM_PROPERTIES = (
    "Length",
    "Width",
    "Height",
    "Radius",
    "Diameter",
    "Size",
    "ThreadSize",
    "Standard",
)


def _object_bom_entry(obj: Any) -> dict[str, Any]:
    """Extract the BOM-relevant fields from a FreeCAD object."""
    props: dict[str, Any] = {}
    try:
        if hasattr(obj, "PropertiesList"):
            for p in obj.PropertiesList:
                try:
                    props[p] = getattr(obj, p)
                except Exception:
                    continue
    except Exception:
        pass

    dimensions: dict[str, Any] = {}
    for key in _DEFAULT_DIM_PROPERTIES:
        if key in props:
            try:
                v = props[key]
                if isinstance(v, (int, float, str, bool)) or v is None:
                    dimensions[key] = v
            except Exception:
                continue

    # Extras (non-dimension properties) — useful for callers that
    # want the full object metadata.
    extras = {
        k: v
        for k, v in props.items()
        if k not in dimensions and k not in {"Label", "Name"}
    }

    # Quantity — default 1; honour ``Count`` if the workbench sets it.
    qty = 1
    try:
        if "Count" in props:
            qty = max(1, int(props["Count"]))
    except Exception:
        qty = 1

    type_id = ""
    try:
        type_id = obj.TypeId
    except Exception:
        type_id = ""

    return {
        "name": getattr(obj, "Name", ""),
        "label": getattr(obj, "Label", ""),
        "type": type_id,
        "quantity": qty,
        "dimensions": dimensions,
        "extra": extras,
    }


def bom_export(
    doc_name: str,
    fmt: str = "json",
    include_extras: bool = False,
    group_by_type: bool = True,
) -> dict[str, Any]:
    """Export a Bill of Materials for *doc_name*.

    Args:
        doc_name: Document to introspect.
        fmt: ``"json"`` (default) or ``"csv"``.
        include_extras: Include non-dimension properties in JSON
            output. Always False for CSV.
        group_by_type: When True, deduplicate identical entries and
            increment ``quantity`` instead. When False, list every
            object verbatim.

    Returns:
        ``{"success": True, "format": "json"|"csv", "data": ...,
        "entry_count": N, "unique_count": M}`` or
        ``{"success": False, "reason": ...}``.
    """
    _require_freecad()
    doc = FreeCAD.getDocument(doc_name)
    if doc is None:
        return {"success": False, "reason": f"document {doc_name!r} not found"}

    entries: list[dict[str, Any]] = []
    try:
        objs = list(doc.Objects)
    except Exception as e:
        return {"success": False, "reason": f"cannot list objects: {e}"}

    for obj in objs:
        try:
            entries.append(_object_bom_entry(obj))
        except Exception as e:
            entries.append(
                {
                    "name": getattr(obj, "Name", ""),
                    "label": getattr(obj, "Label", ""),
                    "type": getattr(obj, "TypeId", ""),
                    "quantity": 1,
                    "dimensions": {},
                    "extra": {"_error": f"{type(e).__name__}: {e}"},
                }
            )

    if group_by_type:
        grouped: dict[tuple, dict[str, Any]] = {}
        for e in entries:
            # Key by type + dimensions + label (label varies per
            # instance, so we collapse on type+dims to count
            # identical parts).
            key = (
                e["type"],
                json.dumps(e["dimensions"], sort_keys=True, default=str),
            )
            if key in grouped:
                grouped[key]["quantity"] += e["quantity"]
                if include_extras:
                    # Merge extras under a list so we don't lose info.
                    if "_merged_extras" not in grouped[key]:
                        grouped[key]["_merged_extras"] = []
                    grouped[key]["_merged_extras"].append(e["extra"])
            else:
                grouped[key] = dict(e)
        entries = list(grouped.values())

    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        # Header: type, label, name, qty, then the dim columns
        # that appear in at least one row.
        all_dims: list[str] = []
        for e in entries:
            for k in e["dimensions"]:
                if k not in all_dims:
                    all_dims.append(k)
        writer.writerow(["type", "label", "name", "quantity", *all_dims])
        for e in entries:
            row = [
                e["type"],
                e["label"],
                e["name"],
                e["quantity"],
                *[e["dimensions"].get(k, "") for k in all_dims],
            ]
            writer.writerow(row)
        data = buf.getvalue()
    else:
        payload = {
            "doc_name": doc_name,
            "entry_count": len(entries),
            "entries": entries,
        }
        if include_extras:
            payload["include_extras"] = True
        data = json.dumps(payload, ensure_ascii=False, indent=2, default=str)

    unique = len({(e["type"], json.dumps(e["dimensions"], sort_keys=True, default=str)) for e in entries})

    return {
        "success": True,
        "format": fmt,
        "doc_name": doc_name,
        "entry_count": len(entries),
        "unique_count": unique,
        "data": data,
    }


__all__ = ["bom_export", "_object_bom_entry"]
