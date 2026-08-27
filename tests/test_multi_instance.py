"""Tests for the multi_instance module (v1.1.2).

These tests use a temporary ``XDG_CACHE_HOME`` so the real
discovery directory is never touched.
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest


@pytest.fixture
def mi_mod(monkeypatch, tmp_path):
    """Reload the multi_instance module with an isolated cache dir."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    # Drop any previously-cached module.
    sys.modules.pop("addon.FreeCADMCP.rpc_server.multi_instance", None)
    sys.modules.pop("multi_instance", None)
    spec = importlib.util.spec_from_file_location(
        "_multi_instance_for_test",
        Path(__file__).resolve().parent.parent / "addon/FreeCADMCP/rpc_server/multi_instance.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_multi_instance_for_test"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def test_discovery_dir_creates_path(mi_mod, tmp_path) -> None:
    d = mi_mod.discovery_dir()
    assert d.exists()
    assert d == tmp_path / "freecad-mcp" / "instances"


def test_register_and_list_instance(mi_mod) -> None:
    info = mi_mod.register_instance(label="test", port=9875)
    assert info.label == "test"
    assert info.port == 9875
    items = mi_mod.list_instances()
    assert len(items) == 1
    assert items[0].uuid == info.uuid


def test_get_instance_roundtrip(mi_mod) -> None:
    info = mi_mod.register_instance(label="rt", port=1234)
    got = mi_mod.get_instance(info.uuid)
    assert got is not None
    assert got.port == 1234
    assert got.label == "rt"


def test_unregister_instance(mi_mod) -> None:
    info = mi_mod.register_instance(label="del", port=1234)
    assert mi_mod.unregister_instance(info.uuid) is True
    assert mi_mod.get_instance(info.uuid) is None
    # Second call returns False (idempotent).
    assert mi_mod.unregister_instance(info.uuid) is False


def test_list_prunes_stale(mi_mod) -> None:
    # Register an instance then age it manually.
    info = mi_mod.register_instance(label="old", port=9999)
    p = mi_mod._info_path(info.uuid)
    data = json.loads(p.read_text())
    data["started_at"] -= 10 ** 9  # 30+ years
    p.write_text(json.dumps(data))
    items = mi_mod.list_instances(max_age_seconds=1.0)
    assert all(i.uuid != info.uuid for i in items)


def test_list_corrupt_file_is_removed(mi_mod) -> None:
    # Write garbage; list should ignore it.
    mi_mod.discovery_dir().joinpath("garbage.json").write_text("not-json")
    items = mi_mod.list_instances()
    assert items == []


def test_set_get_clear_active(mi_mod) -> None:
    assert mi_mod.get_active() is None
    mi_mod.set_active("abc-123")
    assert mi_mod.get_active() == "abc-123"
    mi_mod.clear_active()
    assert mi_mod.get_active() is None


def test_select_instance_not_found(mi_mod) -> None:
    res = mi_mod.select_instance("does-not-exist")
    assert res.get("ok") is False
    assert res.get("uuid") == "does-not-exist"
    assert res.get("reason") == "not found"


def test_select_instance_probe(monkeypatch, mi_mod) -> None:
    info = mi_mod.register_instance(label="probe", port=19999)
    # Patch the probe so we don't actually open a socket.
    monkeypatch.setattr(mi_mod, "_probe_tcp", lambda h, p, **kw: (True, 1.23))
    res = mi_mod.select_instance(info.uuid)
    assert res["ok"] is True
    assert res["info"]["port"] == 19999
    assert res["info"]["latency_ms"] == 1.23


def test_instance_info_serialization() -> None:
    from addon.FreeCADMCP.rpc_server.multi_instance import InstanceInfo  # type: ignore

    info = InstanceInfo(
        uuid="u", label="l", pid=1, host="h", port=2,
        started_at=3.0,
    )
    d = info.to_dict()
    assert d["uuid"] == "u"
    rt = InstanceInfo.from_dict(d)
    assert rt.uuid == "u"
    # Unknown fields are dropped.
    rt2 = InstanceInfo.from_dict({**d, "unknown": 42})
    assert rt2.uuid == "u"


def test_register_uses_socket_hostname(mi_mod) -> None:
    info = mi_mod.register_instance(port=9876)
    # Label is auto-generated when not provided.
    assert "FreeCAD" in info.label
    assert info.port == 9876
