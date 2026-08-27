"""CAM/Path toolpath operations for the FreeCAD MCP addon.

This module wraps FreeCAD's Path workbench in a small, JSON-friendly
API. It is **not** a full G-code generator — it delegates to
FreeCAD's Path objects (which produce the real G-code via
post-processors).

Available helpers
-----------------

* :func:`cam_create_job`            — create a ``Path::Job`` with stock + tool controller.
* :func:`cam_add_operation`         — add a profile/pocket/adaptive/drilling/face op.
* :func:`cam_create_tool`           — create a tool in the tool library.
* :func:`cam_create_tool_controller`— create a tool controller (spindle, feed).
* :func:`cam_post_process`          — emit G-code via a post-processor.
* :func:`cam_simulate_toolpath`     — backplot simulation as a list of segments.

These helpers require the **Path** workbench (which ships with
FreeCAD). If unavailable, all helpers return a ``{"success":
False, "reason": "Path workbench not available"}`` dict.
"""
from __future__ import annotations

from typing import Any

try:
    import FreeCAD
    import Path
    import PathScripts
    import PathTool
    import PathToolController  # type: ignore[import-not-found]
    import PathScripts.tools as _path_tools  # noqa: F401
except Exception:  # pragma: no cover
    FreeCAD = None  # type: ignore[assignment]
    Path = None  # type: ignore[assignment]
    PathScripts = None  # type: ignore[assignment]
    PathTool = None  # type: ignore[assignment]
    PathToolController = None  # type: ignore[assignment]


def _err(reason: str) -> dict[str, Any]:
    return {"success": False, "reason": reason}


def _ensure_path() -> bool:
    return FreeCAD is not None and Path is not None


def _get_doc(doc_name: str):
    if FreeCAD is None:
        return None
    return FreeCAD.getDocument(doc_name)


# ---------------------------------------------------------------------------
# Tool creation
# ---------------------------------------------------------------------------


def cam_create_tool(
    doc_name: str,
    name: str,
    *,
    tool_type: str = "EndMill",
    diameter: float = 6.0,
    length: float = 50.0,
    material: str = "HighSpeedSteel",
) -> dict[str, Any]:
    """Create a tool entry in the document's tool library.

    ``tool_type`` is one of ``EndMill``, ``BallEndMill``, ``Drill``,
    ``CenterDrill``, ``CounterSink``, ``CounterBore``, ``ChamferMill``,
    ``Engraver``, ``BullnoseEndMill``.

    Returns ``{"success": True, "tool_name": ...}``.
    """
    if not _ensure_path():
        return _err("Path workbench not available")
    doc = _get_doc(doc_name)
    if doc is None:
        return _err(f"document {doc_name!r} not found")

    try:
        tool = doc.addObject("Path::Tool", name)
        tool.ToolType = tool_type
        tool.Diameter = float(diameter)
        tool.Length = float(length)
        try:
            tool.Material = material
        except Exception:
            # Older FreeCAD may not have Material on Path::Tool.
            pass
        doc.recompute()
        return {
            "success": True,
            "tool_name": tool.Name,
            "tool_type": tool_type,
            "diameter": diameter,
            "length": length,
        }
    except Exception as e:
        return _err(f"cam_create_tool failed: {type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# Tool controller
# ---------------------------------------------------------------------------


def cam_create_tool_controller(
    doc_name: str,
    name: str,
    tool_name: str,
    *,
    spindle_speed: float = 12000.0,
    feed_rate: float = 600.0,
    feed_rate_vertical: float = 300.0,
) -> dict[str, Any]:
    """Create a ``Path::ToolController`` that binds a tool to
    spindle speed + feed rates."""
    if not _ensure_path():
        return _err("Path workbench not available")
    doc = _get_doc(doc_name)
    if doc is None:
        return _err(f"document {doc_name!r} not found")
    tool = doc.getObject(tool_name)
    if tool is None:
        return _err(f"tool {tool_name!r} not found")
    try:
        tc = doc.addObject("Path::ToolController", name)
        tc.Tool = tool
        tc.SpindleSpeed = float(spindle_speed)
        tc.FeedRate = float(feed_rate)
        tc.FeedRateVertical = float(feed_rate_vertical)
        doc.recompute()
        return {
            "success": True,
            "controller_name": tc.Name,
            "tool": tool_name,
            "spindle_speed": spindle_speed,
            "feed_rate": feed_rate,
            "feed_rate_vertical": feed_rate_vertical,
        }
    except Exception as e:
        return _err(f"cam_create_tool_controller failed: {type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# Job creation
# ---------------------------------------------------------------------------


def cam_create_job(
    doc_name: str,
    name: str,
    *,
    base_shape: str | None = None,
    tool_controller_name: str | None = None,
    stock_x: float = 100.0,
    stock_y: float = 100.0,
    stock_z: float = 25.0,
) -> dict[str, Any]:
    """Create a Path::Job referencing a base shape and a tool controller."""
    if not _ensure_path():
        return _err("Path workbench not available")
    doc = _get_doc(doc_name)
    if doc is None:
        return _err(f"document {doc_name!r} not found")

    try:
        job = doc.addObject("Path::Job", name)
    except Exception as e:
        return _err(f"cam_create_job failed: {type(e).__name__}: {e}")

    if base_shape is not None:
        base = doc.getObject(base_shape)
        if base is None:
            return _err(f"base shape {base_shape!r} not found")
        try:
            job.Base = base
        except Exception as e:
            return _err(f"failed to set Base: {type(e).__name__}: {e}")

    if tool_controller_name is not None:
        tc = doc.getObject(tool_controller_name)
        if tc is None:
            return _err(f"tool controller {tool_controller_name!r} not found")
        try:
            job.ToolController = tc
        except Exception as e:
            return _err(f"failed to set ToolController: {type(e).__name__}: {e}")

    # Set stock extents.
    try:
        job.Stock = [
            (-float(stock_x) / 2, -float(stock_y) / 2, 0.0),
            (float(stock_x) / 2, float(stock_y) / 2, float(stock_z)),
        ]
    except Exception as e:
        return _err(f"failed to set Stock: {type(e).__name__}: {e}")

    doc.recompute()
    return {
        "success": True,
        "job_name": job.Name,
        "base_shape": base_shape,
        "tool_controller": tool_controller_name,
        "stock": [stock_x, stock_y, stock_z],
    }


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


_VALID_OPS = {
    "profile": "Profile",
    "pocket": "Pocket",
    "adaptive": "Adaptive",
    "drilling": "Drilling",
    "face": "FaceMilling",
}


def cam_add_operation(
    doc_name: str,
    job_name: str,
    op_type: str,
    name: str,
    *,
    base_shape: str | None = None,
    side: str = "Outside",
    step_down: float = 1.0,
    tool_controller_name: str | None = None,
) -> dict[str, Any]:
    """Add a Path operation to a Job.

    ``op_type`` is one of: ``profile``, ``pocket``, ``adaptive``,
    ``drilling``, ``face``.

    Returns ``{"success": True, "operation_name": ..., "op_type": ...}``.
    """
    if not _ensure_path():
        return _err("Path workbench not available")
    doc = _get_doc(doc_name)
    if doc is None:
        return _err(f"document {doc_name!r} not found")
    job = doc.getObject(job_name)
    if job is None:
        return _err(f"job {job_name!r} not found")
    if op_type not in _VALID_OPS:
        return _err(f"op_type {op_type!r} unknown (valid: {sorted(_VALID_OPS)})")

    # Resolve the base shape: argument wins, else job.Base, else job.Stock.
    base_obj = None
    if base_shape is not None:
        base_obj = doc.getObject(base_shape)
        if base_obj is None:
            return _err(f"base shape {base_shape!r} not found")
    else:
        try:
            base_obj = job.Base
        except Exception:
            base_obj = None
        if base_obj is None:
            try:
                base_obj = job.Stock
            except Exception:
                base_obj = None

    try:
        if op_type == "profile":
            op = doc.addObject("Path::FeaturePython", name)
            from PathScripts.PathProfile import ObjectProfile  # type: ignore[import-not-found]
            proxy = ObjectProfile(op, base_obj)
            op.Proxy = proxy
            op.OpType = "Profile"
            op.Side = side
            op.StepDown = float(step_down)
        elif op_type == "pocket":
            op = doc.addObject("Path::FeaturePython", name)
            from PathScripts.PathPocket import ObjectPocket  # type: ignore[import-not-found]
            proxy = ObjectPocket(op, base_obj)
            op.Proxy = proxy
            op.OpType = "Pocket"
            op.StepDown = float(step_down)
        elif op_type == "face":
            op = doc.addObject("Path::FeaturePython", name)
            from PathScripts.PathFace import ObjectFace  # type: ignore[import-not-found]
            proxy = ObjectFace(op, base_obj)
            op.Proxy = proxy
            op.OpType = "Face"
        elif op_type == "drilling":
            op = doc.addObject("Path::FeaturePython", name)
            from PathScripts.PathDrilling import ObjectDrilling  # type: ignore[import-not-found]
            proxy = ObjectDrilling(op, base_obj)
            op.Proxy = proxy
            op.OpType = "Drilling"
        elif op_type == "adaptive":
            op = doc.addObject("Path::FeaturePython", name)
            from PathScripts.PathAdaptive import ObjectAdaptive  # type: ignore[import-not-found]
            proxy = ObjectAdaptive(op, base_obj)
            op.Proxy = proxy
            op.OpType = "Adaptive"
            try:
                op.StepDown = float(step_down)
            except Exception:
                pass
        else:
            return _err(f"op_type {op_type!r} not implemented")
    except Exception as e:
        return _err(f"cam_add_operation failed: {type(e).__name__}: {e}")

    # Bind the tool controller if asked.
    if tool_controller_name is not None:
        tc = doc.getObject(tool_controller_name)
        if tc is None:
            return _err(f"tool controller {tool_controller_name!r} not found")
        try:
            op.ToolController = tc
        except Exception as e:
            return _err(f"failed to set ToolController: {type(e).__name__}: {e}")

    # Add to the job's operations group.
    try:
        group = job.Operations
        group.append(op)
        job.Operations = group
    except Exception as e:
        return _err(f"failed to add op to job: {type(e).__name__}: {e}")

    doc.recompute()
    return {
        "success": True,
        "operation_name": op.Name,
        "job_name": job_name,
        "op_type": op_type,
    }


# ---------------------------------------------------------------------------
# Post-processing
# ---------------------------------------------------------------------------


def cam_post_process(
    doc_name: str,
    job_name: str,
    *,
    post_processor: str = "linuxcnc",
    output_path: str | None = None,
) -> dict[str, Any]:
    """Run a post-processor on the job and return the G-code.

    If ``output_path`` is given, the G-code is also written there.
    """
    if not _ensure_path():
        return _err("Path workbench not available")
    doc = _get_doc(doc_name)
    if doc is None:
        return _err(f"document {doc_name!r} not found")
    job = doc.getObject(job_name)
    if job is None:
        return _err(f"job {job_name!r} not found")

    try:
        from PathScripts.PostUtils import PostProcessor  # type: ignore[import-not-found]
    except Exception as e:
        return _err(f"PostUtils not available: {type(e).__name__}: {e}")

    try:
        post = PostProcessor(post_processor, "")
        gcode = post.export_GCode(job)
    except Exception as e:
        return _err(f"post-process failed: {type(e).__name__}: {e}")

    if output_path is not None:
        try:
            with open(output_path, "w", encoding="utf-8") as fh:
                fh.write(gcode)
        except Exception as e:
            return _err(f"failed to write {output_path!r}: {type(e).__name__}: {e}")

    line_count = gcode.count("\n") + 1 if gcode else 0
    return {
        "success": True,
        "job_name": job_name,
        "post_processor": post_processor,
        "output_path": output_path,
        "line_count": line_count,
        "char_count": len(gcode) if gcode else 0,
        "gcode_preview": gcode[:500] if gcode else "",
    }


# ---------------------------------------------------------------------------
# Simulation (backplot)
# ---------------------------------------------------------------------------


def cam_simulate_toolpath(
    doc_name: str,
    job_name: str,
    *,
    max_segments: int = 5000,
) -> dict[str, Any]:
    """Return a list of ``(x, y, z)`` points approximating the tool path.

    The result is a downsampled backplot suitable for plotting
    on the client side.
    """
    if not _ensure_path():
        return _err("Path workbench not available")
    doc = _get_doc(doc_name)
    if doc is None:
        return _err(f"document {doc_name!r} not found")
    job = doc.getObject(job_name)
    if job is None:
        return _err(f"job {job_name!r} not found")

    points: list[list[float]] = []
    try:
        # Iterate every operation in the job and concatenate Path commands.
        for op in getattr(job, "Operations", []):
            try:
                cmd_list = op.Path.Commands
            except Exception:
                continue
            for cmd in cmd_list:
                try:
                    # Each ``Command`` has ``x, y, z`` (or NaN).
                    x = float(getattr(cmd, "x", 0.0))
                    y = float(getattr(cmd, "y", 0.0))
                    z = float(getattr(cmd, "z", 0.0))
                except Exception:
                    continue
                points.append([round(x, 4), round(y, 4), round(z, 4)])
                if len(points) >= max_segments:
                    break
            if len(points) >= max_segments:
                break
    except Exception as e:
        return _err(f"simulate failed: {type(e).__name__}: {e}")

    return {
        "success": True,
        "job_name": job_name,
        "point_count": len(points),
        "truncated": len(points) >= max_segments,
        "points": points,
    }


__all__ = [
    "cam_create_tool",
    "cam_create_tool_controller",
    "cam_create_job",
    "cam_add_operation",
    "cam_post_process",
    "cam_simulate_toolpath",
]
