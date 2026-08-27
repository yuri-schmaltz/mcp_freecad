"""Visual / structural diff between two FreeCAD documents.

The MCP layer exposes two document-list primitives (``list_documents``
returns names; ``get_objects`` returns the serialised property tree of
every object). This module consumes those primitives and produces a
structured diff that an LLM (or a human operator) can summarise.

Diff categories
---------------
For each ``ObjectDiff`` the object is classified as one of:

* ``added``      — present in ``doc_b`` only;
* ``removed``    — present in ``doc_a`` only;
* ``modified``   — present in both, but with at least one property
                    whose value differs;
* ``unchanged``  — present in both with identical serialised values.

For ``modified`` objects we also record the per-property diff:
``properties_added``/``properties_removed`` and
``properties_modified`` (mapping name → `` ``( old_value, new_value )``).

The comparison uses ``json.dumps(..., sort_keys=True, default=str)``
on each property value so vectors, placements and nested dicts diff
deterministically regardless of dict ordering.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal

from .freecad_client import FreeCADConnection

Status = Literal["added", "removed", "modified", "unchanged"]


def _serialise_property(value: Any) -> str:
    """Canonical JSON representation used for equality checks."""
    try:
        return json.dumps(value, sort_keys=True, default=str)
    except Exception:  # noqa: BLE001
        return repr(value)


@dataclass
class ObjectDiff:
    """Per-object diff result."""

    name: str
    status: Status
    properties_added: dict[str, Any] = field(default_factory=dict)
    properties_removed: dict[str, Any] = field(default_factory=dict)
    properties_modified: dict[str, tuple[Any, Any]] = field(default_factory=dict)


@dataclass
class DocumentDiff:
    """Diff between two documents."""

    doc_a: str
    doc_b: str
    objects_added: list[str] = field(default_factory=list)
    objects_removed: list[str] = field(default_factory=list)
    objects_modified: list[ObjectDiff] = field(default_factory=list)
    objects_unchanged: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"### Document diff: `{self.doc_a}` ↔ `{self.doc_b}`",
            "",
            f"- **Added:** {len(self.objects_added)} "
            f"({', '.join(self.objects_added) or '—'})",
            f"- **Removed:** {len(self.objects_removed)} "
            f"({', '.join(self.objects_removed) or '—'})",
            f"- **Modified:** {len(self.objects_modified)} "
            f"({', '.join(o.name for o in self.objects_modified) or '—'})",
            f"- **Unchanged:** {len(self.objects_unchanged)}",
        ]
        return "\n".join(lines)

    def detailed(self) -> str:
        lines = [self.summary(), ""]
        if self.objects_added:
            lines.append("#### Added objects")
            for name in self.objects_added:
                lines.append(f"- `{name}`")
            lines.append("")
        if self.objects_removed:
            lines.append("#### Removed objects")
            for name in self.objects_removed:
                lines.append(f"- `{name}`")
            lines.append("")
        if self.objects_modified:
            lines.append("#### Modified objects")
            for obj in self.objects_modified:
                lines.append(f"##### `{obj.name}`")
                if obj.properties_added:
                    lines.append("- Added properties:")
                    for k, v in obj.properties_added.items():
                        lines.append(f"  - `{k}` = `{v!r}`")
                if obj.properties_removed:
                    lines.append("- Removed properties:")
                    for k, v in obj.properties_removed.items():
                        lines.append(f"  - `{k}` = `{v!r}`")
                if obj.properties_modified:
                    lines.append("- Changed properties:")
                    for k, (old, new) in obj.properties_modified.items():
                        lines.append(f"  - `{k}`: `{old!r}` → `{new!r}`")
                lines.append("")
        if not (self.objects_added or self.objects_removed or self.objects_modified):
            lines.append("_Documents are structurally identical._")
        return "\n".join(lines)

    def as_dict(self, detailed: bool = False) -> dict[str, Any]:
        d: dict[str, Any] = {
            "doc_a": self.doc_a,
            "doc_b": self.doc_b,
            "counts": {
                "added": len(self.objects_added),
                "removed": len(self.objects_removed),
                "modified": len(self.objects_modified),
                "unchanged": len(self.objects_unchanged),
            },
            "objects_added": list(self.objects_added),
            "objects_removed": list(self.objects_removed),
            "objects_modified": [
                {
                    "name": o.name,
                    "status": o.status,
                    "properties_added": o.properties_added,
                    "properties_removed": o.properties_removed,
                    "properties_modified": {
                        k: {"old": v[0], "new": v[1]}
                        for k, v in o.properties_modified.items()
                    },
                }
                for o in self.objects_modified
            ],
            "objects_unchanged": list(self.objects_unchanged),
            "summary_markdown": self.summary(),
        }
        if detailed:
            d["detailed_markdown"] = self.detailed()
        return d


def _extract_property_bag(obj: dict[str, Any]) -> dict[str, Any]:
    props = obj.get("Properties")
    if isinstance(props, dict):
        return props
    return {}


def _diff_property_bags(
    a_props: dict[str, Any],
    b_props: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, tuple[Any, Any]]]:
    a_keys, b_keys = set(a_props), set(b_props)
    added = {k: b_props[k] for k in b_keys - a_keys}
    removed = {k: a_props[k] for k in a_keys - b_keys}
    modified: dict[str, tuple[Any, Any]] = {}
    for k in a_keys & b_keys:
        if _serialise_property(a_props[k]) != _serialise_property(b_props[k]):
            modified[k] = (a_props[k], b_props[k])
    return added, removed, modified


def diff_documents(
    connection: FreeCADConnection,
    doc_a: str,
    doc_b: str,
) -> DocumentDiff:
    """Compute a :class:`DocumentDiff` between *doc_a* and *doc_b*."""
    objects_a_raw = _safe_get_objects(connection, doc_a)
    objects_b_raw = _safe_get_objects(connection, doc_b)
    objects_a = {_object_name(o): o for o in objects_a_raw if _object_name(o)}
    objects_b = {_object_name(o): o for o in objects_b_raw if _object_name(o)}

    diff = DocumentDiff(doc_a=doc_a, doc_b=doc_b)
    diff.objects_added = sorted(set(objects_b) - set(objects_a))
    diff.objects_removed = sorted(set(objects_a) - set(objects_b))

    for name in sorted(set(objects_a) & set(objects_b)):
        a_props = _extract_property_bag(objects_a[name])
        b_props = _extract_property_bag(objects_b[name])
        added, removed, modified = _diff_property_bags(a_props, b_props)
        if added or removed or modified:
            diff.objects_modified.append(
                ObjectDiff(
                    name=name,
                    status="modified",
                    properties_added=added,
                    properties_removed=removed,
                    properties_modified=modified,
                )
            )
        else:
            diff.objects_unchanged.append(name)
    return diff


def _object_name(obj: dict[str, Any]) -> str:
    name = obj.get("Name")
    if isinstance(name, str) and name:
        return name
    label = obj.get("Label")
    if isinstance(label, str) and label:
        return label
    return ""


def _safe_get_objects(
    connection: FreeCADConnection, doc_name: str
) -> list[dict[str, Any]]:
    try:
        result = connection.get_objects(doc_name)
    except Exception:
        return []
    if isinstance(result, list):
        return [o for o in result if isinstance(o, dict)]
    return []


__all__ = [
    "ObjectDiff",
    "DocumentDiff",
    "diff_documents",
    "Status",
]
