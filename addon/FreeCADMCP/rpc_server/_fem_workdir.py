"""Helpers for managing the FEM solver scratch directory.

Extracted from ``rpc_server`` so it can be unit-tested without importing
FreeCAD / PySide. The CalculiX solver writes hundreds of MB per run to a
temp dir; without an explicit cleanup step the host disk fills up over
time. Operators can opt out via the ``FREECAD_MCP_KEEP_FEM_WORKDIR`` env
var when they need to inspect CCX inputs/outputs after the fact.
"""
from __future__ import annotations

import os
import shutil
from typing import Callable

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def keep_fem_workdir(env: dict[str, str] | None = None) -> bool:
    """Return True if the FEM scratch dir should be retained after a solve.

    Reads from the provided ``env`` mapping (defaults to ``os.environ``) so
    tests can pass a controlled dict without monkey-patching the process.
    """
    source = os.environ if env is None else env
    val = source.get("FREECAD_MCP_KEEP_FEM_WORKDIR", "").strip().lower()
    return val in _TRUTHY


def safe_rmtree(
    path: str,
    on_warning: Callable[[str], None] | None = None,
) -> None:
    """Best-effort recursive removal that never raises.

    ``on_warning`` (optional) is invoked with a human-readable message if
    cleanup fails; in production this is wired to ``FreeCAD.Console``.
    """
    try:
        shutil.rmtree(path, ignore_errors=False)
    except Exception as e:  # noqa: BLE001 — best-effort cleanup by contract
        if on_warning is not None:
            on_warning(
                f"failed to remove FEM workdir '{path}': {type(e).__name__}: {e}"
            )


__all__ = ["keep_fem_workdir", "safe_rmtree"]
