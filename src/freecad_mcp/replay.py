"""Deterministic session replay for FreeCAD MCP.

Records every tool call (name + args + truncated result summary +
timestamp) and replays it later against a ``FreeCADConnection``.

Why this exists
---------------
1. Post-mortem debugging — when an LLM-driven session produces
   surprising geometry, the operator wants to know exactly which
   sequence of tool calls produced it.

2. CI smoke tests of LLM behaviour — an integration test can record a
   real session, then replay it inside an ephemeral FreeCAD container
   to verify the deployment still produces the expected outcome.
   ``dry_run=True`` is the safe default.

Design contract
---------------
* Recording is append-only and fsync'd per step.
* The recorder is thread-safe — the MCP tool dispatcher hands requests
  to the underlying RPC client from many worker threads.
* ``args`` are stored as ``dict[str, Any]`` (JSON-serialisable).
  ``result_summary`` is a string truncated to 200 chars.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from .freecad_client import FreeCADConnection

logger = logging.getLogger("FreeCADMCPserver")

MAX_RESULT_SUMMARY = 200

_DESTRUCTIVE_TOOLS = frozenset({
    "delete_object",
    "execute_code",
    "export_object",
    "run_fem_analysis",
    "save_document",
})


def default_replay_dir() -> Path:
    """Return the on-disk directory used to store replays."""
    override = os.environ.get("FREECAD_MCP_REPLAY_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if xdg:
        base = Path(xdg)
    elif os.name == "nt":
        appdata = os.environ.get("APPDATA", "").strip()
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    else:
        base = Path.home() / ".config"
    return base / "FreeCAD" / "mcp-freecad" / "replays"


def replay_path(session_id: str) -> Path:
    if not session_id or "/" in session_id or "\\" in session_id or ".." in session_id:
        raise ValueError(f"invalid session_id: {session_id!r}")
    return default_replay_dir() / f"{session_id}.json"


@dataclass
class ReplayStep:
    """One captured tool call."""

    tool_name: str
    args: dict[str, Any]
    timestamp: float
    result_summary: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ReplayStep:
        return cls(
            tool_name=str(payload.get("tool_name", "")),
            args=dict(payload.get("args", {}) or {}),
            timestamp=float(payload.get("timestamp", 0.0)),
            result_summary=str(payload.get("result_summary", "")),
        )


@dataclass
class ReplayResult:
    """Outcome of replaying one step."""

    step: ReplayStep
    status: Literal["ok", "skipped", "failed", "dry-run"]
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_name": self.step.tool_name,
            "timestamp": self.step.timestamp,
            "status": self.status,
            "error": self.error,
            "result_summary": self.step.result_summary,
        }


@dataclass
class SessionRecorder:
    """Thread-safe recorder for one MCP session."""

    session_id: str
    path: Path
    steps: list[ReplayStep] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @classmethod
    def new(cls) -> SessionRecorder:
        sid = uuid.uuid4().hex
        return cls(session_id=sid, path=replay_path(sid))

    def record(self, tool_name: str, args: dict[str, Any], result: Any) -> ReplayStep:
        step = ReplayStep(
            tool_name=str(tool_name),
            args=dict(args or {}),
            timestamp=time.time(),
            result_summary=_summarize(result),
        )
        with self._lock:
            self.steps.append(step)
            self._persist_locked()
        return step

    def _persist_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "session_id": self.session_id,
            "steps": [s.to_dict() for s in self.steps],
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except Exception as e:
            logger.warning("failed to persist replay %s: %s", self.session_id, e)
            with suppress(OSError):
                tmp.unlink()

    def export_json(self) -> str:
        payload = {
            "version": 1,
            "session_id": self.session_id,
            "steps": [s.to_dict() for s in self.steps],
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def export_markdown(self) -> str:
        lines: list[str] = [f"# Session replay — `{self.session_id}`", ""]
        lines.append(f"- Steps: **{len(self.steps)}**")
        if self.steps:
            first, last = self.steps[0].timestamp, self.steps[-1].timestamp
            lines.append(
                f"- Duration: **{last - first:.2f}s** "
                f"({_iso(first)} → {_iso(last)})"
            )
        lines.append("")
        for idx, step in enumerate(self.steps, start=1):
            lines.append(f"### Step {idx} — `{step.tool_name}`")
            lines.append("")
            lines.append(f"- Timestamp: `{_iso(step.timestamp)}`")
            lines.append("- Arguments:")
            lines.append("")
            lines.append("```json")
            lines.append(
                json.dumps(step.args, ensure_ascii=False, indent=2, default=str)
            )
            lines.append("```")
            lines.append("- Result summary:")
            lines.append("")
            lines.append("```text")
            lines.append(step.result_summary or "(empty)")
            lines.append("```")
            lines.append("")
        return "\n".join(lines)

    @classmethod
    def load(cls, session_id: str) -> SessionRecorder:
        path = replay_path(session_id)
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        rec = cls(session_id=session_id, path=path)
        rec.steps = [ReplayStep.from_dict(s) for s in data.get("steps", []) or []]
        return rec

    def replay(
        self,
        connection: FreeCADConnection,
        dry_run: bool = True,
        *,
        allow_destructive: bool = False,
    ) -> list[ReplayResult]:
        results: list[ReplayResult] = []
        for step in self.steps:
            results.append(
                _replay_step(
                    connection,
                    step,
                    dry_run=dry_run,
                    allow_destructive=allow_destructive,
                )
            )
        return results

    def __len__(self) -> int:
        with self._lock:
            return len(self.steps)


def _summarize(result: Any) -> str:
    if result is None:
        return ""
    if isinstance(result, list):
        parts: list[str] = []
        for item in result:
            text = getattr(item, "text", None)
            if isinstance(text, str):
                parts.append(text)
                continue
            if getattr(item, "type", None) == "image" or hasattr(item, "data"):
                parts.append(
                    f"<image {getattr(item, 'mimeType', 'image/png')}>"
                )
        return _clip("\n".join(parts))
    if isinstance(result, str):
        return _clip(result)
    if isinstance(result, (dict, list, int, float, bool)):
        try:
            return _clip(json.dumps(result, ensure_ascii=False, default=str))
        except Exception:
            return _clip(repr(result))
    return _clip(repr(result))


def _clip(text: str) -> str:
    text = text or ""
    if len(text) > MAX_RESULT_SUMMARY:
        return text[: MAX_RESULT_SUMMARY - 1] + "…"
    return text


def _iso(ts: float) -> str:
    import datetime as _dt
    return _dt.datetime.fromtimestamp(
        ts, tz=_dt.UTC
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


def _replay_step(
    connection: FreeCADConnection,
    step: ReplayStep,
    *,
    dry_run: bool,
    allow_destructive: bool,
) -> ReplayResult:
    tool = step.tool_name
    fn = _lookup_replay_method(connection, tool)
    if fn is None:
        return ReplayResult(
            step=step,
            status="skipped",
            error=f"tool {tool!r} not available on connection",
        )
    if dry_run and tool in _DESTRUCTIVE_TOOLS:
        return ReplayResult(step=step, status="dry-run", error=None)
    if not dry_run and tool in _DESTRUCTIVE_TOOLS and not allow_destructive:
        return ReplayResult(
            step=step,
            status="skipped",
            error=(
                f"refusing destructive tool {tool!r}: "
                f"pass allow_destructive=True to actually replay it"
            ),
        )
    try:
        fn(**step.args)
    except TypeError as e:
        return ReplayResult(step=step, status="failed", error=f"TypeError: {e}")
    except Exception as e:
        return ReplayResult(step=step, status="failed", error=f"{type(e).__name__}: {e}")
    return ReplayResult(step=step, status="ok")


def _lookup_replay_method(connection: FreeCADConnection, tool_name: str):
    """Return the bound method for *tool_name* on *connection*, or None.

    Uses :func:`getattr` with a default so the mapping is evaluated
    lazily — building a dict of ``connection.<attr>`` for every tool
    eagerly would crash if the test stub lacks a method.
    """
    return getattr(connection, tool_name, None)


__all__ = [
    "MAX_RESULT_SUMMARY",
    "ReplayResult",
    "ReplayStep",
    "SessionRecorder",
    "default_replay_dir",
    "replay_path",
]
