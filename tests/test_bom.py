"""Tests for the bom module (v1.1.1)."""
from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

_RPC_DIR = Path(__file__).resolve().parent.parent / "addon/FreeCADMCP/rpc_server"


class _FakeObj:
    """Minimal FreeCAD object stub for BOM tests."""

    def __init__(self, name: str, type_id: str = "Part::Box", **props):
        self.Name = name
        self.Label = name
        self.TypeId = type_id
        self._props = dict(props)

    @property
    def PropertiesList(self):
        return list(self._props.keys())

    def __getattr__(self, key):
        if key in self._props:
            return self._props[key]
        raise AttributeError(key)


class _FakeDoc:
    def __init__(self, objects):
        self.Objects = list(objects)


class _FakeFreeCAD:
    ActiveDocument = None

    def __init__(self):
        self._docs: dict[str, _FakeDoc] = {}

    def add_doc(self, name: str, objs: list[_FakeObj]):
        self._docs[name] = _FakeDoc(objs)

    def getDocument(self, name):
        return self._docs.get(name)


@pytest.fixture
def bom_mod(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "_bom_for_test", _RPC_DIR / "bom.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_bom_for_test"] = mod
    fake_fc = _FakeFreeCAD()
    monkeypatch.setitem(sys.modules, "FreeCAD", fake_fc)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    # After exec, the module already grabbed sys.modules["FreeCAD"]
    # — but if we created that before exec, the reference is already
    # the right one. Patch defensively anyway.
    monkeypatch.setattr(mod, "FreeCAD", fake_fc)
    return mod, fake_fc


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_export_json_basic(bom_mod) -> None:
    mod, fc = bom_mod
    fc.add_doc("Doc1", [
        _FakeObj("Box1", "Part::Box", Length=10.0, Width=5.0, Height=2.0),
        _FakeObj("Box2", "Part::Box", Length=10.0, Width=5.0, Height=2.0),
    ])
    res = mod.bom_export("Doc1", fmt="json")
    assert res["success"] is True
    assert res["format"] == "json"
    assert res["entry_count"] == 1  # grouped_by_type collapses
    assert res["unique_count"] == 1
    payload = json.loads(res["data"])
    assert payload["entry_count"] == 1
    assert payload["entries"][0]["quantity"] == 2


def test_export_csv(bom_mod) -> None:
    mod, fc = bom_mod
    fc.add_doc("Doc1", [
        _FakeObj("Box", "Part::Box", Length=10.0, Width=5.0),
        _FakeObj("Cyl", "Part::Cylinder", Radius=2.0, Height=8.0),
    ])
    res = mod.bom_export("Doc1", fmt="csv")
    assert res["success"] is True
    assert res["format"] == "csv"
    lines = res["data"].strip().splitlines()
    # Header + 2 rows
    assert len(lines) == 3
    assert "type" in lines[0]
    assert "quantity" in lines[0]
    assert "Part::Box" in lines[1]
    assert "Part::Cylinder" in lines[2]


def test_export_unknown_fmt_falls_back_to_json(bom_mod) -> None:
    mod, fc = bom_mod
    fc.add_doc("D", [_FakeObj("X", "Part::Box")])
    res = mod.bom_export("D", fmt="xml")
    # Falls through to json branch because `if fmt == "csv"` is False.
    assert res["success"] is True
    assert res["format"] == "xml"
    payload = json.loads(res["data"])
    assert payload["doc_name"] == "D"


def test_export_includes_extras(bom_mod) -> None:
    mod, fc = bom_mod
    fc.add_doc("D", [
        _FakeObj("X", "Part::Box", Length=1.0, MyExtra="hello", Standard="DIN-933"),
    ])
    res = mod.bom_export("D", include_extras=True)
    payload = json.loads(res["data"])
    assert payload["include_extras"] is True
    entry = payload["entries"][0]
    # Standard is captured as a dimension; MyExtra stays in extras.
    assert "Standard" in entry["dimensions"]
    assert "MyExtra" in entry["extra"]
    assert entry["extra"]["MyExtra"] == "hello"


def test_export_no_grouping(bom_mod) -> None:
    mod, fc = bom_mod
    fc.add_doc("D", [
        _FakeObj("A", "Part::Box", Length=1.0),
        _FakeObj("B", "Part::Box", Length=1.0),
    ])
    res = mod.bom_export("D", group_by_type=False)
    payload = json.loads(res["data"])
    assert payload["entry_count"] == 2
    assert res["unique_count"] == 1  # still 1 unique (same dims)


def test_export_missing_doc(bom_mod) -> None:
    mod, _ = bom_mod
    res = mod.bom_export("Nope")
    assert res["success"] is False
    assert "not found" in res["reason"]


def test_export_quantity_from_count(bom_mod) -> None:
    mod, fc = bom_mod
    fc.add_doc("D", [
        _FakeObj("Bolt", "Part::Feature", Count=12),
    ])
    res = mod.bom_export("D", group_by_type=False)
    payload = json.loads(res["data"])
    assert payload["entries"][0]["quantity"] == 12


def test_export_quantity_invalid_count_falls_back_to_one(bom_mod) -> None:
    mod, fc = bom_mod
    fc.add_doc("D", [
        _FakeObj("X", "Part::Box", Count="not-a-number"),
    ])
    res = mod.bom_export("D", group_by_type=False)
    payload = json.loads(res["data"])
    assert payload["entries"][0]["quantity"] == 1


def test_object_bom_entry_basic(bom_mod) -> None:
    mod, _ = bom_mod
    obj = _FakeObj("X", "Part::Box", Length=10.0, Width=5.0)
    entry = mod._object_bom_entry(obj)
    assert entry["name"] == "X"
    assert entry["label"] == "X"
    assert entry["type"] == "Part::Box"
    assert entry["quantity"] == 1
    assert entry["dimensions"]["Length"] == 10.0


def test_object_bom_entry_handles_missing_props(bom_mod) -> None:
    """An object without PropertiesList still produces a valid entry."""
    mod, _ = bom_mod
    bare = types.SimpleNamespace(Name="X", Label="X", TypeId="Part::Box")
    entry = mod._object_bom_entry(bare)
    assert entry["name"] == "X"
    assert entry["type"] == "Part::Box"


def test_require_freecad_raises_when_none() -> None:
    """With FreeCAD patched to None, the require helper raises."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_bom_for_freecad_test", _RPC_DIR / "bom.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_bom_for_freecad_test"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    mod.FreeCAD = None
    with pytest.raises(RuntimeError, match="FreeCAD"):
        mod._require_freecad()


def test_export_csv_columns_match_dim_keys(bom_mod) -> None:
    mod, fc = bom_mod
    fc.add_doc("D", [
        _FakeObj("X", "Part::Box", Length=1.0, Width=2.0, Height=3.0),
        _FakeObj("Y", "Part::Box", Length=4.0, Width=5.0, Radius=6.0),
    ])
    res = mod.bom_export("D", fmt="csv", group_by_type=False)
    lines = res["data"].strip().splitlines()
    header = lines[0].split(",")
    # All five dim keys must appear somewhere.
    for k in ("Length", "Width", "Height", "Radius"):
        assert k in header


def test_export_groups_identical_dims(bom_mod) -> None:
    mod, fc = bom_mod
    fc.add_doc("D", [
        _FakeObj("A", "Part::Box", Length=10.0, Width=5.0, Height=2.0),
        _FakeObj("B", "Part::Box", Length=10.0, Width=5.0, Height=2.0),
        _FakeObj("C", "Part::Box", Length=20.0, Width=5.0, Height=2.0),
    ])
    res = mod.bom_export("D", group_by_type=True)
    payload = json.loads(res["data"])
    assert payload["entry_count"] == 2
    # Find the entry with quantity 2.
    pair = next(e for e in payload["entries"] if e["quantity"] == 2)
    assert pair["dimensions"]["Length"] == 10.0


def test_export_preserves_unicode_labels(bom_mod) -> None:
    mod, fc = bom_mod
    fc.add_doc("D", [_FakeObj("Çubuk", "Part::Box", Length=1.0)])
    res = mod.bom_export("D", fmt="json")
    assert "Çubuk" in res["data"]
