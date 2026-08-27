"""F5: Reusable workflows composed of MCP tool calls.

A ``Workflow`` is an ordered list of ``WorkflowStep``s. Each step
names an MCP tool and supplies an ``args_template`` whose values
may reference the output of previous steps via ``{prev.<field>}``
(dotted paths supported: ``{prev.foo.bar}``).

The ``WorkflowRegistry`` ships three built-ins
(``create-box-with-save``, ``safe-execute``, ``duplicate-object``)
and loads custom workflows from
``~/.config/FreeCAD/mcp-freecad/workflows.json`` on construction.

MCP tools exposed (via the standalone ops, ready for ``server.py``):
* ``list_workflows()``  → ``[{name, description, step_count}, ...]``
* ``run_workflow(name, args)`` → per-step results
"""
from __future__ import annotations

import builtins
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .freecad_client import FreeCADConnection
from .guidelines import check_code_conflict
from .responses import text_response
from .tool_policy import ALL_TOOL_NAMES
from .utils import safe_operation

logger = logging.getLogger("FreeCADMCPworkflows")


_PREV_TOKEN_RE = re.compile(
    r"\{prev\.([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_0-9]+)*)\}"
)
_USER_VAR_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass
class WorkflowStep:
    tool: str
    args_template: dict[str, Any] = field(default_factory=dict)
    optional: bool = False


@dataclass
class Workflow:
    name: str
    description: str
    steps: list[WorkflowStep]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "steps": [
                {
                    "tool": s.tool,
                    "args_template": s.args_template,
                    "optional": s.optional,
                }
                for s in self.steps
            ],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Workflow:
        steps_raw = data.get("steps") or []
        steps = [
            WorkflowStep(
                tool=str(s["tool"]),
                args_template=dict(s.get("args_template") or {}),
                optional=bool(s.get("optional", False)),
            )
            for s in steps_raw
        ]
        return cls(
            name=str(data["name"]),
            description=str(data.get("description", "")),
            steps=steps,
        )


def _resolve_path(root: Any, dotted: str) -> Any:
    cur: Any = root
    for part in dotted.split("."):
        if isinstance(cur, dict):
            if part not in cur:
                return f"<unresolved: {dotted}>"
            cur = cur[part]
        elif isinstance(cur, list):
            try:
                idx = int(part)
            except ValueError:
                return f"<unresolved: {dotted}>"
            if idx < 0 or idx >= len(cur):
                return f"<unresolved: {dotted}>"
            cur = cur[idx]
        else:
            return f"<unresolved: {dotted}>"
    return cur


def _substitute_args(template: dict[str, Any], previous: list[Any]) -> dict[str, Any]:
    def walk(node: Any) -> Any:
        if isinstance(node, str):
            def repl(m: re.Match[str]) -> str:
                if not previous:
                    return f"<unresolved: {m.group(1)}>"
                return str(_resolve_path(previous[-1], m.group(1)))

            return _PREV_TOKEN_RE.sub(repl, node)
        if isinstance(node, dict):
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    result = walk(template)
    assert isinstance(result, dict)
    return result


def _fill_user_vars(node: Any, ctx: dict[str, Any]) -> Any:
    if isinstance(node, str):
        def repl(m: re.Match[str]) -> str:
            key = m.group(1)
            if key == "prev":
                return m.group(0)
            if key not in ctx:
                return f"<unresolved: {key}>"
            return str(ctx[key])

        return _USER_VAR_RE.sub(repl, node)
    if isinstance(node, dict):
        return {k: _fill_user_vars(v, ctx) for k, v in node.items()}
    if isinstance(node, list):
        return [_fill_user_vars(v, ctx) for v in node]
    return node


def _resolve_step_args(
    template: dict[str, Any],
    ctx: dict[str, Any],
    previous: list[Any],
) -> dict[str, Any]:
    filled = _fill_user_vars(template, ctx)
    return _substitute_args(filled, previous)


def _builtin_workflows() -> dict[str, Workflow]:
    return {
        "create-box-with-save": Workflow(
            name="create-box-with-save",
            description=(
                "Create a fresh document, add a 10x10x10 Part::Box, and save "
                "it. Demonstrates forward-chaining via {prev.document_name}."
            ),
            steps=[
                WorkflowStep(tool="create_document", args_template={"name": "{doc_name}"}),
                WorkflowStep(
                    tool="create_object",
                    args_template={
                        "doc_name": "{prev.document_name}",
                        "obj_data": {
                            "Name": "{box_name}",
                            "Type": "Part::Box",
                            "Properties": {
                                "Length": 10.0,
                                "Width": 10.0,
                                "Height": 10.0,
                            },
                        },
                    },
                ),
                WorkflowStep(
                    tool="save_document",
                    args_template={"doc_name": "{prev.document_name}"},
                    optional=True,
                ),
            ],
        ),
        "safe-execute": Workflow(
            name="safe-execute",
            description=(
                "Run execute_code with a guidelines pre-check and one retry "
                "on transient RPC errors. {code} is the snippet to execute."
            ),
            steps=[
                WorkflowStep(tool="execute_code", args_template={"code": "{code}"})
            ],
        ),
        "duplicate-object": Workflow(
            name="duplicate-object",
            description=(
                "Read an existing object's properties, then create a new "
                "object of the same type with the same properties under "
                "a new name."
            ),
            steps=[
                WorkflowStep(
                    tool="get_object",
                    args_template={
                        "doc_name": "{doc_name}",
                        "obj_name": "{src_obj_name}",
                    },
                ),
                WorkflowStep(
                    tool="create_object",
                    args_template={
                        "doc_name": "{doc_name}",
                        "obj_data": {
                            "Name": "{dst_obj_name}",
                            "Type": "{prev.Type}",
                            "Properties": "{prev.Properties}",
                        },
                    },
                ),
            ],
        ),
    }


class WorkflowError(RuntimeError):
    """Raised when a workflow name is unknown or invalid."""


class WorkflowRegistry:
    DEFAULT_CONFIG_PATH = (
        Path.home() / ".config" / "FreeCAD" / "mcp-freecad" / "workflows.json"
    )

    def __init__(self, config_path: Path | None = None) -> None:
        self._path = config_path or self.DEFAULT_CONFIG_PATH
        self.workflows: dict[str, Workflow] = {}
        for name, wf in _builtin_workflows().items():
            self.workflows[name] = wf
        self._load_custom()

    def _load_custom(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(
                "could not parse workflows.json (%s): %s", self._path, e
            )
            return
        if not isinstance(raw, list):
            return
        for entry in raw:
            try:
                wf = Workflow.from_dict(entry)
                _validate_workflow(wf)
                self.workflows[wf.name] = wf
            except Exception as e:
                logger.warning("skipping invalid workflow entry: %s", e)

    def _save_custom(self) -> None:
        builtin_names = set(_builtin_workflows().keys())
        custom = [
            wf.to_dict()
            for name, wf in self.workflows.items()
            if name not in builtin_names
        ]
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(custom, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning("could not persist workflows.json: %s", e)

    def register(self, wf: Workflow, *, persistent: bool = True) -> None:
        _validate_workflow(wf)
        self.workflows[wf.name] = wf
        if persistent:
            self._save_custom()

    def unregister(self, name: str, *, persistent: bool = True) -> bool:
        if name in _builtin_workflows():
            raise WorkflowError(f"Cannot remove built-in workflow '{name}'.")
        if name not in self.workflows:
            return False
        del self.workflows[name]
        if persistent:
            self._save_custom()
        return True

    def get(self, name: str) -> Workflow | None:
        return self.workflows.get(name)

    def list(self) -> builtins.list[str]:
        # ``sorted(...)`` returns ``list[str]``; explicit cast helps mypy
        # resolve the method-vs-type ambiguity on the name ``list``.
        result: builtins.list[str] = sorted(self.workflows.keys())
        return result

    def list_detailed(self) -> builtins.list[dict[str, Any]]:
        return [
            {
                "name": wf.name,
                "description": wf.description,
                "step_count": len(wf.steps),
            }
            for wf in (self.workflows[name] for name in sorted(self.workflows))
        ]

    def run(
        self,
        name: str,
        connection: FreeCADConnection,
        initial_args: dict[str, Any] | None = None,
        *,
        max_retries: int = 1,
    ) -> builtins.list[dict[str, Any]]:
        wf = self.workflows.get(name)
        if wf is None:
            raise WorkflowError(f"Unknown workflow '{name}'.")
        user_args = dict(initial_args or {})
        results: list[dict[str, Any]] = []
        previous: list[Any] = []
        ctx: dict[str, Any] = dict(user_args)

        for idx, step in enumerate(wf.steps):
            attempt = 0
            last_err: Exception | None = None
            resolved: dict[str, Any] = {}
            while attempt <= max_retries:
                resolved = _resolve_step_args(step.args_template, ctx, previous)
                if step.tool == "execute_code":
                    code_val = resolved.get("code", "")
                    if isinstance(code_val, str):
                        conflict, msg = check_code_conflict(code_val)
                        if conflict:
                            entry = {
                                "step": idx,
                                "tool": step.tool,
                                "args": resolved,
                                "result": {"blocked": True, "reason": msg},
                                "success": False,
                                "skipped": True,
                                "duration_ms": 0.0,
                                "attempts": attempt + 1,
                            }
                            results.append(entry)
                            previous.append({"blocked": True, "reason": msg})
                            if not step.optional:
                                raise WorkflowError(
                                    f"Step {idx} ({step.tool}) blocked by "
                                    f"guidelines: {msg}"
                                )
                            last_err = None
                            break
                t0 = time.monotonic()
                try:
                    result = _invoke_tool(connection, step.tool, resolved)
                except Exception as e:
                    last_err = e
                    logger.warning(
                        "workflow %s step %d attempt %d raised: %s",
                        name, idx, attempt + 1, e,
                    )
                    attempt += 1
                    if attempt > max_retries:
                        break
                    continue
                dt = (time.monotonic() - t0) * 1000.0
                success = _result_success(result)
                entry = {
                    "step": idx,
                    "tool": step.tool,
                    "args": resolved,
                    "result": result,
                    "success": success,
                    "skipped": False,
                    "duration_ms": round(dt, 2),
                    "attempts": attempt + 1,
                }
                results.append(entry)
                previous.append(result)
                last_err = None
                break
            if last_err is not None:
                entry = {
                    "step": idx,
                    "tool": step.tool,
                    "args": resolved,
                    "result": {"error": str(last_err)},
                    "success": False,
                    "skipped": step.optional,
                    "duration_ms": 0.0,
                    "attempts": attempt,
                }
                results.append(entry)
                previous.append({"error": str(last_err)})
                if not step.optional:
                    raise WorkflowError(
                        f"Step {idx} ({step.tool}) failed after "
                        f"{attempt} attempt(s): {last_err}"
                    )
        return results


def _validate_workflow(wf: Workflow) -> None:
    if not wf.name or not isinstance(wf.name, str):
        raise WorkflowError("workflow.name must be a non-empty string")
    if not wf.steps:
        raise WorkflowError(f"workflow '{wf.name}' has no steps")
    unknown = [s.tool for s in wf.steps if s.tool not in ALL_TOOL_NAMES]
    if unknown:
        raise WorkflowError(
            f"workflow '{wf.name}' references unknown tools: {unknown}. "
            f"Known: {sorted(ALL_TOOL_NAMES)}"
        )


def _result_success(result: Any) -> bool:
    if isinstance(result, dict):
        if "success" in result:
            return bool(result["success"])
        if "error" in result:
            return False
    return True


def _invoke_tool(
    connection: FreeCADConnection, tool: str, args: dict[str, Any]
) -> Any:
    method = getattr(connection, tool, None)
    if method is None or not callable(method):
        raise WorkflowError(f"FreeCADConnection has no method '{tool}'.")
    return method(**args)


_DEFAULT_REGISTRY: WorkflowRegistry | None = None


def get_default_registry() -> WorkflowRegistry:
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = WorkflowRegistry()
    return _DEFAULT_REGISTRY


def reset_default_registry(reg: WorkflowRegistry | None = None) -> None:
    """Replace the singleton (used by tests)."""
    global _DEFAULT_REGISTRY
    _DEFAULT_REGISTRY = reg


@safe_operation
def list_workflows_operation() -> list:
    """MCP tool: return [{name, description, step_count}, ...]."""
    reg = get_default_registry()
    return text_response(
        json.dumps(reg.list_detailed(), ensure_ascii=False, indent=2)
    )


@safe_operation
def run_workflow_operation(
    connection: FreeCADConnection,
    name: str,
    args: dict[str, Any] | None = None,
) -> list:
    """MCP tool: execute a registered workflow."""
    reg = get_default_registry()
    wf = reg.get(name)
    if wf is None:
        return text_response(
            f"Unknown workflow '{name}'. Available: {reg.list()}"
        )
    for s in wf.steps:
        if s.tool == "execute_code":
            code_val = (args or {}).get("code", "")
            if isinstance(code_val, str):
                conflict, msg = check_code_conflict(code_val)
                if conflict:
                    return text_response(
                        f"Workflow blocked by guidelines: {msg}"
                    )
    results = reg.run(name, connection, args)
    return text_response(
        json.dumps(
            {"workflow": name, "steps": results},
            ensure_ascii=False, indent=2, default=str,
        )
    )


__all__ = [
    "Workflow",
    "WorkflowStep",
    "WorkflowRegistry",
    "WorkflowError",
    "get_default_registry",
    "reset_default_registry",
    "list_workflows_operation",
    "run_workflow_operation",
]
