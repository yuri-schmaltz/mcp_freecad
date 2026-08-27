"""Async job runner — long-running FreeCAD operations off the main thread.

The standard RPC path is **synchronous**: each call blocks the MCP
client until the FreeCAD GUI thread finishes its work. Long ops
(FEM with 100k nodes, mesh boolean on 1M-triangle meshes, CAM
toolpath on 200-feature jobs) can take minutes. Holding the MCP
client hostage that long is unacceptable.

This module provides a tiny background-job system:

* ``submit(code, label)`` → returns ``job_id`` immediately.
* ``poll(job_id)`` → returns status, elapsed, result/error/None.
* ``list_jobs()`` → all known jobs (in-memory + on-disk).
* ``cancel(job_id)`` → marks as ``cancelled`` (cooperative; the
  running code is **not** interrupted mid-flight).

State is persisted to ``<job_dir>/<job_id>.json`` so jobs survive
restarts of the MCP server (the FreeCAD-side runner may still
finish them — at the very least, we record status accurately).

Threading model
---------------

The runner uses ``concurrent.futures.ThreadPoolExecutor`` with a
configurable ``max_workers`` (default 1 — FreeCAD's GUI is not
thread-safe). Jobs run sequentially but in the background, so the
MCP client can do other work while waiting.
"""
from __future__ import annotations

import json
import os
import threading
import time
import traceback
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

JOB_DIRNAME = "freecad-mcp"
JOB_LEAF = "jobs"


def job_dir() -> Path:
    """Return the directory where job JSON files live."""
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    p = Path(base) / JOB_DIRNAME / JOB_LEAF
    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return p


def _job_path(job_id: str) -> Path:
    return job_dir() / f"{job_id}.json"


# ---------------------------------------------------------------------------
# Job record
# ---------------------------------------------------------------------------


# Status string constants (intentionally not an Enum so they can be
# serialised trivially to JSON).
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_ERROR = "error"
STATUS_CANCELLED = "cancelled"

_TERMINAL = {STATUS_DONE, STATUS_ERROR, STATUS_CANCELLED}


@dataclass
class Job:
    job_id: str
    label: str
    status: str = STATUS_PENDING
    submitted_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    result: Any = None
    error: str | None = None
    traceback: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Job:
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL

    def elapsed_seconds(self) -> float:
        if self.started_at is None:
            return 0.0
        end = self.finished_at if self.finished_at is not None else time.time()
        return end - self.started_at


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class JobRunner:
    """Background runner. Singleton per process.

    FreeCAD's GUI is not thread-safe; running two FreeCAD scripts
    in parallel usually segfaults the kernel. We use a single-worker
    executor by default. ``max_workers`` can be raised for **pure**
    Python jobs (e.g. file parsing, format conversion) — but
    anything that touches ``App.ActiveDocument`` should stay on
    worker 0.
    """

    def __init__(self, max_workers: int = 1) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._futures: dict[str, Future[Any]] = {}
        self._cancelled: set[str] = set()
        # Load any persisted jobs so list_jobs() works across restarts.
        for p in job_dir().glob("*.json"):
            try:
                data = json.loads(p.read_text())
                job = Job.from_dict(data)
                # A persisted job that wasn't terminal is "lost" — mark error.
                if not job.is_terminal:
                    job.status = STATUS_ERROR
                    job.error = "runner restarted before completion"
                    job.finished_at = job.finished_at or time.time()
                    p.write_text(json.dumps(job.to_dict(), indent=2))
                self._jobs[job.job_id] = job
            except Exception:
                with __import__("contextlib").suppress(Exception):
                    p.unlink()
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="mcp-freecad-job",
        )

    # -- submission ----------------------------------------------------------

    def submit(
        self,
        code: str,
        *,
        label: str = "",
        globals_: dict[str, Any] | None = None,
        runner: Callable[[str, dict[str, Any]], Any] | None = None,
    ) -> Job:
        """Submit ``code`` to be run in a background thread.

        ``runner`` is an optional override: ``runner(code, globals)``.
        It defaults to :func:`_default_runner`, which executes the
        code with ``exec(code, globals_)`` where ``globals_`` defaults
        to ``{"FreeCAD": FreeCAD, ...}`` if FreeCAD is importable.
        """
        job = Job(
            job_id=str(uuid.uuid4()),
            label=label or f"job-{time.strftime('%H%M%S')}",
        )
        with self._lock:
            self._jobs[job.job_id] = job
            self._persist(job)
            self._cancelled.discard(job.job_id)
            fut = self._executor.submit(self._run, job, code, globals_, runner)
            self._futures[job.job_id] = fut
        return job

    # -- inspection ----------------------------------------------------------

    def poll(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list_jobs(self, *, include_terminal: bool = True) -> list[Job]:
        with self._lock:
            jobs = list(self._jobs.values())
        jobs.sort(key=lambda j: j.submitted_at, reverse=True)
        if not include_terminal:
            jobs = [j for j in jobs if not j.is_terminal]
        return jobs

    def cancel(self, job_id: str) -> bool:
        """Request cancellation. Returns True if the job is now cancelled.

        Cooperative: if the job is already running, the running code
        is **not** interrupted. The next :func:`poll` will still show
        ``running`` until the job naturally finishes.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            if job.is_terminal:
                return False
            if job.status == STATUS_PENDING:
                job.status = STATUS_CANCELLED
                job.finished_at = time.time()
                self._persist(job)
                return True
            # Already running — mark and let _run pick it up.
            self._cancelled.add(job_id)
            return True

    # -- internals -----------------------------------------------------------

    def _run(
        self,
        job: Job,
        code: str,
        globals_: dict[str, Any] | None,
        runner: Callable[[str, dict[str, Any]], Any] | None,
    ) -> None:
        with self._lock:
            if job.job_id in self._cancelled:
                job.status = STATUS_CANCELLED
                job.finished_at = time.time()
                self._persist(job)
                return
            job.status = STATUS_RUNNING
            job.started_at = time.time()
            self._persist(job)

        try:
            r = (runner or _default_runner)(code, globals_ or {})
            with self._lock:
                if job.job_id in self._cancelled:
                    job.status = STATUS_CANCELLED
                else:
                    job.status = STATUS_DONE
                    job.result = _truncate(r)
                job.finished_at = time.time()
                self._persist(job)
        except Exception as e:
            with self._lock:
                job.status = STATUS_ERROR if job.job_id not in self._cancelled else STATUS_CANCELLED
                job.error = f"{type(e).__name__}: {e}"
                job.traceback = traceback.format_exc(limit=12)
                job.finished_at = time.time()
                self._persist(job)

    def _persist(self, job: Job) -> None:
        try:
            _job_path(job.job_id).write_text(json.dumps(job.to_dict(), indent=2, default=str))
        except Exception:
            pass


def _truncate(value: Any, *, max_chars: int = 20000) -> Any:
    """Best-effort truncation of a result for JSON persistence.

    Strings: truncate. Anything else: pass through unchanged. We
    deliberately avoid ``repr`` because large numpy arrays blow
    up to megabytes.
    """
    if isinstance(value, str) and len(value) > max_chars:
        return value[:max_chars] + f"\n... [truncated {len(value) - max_chars} chars]"
    return value


def _default_runner(code: str, globals_: dict[str, Any]) -> Any:
    """Run ``code`` via ``exec`` and return the value of ``result`` if set."""
    ns = dict(globals_)
    exec(code, ns)
    return ns.get("result", ns.get("_", None))


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------


_singleton: JobRunner | None = None
_singleton_lock = threading.Lock()


def get_runner() -> JobRunner:
    """Return the process-wide singleton, creating it on first call."""
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = JobRunner()
    return _singleton


def reset_runner() -> None:
    """Drop the singleton (test helper)."""
    global _singleton
    with _singleton_lock:
        _singleton = None


__all__ = [
    "Job",
    "JobRunner",
    "STATUS_PENDING",
    "STATUS_RUNNING",
    "STATUS_DONE",
    "STATUS_ERROR",
    "STATUS_CANCELLED",
    "get_runner",
    "reset_runner",
    "job_dir",
]
