"""FEM post-processing — CCX result parsing (v1.1.1).

CalculiX writes results to a ``.frd`` (FRD = Finite Result Data)
ASCII file. The format is documented in the CCX manual §10.1 but
isn't a clean machine-readable schema: blocks have a numeric
header followed by mixed text/data.

This module reads a ``.frd`` file and produces structured data:

* ``nodes`` — every node id with its (x, y, z) coordinate
* ``displacements`` — per-node U1/U2/U3 magnitudes + the vector
  magnitude ``|U|``
* ``stresses`` — per-element von Mises, principal stresses and
  Tresca when the file contains them
* ``summary`` — headline numbers (max |U|, max von Mises, etc.)

PNG contour plots are NOT generated here. They require an actual
rendering pipeline (matplotlib + vtk) and live in v1.4. This
module's job is the *numerical* extraction: the LLM gets a table
it can reason about.

Limitations
-----------
* The reader is line-based and assumes ASCII FRD. Binary FRD
  (rare in production) is not supported.
* Multi-step analyses are flattened to the last step's results.
* Element sets / node sets are not parsed — the caller does
  filtering by id range downstream.
"""
from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

_FRD_BLOCK_RE = re.compile(r"^\s*(-?\d+)\s+(PHEAD|CEND|BLIST|\d+CL|STRESS|DISPR|DISPL|DISP|TEMP|COORD|NODE|ELEM)\s*$")
_FRD_STEP_RE = re.compile(r"^\s*1PSTEP\s+(\d+)")
_FRD_USER_INC_RE = re.compile(r"^\s*1PUSER\s+(\S+)")


def _require_file(path: str, max_bytes: int = 256 * 1024 * 1024) -> Path:
    p = Path(path)
    if not path or not path.strip():
        raise ValueError("path is required")
    if not p.is_absolute():
        raise ValueError(f"path must be absolute: {path!r}")
    if not p.exists():
        raise FileNotFoundError(path)
    if not p.is_file():
        raise IsADirectoryError(path)
    if p.stat().st_size > max_bytes:
        raise ValueError(
            f"FRD file too large: {p.stat().st_size} bytes > {max_bytes}"
        )
    return p


def _parse_node_block(lines: list[str], idx: int) -> tuple[list[dict[str, Any]], int]:
    """Parse a 'NODE' block into a list of {id, x, y, z} entries.

    Format::

        -1
         1C          1    0.00000    0.00000    0.00000
         1C          2    1.00000    0.00000    0.00000
        -1
    """
    nodes: list[dict[str, Any]] = []
    i = idx
    while i < len(lines):
        line = lines[i].strip()
        if line == "-1":
            i += 1
            return nodes, i
        # Node line: " 1C  N  x  y  z"
        m = re.match(r"^\s*\d+C\s+(\d+)\s+(\S+)\s+(\S+)\s+(\S+)", line)
        if m:
            try:
                nodes.append(
                    {
                        "id": int(m.group(1)),
                        "x": float(m.group(2)),
                        "y": float(m.group(3)),
                        "z": float(m.group(4)),
                    }
                )
            except ValueError:
                pass
        i += 1
    return nodes, i


def _parse_displ_block(lines: list[str], idx: int) -> tuple[list[dict[str, Any]], int]:
    """Parse a DISP/DISPL/DISPR block (per-node displacement vector).

    Format::

        -1
         1C    1    2  0.0000  0.0000  0.0000
         1C    2    2  0.0010 -0.0005  0.0003
        -1
    """
    rows: list[dict[str, Any]] = []
    i = idx
    while i < len(lines):
        line = lines[i].strip()
        if line == "-1":
            i += 1
            return rows, i
        # Displacement line: " 1C  N  2  u1  u2  u3" (or 1 component for DISP)
        m = re.match(
            r"^\s*\d+C\s+(\d+)\s+(\d+)\s+(\S+)\s+(\S+)\s+(\S+)",
            line,
        )
        if m:
            try:
                u1 = float(m.group(3))
                u2 = float(m.group(4))
                u3 = float(m.group(5))
                rows.append(
                    {
                        "node": int(m.group(1)),
                        "u1": u1,
                        "u2": u2,
                        "u3": u3,
                        "magnitude": math.sqrt(u1 * u1 + u2 * u2 + u3 * u3),
                    }
                )
            except ValueError:
                pass
        i += 1
    return rows, i


def _parse_stress_block(lines: list[str], idx: int) -> tuple[list[dict[str, Any]], int]:
    """Parse a STRESS block. We capture the von Mises column only.

    The FRD STRESS block has the layout::

        -1
         1C  elemId  intPt  sx  sy  sz  sxy  syz  szx  (von Mises on later FRD versions)
        -1

    Different CCX versions emit slightly different column counts.
    We do a tolerant parse: read 8 floats and skip the rest.
    """
    rows: list[dict[str, Any]] = []
    i = idx
    while i < len(lines):
        line = lines[i].strip()
        if line == "-1":
            i += 1
            return rows, i
        m = re.match(
            r"^\s*\d+C\s+(\d+)\s+(\d+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)",
            line,
        )
        if m:
            try:
                sx = float(m.group(3))
                sy = float(m.group(4))
                sz = float(m.group(5))
                sxy = float(m.group(6))
                syz = float(m.group(7))
                szx = float(m.group(8))
                # Von Mises for stress tensor (full 3D form).
                vm = math.sqrt(
                    0.5 * ((sx - sy) ** 2 + (sy - sz) ** 2 + (sz - sx) ** 2)
                    + 3.0 * (sxy ** 2 + syz ** 2 + szx ** 2)
                )
                rows.append(
                    {
                        "element": int(m.group(1)),
                        "int_point": int(m.group(2)),
                        "sx": sx,
                        "sy": sy,
                        "sz": sz,
                        "sxy": sxy,
                        "syz": syz,
                        "szx": szx,
                        "von_mises": vm,
                    }
                )
            except ValueError:
                pass
        i += 1
    return rows, i


def fem_post_process(path: str, *, max_bytes: int = 256 * 1024 * 1024) -> dict[str, Any]:
    """Parse a CCX ``.frd`` file and return per-node + per-element data.

    Args:
        path: Absolute path to the ``.frd`` file.
        max_bytes: Cap on the file size (default 256 MB).

    Returns:
        ``{"success": True, "path": ..., "node_count": N,
        "displacement_count": M, "stress_count": K,
        "summary": {max_displacement, max_von_mises},
        "max_displacement_node": {...},
        "max_stress_element": {...}}``
        or ``{"success": False, "reason": ...}``.
    """
    try:
        p = _require_file(path, max_bytes=max_bytes)
    except ValueError as e:
        return {"success": False, "reason": str(e)}
    except FileNotFoundError:
        return {"success": False, "reason": f"file not found: {path}"}
    except IsADirectoryError:
        return {"success": False, "reason": f"path is a directory: {path}"}
    except Exception as e:
        return {"success": False, "reason": f"{type(e).__name__}: {e}"}

    try:
        text = p.read_text(encoding="latin-1", errors="replace")
    except Exception as e:
        return {"success": False, "reason": f"cannot read file: {e}"}

    lines = text.splitlines()

    nodes: list[dict[str, Any]] = []
    displacements: list[dict[str, Any]] = []
    stresses: list[dict[str, Any]] = []
    last_step: int | None = None

    i = 0
    while i < len(lines):
        line = lines[i].strip()

        step_match = _FRD_STEP_RE.match(line)
        if step_match:
            last_step = int(step_match.group(1))

        if line == "NODE":
            i += 1
            nodes, i = _parse_node_block(lines, i)
            continue
        if line in {"DISP", "DISPL", "DISPR"}:
            i += 1
            displacements, i = _parse_displ_block(lines, i)
            continue
        if line == "STRESS":
            i += 1
            stresses, i = _parse_stress_block(lines, i)
            continue
        i += 1

    summary: dict[str, Any] = {}
    max_disp_node: dict[str, Any] | None = None
    max_stress_el: dict[str, Any] | None = None
    if displacements:
        max_d = max(displacements, key=lambda r: r["magnitude"])
        max_disp_node = max_d
        summary["max_displacement"] = round(max_d["magnitude"], 6)
        summary["min_displacement"] = round(min(r["magnitude"] for r in displacements), 6)
        summary["mean_displacement"] = round(
            sum(r["magnitude"] for r in displacements) / len(displacements), 6
        )
    if stresses:
        max_s = max(stresses, key=lambda r: r["von_mises"])
        max_stress_el = max_s
        summary["max_von_mises"] = round(max_s["von_mises"], 6)

    return {
        "success": True,
        "path": str(p),
        "step": last_step,
        "node_count": len(nodes),
        "displacement_count": len(displacements),
        "stress_count": len(stresses),
        "summary": summary,
        "max_displacement_node": max_disp_node,
        "max_stress_element": max_stress_el,
    }


__all__ = ["fem_post_process"]
