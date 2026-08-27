"""Tests for src/freecad_mcp/workflows.py."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

import freecad_mcp.workflows as wf
from freecad_mcp.workflows import (
    Workflow,
    WorkflowError,
    WorkflowRegistry,
    WorkflowStep,
)


class _FakeConnection:
    def __init__(self) -> None:
        self.calls: list = []
        self.fail_on: set = set()
        self.fail_n_times: dict = {}

    def _maybe_fail(self, tool: str):
        if tool in self.fail_n_times and self.fail_n_times[tool] > 0:
            self.fail_n_times[tool] -= 1
            raise ConnectionError(f"flaky {tool}")
        if tool in self.fail_on:
            return {"success": False, "error": f"{tool} failed"}
        return None

    def create_document(self, name, request_id=None):
        self.calls.append(("create_document", {"name": name}))
        err = self._maybe_fail("create_document")
        if err:
            return err
        return {"success": True, "document_name": name}

    def create_object(self, doc_name, obj_data=None, request_id=None):
        self.calls.append(("create_object", {"doc_name": doc_name, "obj_data": obj_data}))
        err = self._maybe_fail("create_object")
        if err:
            return err
        if obj_data is None:
            obj_data = {}
        return {"success": True, "object_name": obj_data.get("Name", "obj")}

    def get_object(self, doc_name, obj_name):
        self.calls.append(("get_object", {"doc_name": doc_name, "obj_name": obj_name}))
        err = self._maybe_fail("get_object")
        if err:
            return err
        return {"success": True, "Type": "Part::Box", "Properties": {"Length": 10}}

    def execute_code(self, code, request_id=None):
        self.calls.append(("execute_code", {"code": code}))
        err = self._maybe_fail("execute_code")
        if err:
            return err
        return {"success": True, "message": "ok"}

    def save_document(self, doc_name, path=None, request_id=None):
        self.calls.append(("save_document", {"doc_name": doc_name}))
        err = self._maybe_fail("save_document")
        if err:
            return err
        return {"success": True, "path": path or f"/tmp/{doc_name}.FCStd"}


def test_registry_register_and_get(tmp_path) -> None:
    reg = WorkflowRegistry(config_path=tmp_path / "wf.json")
    custom = Workflow(
        name="custom-flow",
        description="x",
        steps=[WorkflowStep(tool="list_documents", args_template={})],
    )
    reg.register(custom, persistent=False)
    assert reg.get("custom-flow") is custom
    assert "custom-flow" in reg.list()


def test_registry_persists_to_disk(tmp_path) -> None:
    path = tmp_path / "wf.json"
    reg = WorkflowRegistry(config_path=path)
    reg.register(
        Workflow(
            name="persisted",
            description="d",
            steps=[WorkflowStep(tool="list_documents", args_template={})],
        ),
    )
    assert path.exists()
    reg2 = WorkflowRegistry(config_path=path)
    assert reg2.get("persisted") is not None


def test_registry_rejects_unknown_tool(tmp_path) -> None:
    reg = WorkflowRegistry(config_path=tmp_path / "wf.json")
    with pytest.raises(WorkflowError):
        reg.register(
            Workflow(
                name="bad",
                description="x",
                steps=[WorkflowStep(tool="not_a_real_tool", args_template={})],
            ),
            persistent=False,
        )


def test_registry_unregister_blocks_builtin(tmp_path) -> None:
    reg = WorkflowRegistry(config_path=tmp_path / "wf.json")
    with pytest.raises(WorkflowError):
        reg.unregister("safe-execute")


def test_registry_unregister_missing_returns_false(tmp_path) -> None:
    reg = WorkflowRegistry(config_path=tmp_path / "wf.json")
    assert reg.unregister("never-existed") is False


def test_template_substitution_simple(tmp_path) -> None:
    reg = WorkflowRegistry(config_path=tmp_path / "wf.json")
    conn = _FakeConnection()
    results = reg.run(
        "safe-execute",
        conn,
        {"code": "FreeCAD.newDocument('X')"},
    )
    assert results[0]["tool"] == "execute_code"
    assert results[0]["success"]


def test_template_substitution_dotted_path(tmp_path) -> None:
    reg = WorkflowRegistry(config_path=tmp_path / "wf.json")
    conn = _FakeConnection()
    results = reg.run(
        "duplicate-object",
        conn,
        {"doc_name": "DocA", "src_obj_name": "Box", "dst_obj_name": "Box2"},
    )
    assert results[0]["tool"] == "get_object"
    assert results[1]["tool"] == "create_object"
    args = results[1]["args"]
    assert args["obj_data"]["Type"] == "Part::Box"
    assert args["obj_data"]["Name"] == "Box2"


def test_template_substitution_missing_path_returns_unresolved() -> None:
    # Use the internal helper directly.
    from freecad_mcp.workflows import _resolve_step_args

    out = _resolve_step_args({"x": "{prev.missing}"}, {}, [{"foo": 1}])
    assert out["x"] == "<unresolved: missing>"


def test_template_user_vars_then_prev() -> None:
    from freecad_mcp.workflows import _resolve_step_args

    out = _resolve_step_args(
        {"doc": "{doc_name}", "from_prev": "{prev.foo}"},
        {"doc_name": "D"},
        [{"foo": "bar"}],
    )
    assert out["doc"] == "D"
    assert out["from_prev"] == "bar"


def test_run_creates_box(tmp_path) -> None:
    reg = WorkflowRegistry(config_path=tmp_path / "wf.json")
    conn = _FakeConnection()
    results = reg.run(
        "create-box-with-save",
        conn,
        {"doc_name": "MyDoc", "box_name": "MyBox"},
    )
    assert [r["tool"] for r in results] == [
        "create_document",
        "create_object",
        "save_document",
    ]
    assert all(r["success"] for r in results)


def test_optional_step_skipped_on_failure(tmp_path) -> None:
    reg = WorkflowRegistry(config_path=tmp_path / "wf.json")
    conn = _FakeConnection()
    conn.fail_on.add("save_document")
    results = reg.run(
        "create-box-with-save",
        conn,
        {"doc_name": "D", "box_name": "B"},
    )
    last = results[-1]
    assert last["tool"] == "save_document"
    # ``skipped`` is True only when the call raised. ``fail_on`` causes the
    # stub to *return* ``{"success": False, ...}`` — that goes through the
    # happy path and is recorded as ``skipped=False``. The exception variant
    # is covered by test_recorder_replay_failure in test_replay.py.
    assert last["success"] is False


def test_safe_execute_blocks_conflicting_code(tmp_path) -> None:
    reg = WorkflowRegistry(config_path=tmp_path / "wf.json")
    conn = _FakeConnection()
    # Simulate a known-conflict pattern via the guidelines helper.
    from freecad_mcp.guidelines import check_code_conflict

    conflict, msg = check_code_conflict("os.system('rm -rf /')")
    if not conflict:
        pytest.skip("guidelines rule not flagged by this version")
    with pytest.raises(WorkflowError):
        reg.run("safe-execute", conn, {"code": "os.system('rm -rf /')"})


def test_workflow_from_dict_round_trip(tmp_path) -> None:
    wf_dict = {
        "name": "test",
        "description": "x",
        "steps": [
            {"tool": "list_documents", "args_template": {}, "optional": False},
        ],
    }
    parsed = Workflow.from_dict(wf_dict)
    assert parsed.name == "test"
    assert len(parsed.steps) == 1
    roundtripped = parsed.to_dict()
    assert roundtripped == wf_dict


def test_list_workflows_operation_returns_json() -> None:
    out = wf.list_workflows_operation()
    assert out
    text = out[0].text if hasattr(out[0], "text") else str(out[0])
    parsed = json.loads(text)
    assert isinstance(parsed, list)
    assert any(p["name"] == "safe-execute" for p in parsed)


def test_run_workflow_operation_unknown_returns_message() -> None:
    out = wf.run_workflow_operation(_FakeConnection(), "does-not-exist", {})
    text = out[0].text if hasattr(out[0], "text") else str(out[0])
    assert "Unknown workflow" in text


def test_run_workflow_operation_runs(tmp_path) -> None:
    wf.reset_default_registry(WorkflowRegistry(config_path=tmp_path / "wf.json"))
    out = wf.run_workflow_operation(
        _FakeConnection(),
        "safe-execute",
        {"code": "FreeCAD.newDocument('X')"},
    )
    text = out[0].text if hasattr(out[0], "text") else str(out[0])
    parsed = json.loads(text)
    assert parsed["workflow"] == "safe-execute"


def test_validate_workflow_rejects_empty_name(tmp_path) -> None:
    from freecad_mcp.workflows import _validate_workflow
    with pytest.raises(WorkflowError):
        _validate_workflow(Workflow(name="", description="x", steps=[]))


def test_validate_workflow_rejects_no_steps(tmp_path) -> None:
    from freecad_mcp.workflows import _validate_workflow
    with pytest.raises(WorkflowError):
        _validate_workflow(Workflow(name="x", description="x", steps=[]))
