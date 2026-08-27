"""Tests for src/freecad_mcp/replay.py."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from freecad_mcp.replay import (
    ReplayStep,
    SessionRecorder,
    default_replay_dir,
    replay_path,
)


def test_session_id_filename_safety() -> None:
    with pytest.raises(ValueError):
        replay_path("../etc/passwd")


def test_session_id_slash_rejected() -> None:
    with pytest.raises(ValueError):
        replay_path("foo/bar")


def test_recorder_persists_to_disk(tmp_path, monkeypatch) -> None:
    base = tmp_path / "replays"
    monkeypatch.setenv("FREECAD_MCP_REPLAY_DIR", str(base))
    rec = SessionRecorder.new()
    rec.record("create_document", {"name": "Doc"}, {"success": True})
    rec.record("create_object", {"doc_name": "Doc"}, {"success": True})
    assert len(rec) == 2
    target = base / f"{rec.session_id}.json"
    assert target.exists()
    payload = json.loads(target.read_text())
    assert payload["version"] == 1
    assert len(payload["steps"]) == 2


def test_recorder_export_markdown() -> None:
    rec = SessionRecorder.new()
    rec.record("create_document", {"name": "A"}, {"success": True})
    md = rec.export_markdown()
    assert rec.session_id in md
    assert "create_document" in md
    assert "Step 1" in md


def test_recorder_load_round_trip(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FREECAD_MCP_REPLAY_DIR", str(tmp_path))
    rec = SessionRecorder.new()
    rec.record("create_document", {"name": "X"}, {"success": True})
    rec.record("create_object", {"obj_type": "Part::Box"}, {"object_name": "Box"})
    loaded = SessionRecorder.load(rec.session_id)
    assert [s.tool_name for s in loaded.steps] == ["create_document", "create_object"]
    assert loaded.steps[1].args["obj_type"] == "Part::Box"


def test_recorder_replay_dry_run_skips_destructive(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FREECAD_MCP_REPLAY_DIR", str(tmp_path))
    rec = SessionRecorder.new()
    rec.record("delete_object", {"doc_name": "D", "obj_name": "O"}, {"success": True})
    rec.record("save_document", {"doc_name": "D"}, {"success": True})

    class _Conn:
        deleted: list = []
        saved: list = []

        def delete_object(self, doc_name, obj_name):
            self.deleted.append((doc_name, obj_name))
            return {"success": True}

        def save_document(self, doc_name):
            self.saved.append(doc_name)
            return {"success": True}

    conn = _Conn()
    results = rec.replay(conn, dry_run=True)
    assert conn.deleted == []
    assert conn.saved == []
    statuses = {r.status for r in results}
    assert statuses == {"dry-run"}


def test_recorder_replay_actually_runs_when_allowed(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FREECAD_MCP_REPLAY_DIR", str(tmp_path))
    rec = SessionRecorder.new()
    rec.record("delete_object", {"doc_name": "D", "obj_name": "O"}, {"success": True})

    class _Conn:
        calls: list = []

        def delete_object(self, doc_name, obj_name):
            self.calls.append(("delete_object", doc_name, obj_name))
            return {"success": True}

    conn = _Conn()
    results = rec.replay(conn, dry_run=False, allow_destructive=True)
    assert conn.calls == [("delete_object", "D", "O")]
    assert results[0].status == "ok"


def test_recorder_replay_missing_tool_marked_skipped(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FREECAD_MCP_REPLAY_DIR", str(tmp_path))
    rec = SessionRecorder.new()
    rec.record("totally_made_up_tool", {}, {"success": True})

    class _Conn:
        pass

    results = rec.replay(_Conn(), dry_run=False)
    assert results[0].status == "skipped"
    assert "not available" in (results[0].error or "")


def test_replay_path_default_under_xdg(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("FREECAD_MCP_REPLAY_DIR", raising=False)
    d = default_replay_dir()
    assert d == tmp_path / "FreeCAD" / "mcp-freecad" / "replays"


def test_replay_step_from_dict() -> None:
    payload = {
        "tool_name": "create_document",
        "args": {"name": "X"},
        "timestamp": 123.0,
        "result_summary": "ok",
    }
    step = ReplayStep.from_dict(payload)
    assert step.tool_name == "create_document"
    assert step.args == {"name": "X"}
    assert step.timestamp == 123.0
