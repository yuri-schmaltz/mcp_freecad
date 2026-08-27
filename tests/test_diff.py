"""Tests for src/freecad_mcp/diff.py."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from freecad_mcp.diff import (
    DocumentDiff,
    ObjectDiff,
    diff_documents,
)


class _FakeConn:
    def __init__(self, by_doc: dict) -> None:
        self.by_doc = by_doc

    def get_objects(self, doc_name):
        return self.by_doc.get(doc_name, [])


def test_diff_added_object() -> None:
    conn = _FakeConn({
        "A": [{"Name": "Box", "Properties": {"Length": 10}}],
        "B": [
            {"Name": "Box", "Properties": {"Length": 10}},
            {"Name": "Cyl", "Properties": {"Radius": 5}},
        ],
    })
    d = diff_documents(conn, "A", "B")
    assert d.objects_added == ["Cyl"]
    assert d.objects_removed == []
    assert d.objects_unchanged == ["Box"]


def test_diff_removed_object() -> None:
    conn = _FakeConn({
        "A": [
            {"Name": "Box", "Properties": {"Length": 10}},
            {"Name": "Cyl", "Properties": {"Radius": 5}},
        ],
        "B": [{"Name": "Box", "Properties": {"Length": 10}}],
    })
    d = diff_documents(conn, "A", "B")
    assert d.objects_removed == ["Cyl"]
    assert d.objects_added == []


def test_diff_modified_property() -> None:
    conn = _FakeConn({
        "A": [{"Name": "Box", "Properties": {"Length": 10, "Width": 5}}],
        "B": [{"Name": "Box", "Properties": {"Length": 12, "Width": 5}}],
    })
    d = diff_documents(conn, "A", "B")
    assert len(d.objects_modified) == 1
    mod = d.objects_modified[0]
    assert mod.name == "Box"
    assert "Length" in mod.properties_modified
    old, new = mod.properties_modified["Length"]
    assert old == 10
    assert new == 12


def test_diff_property_added_and_removed() -> None:
    conn = _FakeConn({
        "A": [{"Name": "Box", "Properties": {"Length": 10}}],
        "B": [{"Name": "Box", "Properties": {"Length": 10, "Width": 5}}],
    })
    d = diff_documents(conn, "A", "B")
    mod = d.objects_modified[0]
    assert "Width" in mod.properties_added


def test_diff_uses_label_when_name_missing() -> None:
    conn = _FakeConn({
        "A": [{"Label": "Box", "Properties": {}}],
        "B": [],
    })
    d = diff_documents(conn, "A", "B")
    assert d.objects_removed == ["Box"]


def test_diff_skips_objects_without_name() -> None:
    conn = _FakeConn({
        "A": [{"Properties": {"Length": 10}}],
        "B": [],
    })
    d = diff_documents(conn, "A", "B")
    assert d.objects_removed == []
    assert d.objects_added == []


def test_diff_missing_doc_returns_added() -> None:
    conn = _FakeConn({
        "A": [{"Name": "X", "Properties": {}}],
    })
    d = diff_documents(conn, "A", "missing")
    assert d.objects_removed == ["X"]
    assert d.objects_added == []


def test_diff_summary_markdown() -> None:
    conn = _FakeConn({
        "A": [],
        "B": [{"Name": "X", "Properties": {}}],
    })
    d = diff_documents(conn, "A", "B")
    md = d.summary()
    assert "Document diff" in md
    assert "**Added:** 1" in md


def test_diff_detailed_markdown_includes_changes() -> None:
    conn = _FakeConn({
        "A": [{"Name": "Box", "Properties": {"Length": 10}}],
        "B": [{"Name": "Box", "Properties": {"Length": 12}}],
    })
    d = diff_documents(conn, "A", "B")
    md = d.detailed()
    assert "Modified objects" in md
    assert "Length" in md


def test_diff_as_dict_includes_detailed_when_requested() -> None:
    conn = _FakeConn({
        "A": [{"Name": "X", "Properties": {}}],
        "B": [],
    })
    d = diff_documents(conn, "A", "B")
    short = d.as_dict()
    assert "detailed_markdown" not in short
    full = d.as_dict(detailed=True)
    assert "detailed_markdown" in full


def test_diff_handles_vector_values_via_json() -> None:
    conn = _FakeConn({
        "A": [{"Name": "Box", "Properties": {"Placement": {"x": 1, "y": 2}}}],
        "B": [{"Name": "Box", "Properties": {"Placement": {"y": 2, "x": 1}}}],
    })
    d = diff_documents(conn, "A", "B")
    assert d.objects_unchanged == ["Box"]


def test_object_diff_dataclass_defaults() -> None:
    od = ObjectDiff(name="x", status="modified")
    assert od.properties_added == {}
    assert od.properties_removed == {}
    assert od.properties_modified == {}


def test_document_diff_dataclass_defaults() -> None:
    dd = DocumentDiff(doc_a="A", doc_b="B")
    assert dd.objects_added == []
    assert dd.objects_removed == []
