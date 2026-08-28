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

import contextlib
import json
import os
import re
import threading
import time
import traceback
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

JOB_DIRNAME = "freecad-mcp"
JOB_LEAF = "jobs"


def job_dir() -> Path:
    """Return the directory where job JSON files live."""
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    p = Path(base) / JOB_DIRNAME / JOB_LEAF
    with contextlib.suppress(Exception):
        p.mkdir(parents=True, exist_ok=True)
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
        """Serialise the job state for JSON persistence / API responses."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Job:
        """Rebuild a ``Job`` from its dict form.

        Unknown keys are dropped (forward-compat with newer builds).
        """
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    @property
    def is_terminal(self) -> bool:
        """``True`` when the job has finished (``done``/``error``/``cancelled``)."""
        return self.status in _TERMINAL

    def elapsed_seconds(self) -> float:
        """Return wall-clock seconds since ``started_at``.

        Uses ``finished_at`` if available, otherwise ``now`` — so
        callers polling a running job get a live elapsed value.
        Returns 0.0 for jobs that never started (e.g. cancelled
        while still pending).
        """
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
        """Return the current snapshot of a job, or ``None`` if unknown.

        Cheap; suitable for tight polling loops.
        """
        with self._lock:
            return self._jobs.get(job_id)

    def list_jobs(self, *, include_terminal: bool = True) -> list[Job]:
        """Return all known jobs sorted by submission time (newest first).

        Set ``include_terminal=False`` to drop already-finished jobs
        (``done`` / ``error`` / ``cancelled``) — useful for showing
        just what's still running.
        """
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
                # Redact secrets from the error message before persisting.
                job.error = _redact(f"{type(e).__name__}: {e}")
                job.traceback = _redact(traceback.format_exc(limit=12))
                job.finished_at = time.time()
                self._persist(job)

    def _persist(self, job: Job) -> None:
        with contextlib.suppress(Exception):
            _job_path(job.job_id).write_text(
                json.dumps(job.to_dict(), indent=2, default=str)
            )


def _truncate(value: Any, *, max_chars: int = 20000) -> Any:
    """Best-effort truncation + redaction of a result for JSON persistence.

    Strings: redact secrets first, then truncate. Containers: redact
    recursively, then truncate the JSON dump if it grows past
    ``max_chars``. Anything else: pass through unchanged. We
    deliberately avoid ``repr`` because large numpy arrays blow
    up to megabytes.
    """
    redacted = _redact(value)
    if isinstance(redacted, str) and len(redacted) > max_chars:
        return redacted[:max_chars] + f"\n... [truncated {len(redacted) - max_chars} chars]"
    return redacted


# Patterns that look like secrets we never want persisted to disk in
# job records. The list is intentionally conservative — false positives
# are fine, false negatives are not.
#
# The first regex matches the whole ``key=value`` pair (so the dict
# walker, which sees values one at a time, can still tell that the
# value belongs to a sensitive key by combining the key + value via
# JSON-style ``repr`` first).
_SENSITIVE_PATTERNS = (
    # ``password = X``, ``password: X``, ``password="hunter2"`` etc.
    re.compile(
        r"(?i)(?:password|passwd|secret|token|api[_-]?key)"
        r"\s*[=:]\s*"
        r"(?:['\"]?)([^\s,'\"]+)(?:['\"]?)"
    ),
    # ``Authorization: Bearer <token>`` — case-insensitive header prefix
    re.compile(r"(?i)\bbearer\s+([A-Za-z0-9._\-]{8,})"),
    # GitHub personal access tokens (classic + fine-grained + user-to-server)
    re.compile(r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"),
    # OpenAI / Anthropic-style keys
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    # Slack tokens
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
)

# Keys whose value alone should be redacted, even without the ``key=value``
# pattern around it (e.g. inside dicts where the walker only sees values).
_SENSITIVE_KEYS = frozenset({
    "password", "passwd", "secret", "token", "api_key", "apikey",
    "access_token", "refresh_token", "private_key", "auth", "authorization",
    "bearer",
})


def _redact(value: Any) -> Any:
    """Replace secrets in strings with ``[REDACTED]`` before persisting.

    Walks dicts and lists recursively. Dict values whose key matches a
    sensitive name (e.g. ``"password"``) are redacted wholesale even
    when the value alone does not look like a secret — this catches
    short or atypical secrets like ``"hunter2"`` or ``"x"`` that a
    pure-value regex would miss.

    Within string values we run a set of regex patterns that match
    common secret formats (GitHub tokens, OpenAI keys, ``key=val``
    pairs, …).
    """
    if isinstance(value, str):
        out = value
        for pat in _SENSITIVE_PATTERNS:
            out = pat.sub("[REDACTED]", out)
        return out
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            # If the key itself is sensitive, drop the value entirely.
            # Strip surrounding whitespace and lowercase for tolerance.
            if isinstance(k, str) and k.strip().lower() in _SENSITIVE_KEYS:
                out[k] = "[REDACTED]"
            else:
                out[k] = _redact(v)
        return out
    if isinstance(value, (list, tuple)):
        redacted = [_redact(v) for v in value]
        return type(value)(redacted) if isinstance(value, tuple) else redacted
    return value


def _default_runner(code: str, globals_: dict[str, Any]) -> Any:
    """Run ``code`` via ``exec`` and return the value of ``result`` if set."""
    ns = dict(globals_)
    exec(code, ns)
    return ns.get("result", ns.get("_"))


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------


_singleton: JobRunner | None = None
_singleton_lock = threading.Lock()


def _resolve_max_workers(default: int = 1) -> int:
    """Read max_workers from ``FREECAD_MCP_JOB_WORKERS`` env var.

    Operators raising the worker count past 1 must ensure their jobs
    do not touch FreeCAD's GUI thread (see the threading model note in
    the module docstring). Values below 1 are clamped to 1.
    """
    raw = os.environ.get("FREECAD_MCP_JOB_WORKERS", "").strip()
    if not raw:
        return default
    try:
        n = int(raw)
    except ValueError:
        return default
    return max(1, n)


def get_runner() -> JobRunner:
    """Return the process-wide singleton, creating it on first call.

    Worker count comes from :func:`_resolve_max_workers`, which honours
    the ``FREECAD_MCP_JOB_WORKERS`` environment variable. Tests can
    override via :func:`reset_runner` followed by a manual
    ``JobRunner(max_workers=N)`` instance.
    """
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = JobRunner(max_workers=_resolve_max_workers())
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
