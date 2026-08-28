"""Multi-instance FreeCAD registry — discovery + spawn + select.

This module enables the MCP server (or external tooling) to manage
**multiple FreeCAD instances simultaneously**, each with its own
UUID, socket path, and discovery file.

Typical use cases:

* Run several batch jobs (FEM, CAM) in parallel on different cores.
* Run a debug build side-by-side with a release build.
* Isolate hot-reload tests from the main work session.

Discovery layout
----------------

Each running FreeCAD writes a JSON file at::

    ``~/.cache/freecad-mcp/instances/<uuid>.json``

with the schema::

    {
        "uuid": "...",
        "label": "user-supplied or generated",
        "pid": 12345,
        "host": "localhost",
        "port": 9875,
        "started_at": 1700000000.0,
        "freecad_version": "1.1.3",
        "active_document": "Unnamed" | null,
        "is_headless": false,
        "command": "freecad ..."
    }

The discovery directory is **auto-created** by
:func:`register_instance` on first write. The
:func:`unregister_instance` call removes the file. Stale files
(``now - started_at > max_age``) are pruned on every list call.

This module is *pure Python* and does **not** import FreeCAD
directly, so it is testable in isolation.
"""
from __future__ import annotations

import contextlib
import json
import os
import socket
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

DISCOVERY_DIRNAME = "freecad-mcp"
DISCOVERY_LEAF = "instances"


def discovery_dir() -> Path:
    """Return the directory where instance metadata files live.

    Honours ``XDG_CACHE_HOME`` (Linux) but falls back to ``~/.cache``.
    Always creates the directory if it does not exist.
    """
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    p = Path(base) / DISCOVERY_DIRNAME / DISCOVERY_LEAF
    # Discovery is best-effort: if we cannot create the directory,
    # callers will get an OSError on the actual write.
    with contextlib.suppress(Exception):
        p.mkdir(parents=True, exist_ok=True)
    return p


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


@dataclass
class InstanceInfo:
    """Metadata about a single running FreeCAD MCP instance."""

    uuid: str
    label: str
    pid: int
    host: str
    port: int
    started_at: float
    freecad_version: str = "unknown"
    active_document: str | None = None
    is_headless: bool = False
    command: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable snapshot of this instance."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> InstanceInfo:
        """Rebuild an ``InstanceInfo`` from its dict form.

        Unknown keys are silently dropped so a forward-compatible
        payload from a newer build still parses on older code.
        """
        # Tolerate unknown fields (forward-compat).
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


def _info_path(uuid_str: str) -> Path:
    return discovery_dir() / f"{uuid_str}.json"


def register_instance(
    *,
    label: str | None = None,
    host: str = "localhost",
    port: int = 9875,
    is_headless: bool = False,
    command: str = "",
    freecad_version: str = "unknown",
    active_document: str | None = None,
) -> InstanceInfo:
    """Register a new instance. Returns its :class:`InstanceInfo`.

    The UUID is generated automatically. The PID comes from
    :func:`os.getpid`. The ``started_at`` is the current epoch time.
    """
    info = InstanceInfo(
        uuid=str(uuid.uuid4()),
        label=label or f"FreeCAD-{socket.gethostname()}-{port}",
        pid=os.getpid(),
        host=host,
        port=port,
        started_at=time.time(),
        freecad_version=freecad_version,
        active_document=active_document,
        is_headless=is_headless,
        command=command or " ".join(sys.argv),
    )
    _info_path(info.uuid).write_text(json.dumps(info.to_dict(), indent=2))
    return info


def unregister_instance(uuid_str: str) -> bool:
    """Remove the discovery file for an instance. Returns True if removed."""
    p = _info_path(uuid_str)
    if p.exists():
        try:
            p.unlink()
            return True
        except Exception:
            return False
    return False


def list_instances(*, max_age_seconds: float = 7 * 24 * 3600.0) -> list[InstanceInfo]:
    """List all known instances, pruning stale files.

    ``max_age_seconds`` defaults to 7 days. Set to 0 to skip pruning.
    """
    out: list[InstanceInfo] = []
    now = time.time()
    for p in list(discovery_dir().glob("*.json")):
        try:
            data = json.loads(p.read_text())
            info = InstanceInfo.from_dict(data)
        except Exception:
            # Corrupted file; remove so it does not pollute future lists.
            with __import__("contextlib").suppress(Exception):
                p.unlink()
            continue
        if max_age_seconds > 0 and (now - info.started_at) > max_age_seconds:
            with __import__("contextlib").suppress(Exception):
                p.unlink()
            continue
        out.append(info)
    out.sort(key=lambda i: i.started_at, reverse=True)
    return out


def get_instance(uuid_str: str) -> InstanceInfo | None:
    """Return instance by UUID, or None if not found."""
    p = _info_path(uuid_str)
    if not p.exists():
        return None
    try:
        return InstanceInfo.from_dict(json.loads(p.read_text()))
    except Exception:
        return None


def select_instance(uuid_str: str) -> dict[str, Any]:
    """Validate that an instance exists and is reachable.

    Returns ``{"uuid": ..., "ok": bool, "reason": str|None,
    "info": dict}``.
    """
    info = get_instance(uuid_str)
    if info is None:
        return {"uuid": uuid_str, "ok": False, "reason": "not found", "info": None}
    reachable, latency_ms = _probe_tcp(info.host, info.port, timeout=0.4)
    payload = info.to_dict()
    payload["latency_ms"] = round(latency_ms, 2)
    return {
        "uuid": uuid_str,
        "ok": reachable,
        "reason": None if reachable else "tcp probe failed",
        "info": payload,
    }


def _probe_tcp(host: str, port: int, *, timeout: float = 0.4) -> tuple[bool, float]:
    """Open a TCP connection to (host, port). Returns (ok, latency_ms)."""
    t0 = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, (time.monotonic() - t0) * 1000.0
    except Exception:
        return False, (time.monotonic() - t0) * 1000.0


# ---------------------------------------------------------------------------
# Active-instance selection
# ---------------------------------------------------------------------------

_ACTIVE_FILE = Path("active_instance.json")


def set_active(uuid_str: str) -> None:
    """Persist the active instance UUID in the discovery directory."""
    (discovery_dir() / _ACTIVE_FILE).write_text(json.dumps({"active": uuid_str}))


def get_active() -> str | None:
    """Read the active UUID, or None if not set."""
    p = discovery_dir() / _ACTIVE_FILE
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text()).get("active")
    except Exception:
        return None


def clear_active() -> None:
    """Remove the active marker file (if any)."""
    p = discovery_dir() / _ACTIVE_FILE
    with __import__("contextlib").suppress(Exception):
        p.unlink()


__all__ = [
    "InstanceInfo",
    "discovery_dir",
    "register_instance",
    "unregister_instance",
    "list_instances",
    "get_instance",
    "select_instance",
    "set_active",
    "get_active",
    "clear_active",
]
