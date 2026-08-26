import logging
from typing import Any

from ..freecad_client import FreeCADConnection
from ..guidelines import check_code_conflict, check_path_conflict
from ..metrics import MetricsRegistry
from ..responses import ToolResponse, add_screenshot_if_available, json_response, text_response
from ..schemas import validate_create_object, validate_edit_object
from ..utils import safe_operation

logger = logging.getLogger("FreeCADMCPserver")


@safe_operation
def create_document_operation(freecad: FreeCADConnection, name: str) -> ToolResponse:
    # Document names are free-form labels — we do not scan them for code-style
    # dangerous tokens (which would block legitimate names like "eval test").
    res = freecad.create_document(name)
    if res.get("success"):
        return text_response(f"Document '{res['document_name']}' created successfully")
    return text_response(f"Failed to create document: {res.get('error')}")


@safe_operation
def create_object_operation(
    freecad: FreeCADConnection,
    only_text_feedback: bool,
    doc_name: str,
    obj_type: str,
    obj_name: str,
    analysis_name: str | None = None,
    obj_properties: dict[str, Any] | None = None,
) -> ToolResponse:
    # v0.4.0 — validate parameters with Pydantic before sending to FreeCAD.
    # Catches typos and structural errors at the MCP layer, so the LLM
    # gets a clear error message rather than a vague ``Fault`` from the
    # FreeCAD process.
    try:
        validated = validate_create_object({
            "doc_name": doc_name,
            "obj_type": obj_type,
            "obj_name": obj_name,
            "analysis_name": analysis_name,
            "obj_properties": obj_properties,
        })
    except Exception as e:
        logger.warning("create_object validation failed: %s", e)
        return text_response(f"Invalid create_object request: {e}")

    # Object names are also labels; no guidelines check here.
    obj_data = {
        "Name": validated.obj_name,
        "Type": validated.obj_type,
        "Properties": validated.obj_properties or {},
        "Analysis": validated.analysis_name,
    }
    res = freecad.create_object(validated.doc_name, obj_data)
    screenshot = freecad.get_active_screenshot()

    if res["success"]:
        response = text_response(f"Object '{res['object_name']}' created successfully")
    else:
        response = text_response(f"Failed to create object: {res['error']}")
    return add_screenshot_if_available(response, screenshot, only_text_feedback)


@safe_operation
def edit_object_operation(
    freecad: FreeCADConnection,
    only_text_feedback: bool,
    doc_name: str,
    obj_name: str,
    obj_properties: dict[str, Any],
) -> ToolResponse:
    # v0.4.0 — same validation gate as create_object.
    try:
        validated = validate_edit_object({
            "doc_name": doc_name,
            "obj_name": obj_name,
            "obj_properties": obj_properties,
        })
    except Exception as e:
        logger.warning("edit_object validation failed: %s", e)
        return text_response(f"Invalid edit_object request: {e}")

    res = freecad.edit_object(
        validated.doc_name, validated.obj_name, {"Properties": validated.obj_properties}
    )
    screenshot = freecad.get_active_screenshot()

    if res["success"]:
        response = text_response(f"Object '{res['object_name']}' edited successfully")
    else:
        response = text_response(f"Failed to edit object: {res['error']}")
    return add_screenshot_if_available(response, screenshot, only_text_feedback)


@safe_operation
def delete_object_operation(
    freecad: FreeCADConnection,
    only_text_feedback: bool,
    doc_name: str,
    obj_name: str,
) -> ToolResponse:
    res = freecad.delete_object(doc_name, obj_name)
    screenshot = freecad.get_active_screenshot()

    if res.get("success"):
        response = text_response(f"Object '{res['object_name']}' deleted successfully")
    else:
        response = text_response(f"Failed to delete object: {res.get('error')}")
    return add_screenshot_if_available(response, screenshot, only_text_feedback)


@safe_operation
def execute_code_operation(
    freecad: FreeCADConnection,
    only_text_feedback: bool,
    code: str,
) -> ToolResponse:
    # This is the only field where the code-style dangerous patterns apply,
    # because the value is forwarded directly to FreeCAD's Python exec().
    conflict, msg = check_code_conflict(code or "")
    if conflict:
        logger.warning("execute_code blocked by guidelines: %s", msg)
        return text_response(msg)

    res = freecad.execute_code(code)
    screenshot = freecad.get_active_screenshot()

    if res["success"]:
        response = text_response(f"Code executed successfully: {res['message']}")
    else:
        response = text_response(f"Failed to execute code: {res['error']}")
    return add_screenshot_if_available(response, screenshot, only_text_feedback)


@safe_operation
def get_view_operation(
    freecad: FreeCADConnection,
    view_name: str,
    width: int | None = None,
    height: int | None = None,
    focus_object: str | None = None,
    image_format: str = "png",
) -> ToolResponse:
    # v1.0.3 — use the structured helper so the error message reflects
    # the actual reason (no active view vs unsupported view type vs
    # Pillow-missing for JPEG/WebP), not a generic "cannot get
    # screenshot".
    status = freecad.get_active_screenshot_with_status(
        view_name=view_name, width=width, height=height,
        focus_object=focus_object, image_format=image_format,
    )
    if status.get("success"):
        return add_screenshot_if_available(
            [], status["screenshot"], only_text_feedback=False, image_format=image_format,
        )
    reason = status.get("reason", "unknown")
    detail = status.get("detail")
    if reason == "no_capture":
        # Server returned None; usually means unsupported view type
        # (Spreadsheet, TechDraw, Drawing) or Pillow missing for JPEG/WebP.
        msg = (
            f"Cannot get screenshot in the current view type or format {image_format!r}. "
            "Likely causes: TechDraw/Spreadsheet/Drawing view, or Pillow not installed "
            "for JPEG/WebP."
        )
    elif reason == "rpc_error":
        msg = f"RPC error while capturing screenshot: {detail}"
    elif reason == "timeout":
        msg = f"Screenshot capture timed out ({detail})"
    else:
        msg = f"Cannot get screenshot (reason={reason}, detail={detail})"
    return text_response(msg)


@safe_operation
def insert_part_from_library_operation(
    freecad: FreeCADConnection,
    only_text_feedback: bool,
    relative_path: str,
) -> ToolResponse:
    # Path-specific guard runs here; the authoritative realpath check happens
    # in the addon (parts_library._safe_resolve), but failing early gives a
    # better error message to the LLM.
    conflict, msg = check_path_conflict(relative_path or "")
    if conflict:
        logger.warning("insert_part_from_library blocked by guidelines: %s", msg)
        return text_response(
            f"Diretriz: {msg} Forneça um caminho relativo dentro da parts library."
        )

    res = freecad.insert_part_from_library(relative_path)
    screenshot = freecad.get_active_screenshot()

    if res.get("success"):
        response = text_response(f"Part inserted from library: {res.get('message')}")
    else:
        response = text_response(f"Failed to insert part from library: {res.get('error')}")
    return add_screenshot_if_available(response, screenshot, only_text_feedback)


@safe_operation
def get_objects_operation(
    freecad: FreeCADConnection,
    only_text_feedback: bool,
    doc_name: str,
) -> ToolResponse:
    screenshot = freecad.get_active_screenshot()
    response = json_response(freecad.get_objects(doc_name))
    return add_screenshot_if_available(response, screenshot, only_text_feedback)


@safe_operation
def get_object_operation(
    freecad: FreeCADConnection,
    only_text_feedback: bool,
    doc_name: str,
    obj_name: str,
) -> ToolResponse:
    screenshot = freecad.get_active_screenshot()
    response = json_response(freecad.get_object(doc_name, obj_name))
    return add_screenshot_if_available(response, screenshot, only_text_feedback)


@safe_operation
def get_parts_list_operation(freecad: FreeCADConnection) -> ToolResponse:
    parts = freecad.get_parts_list()
    if parts:
        return json_response(parts)
    return text_response("No parts found in the parts library. You must add parts_library addon.")


@safe_operation
def list_documents_operation(freecad: FreeCADConnection) -> ToolResponse:
    return json_response(freecad.list_documents())


@safe_operation
def run_fem_analysis_operation(
    freecad: FreeCADConnection,
    only_text_feedback: bool,
    doc_name: str,
    analysis_name: str,
    timeout: int = 600,
) -> ToolResponse:
    res = freecad.run_fem_analysis(doc_name, analysis_name, timeout)
    if res.get("success"):
        def fmt(v, unit):
            return f"{v:.4g} {unit}" if isinstance(v, (int, float)) else f"unavailable ({unit})"
        screenshot = freecad.get_active_screenshot() if not only_text_feedback else None
        response = json_response({
            "summary": (
                f"FEM analysis '{analysis_name}' solved. "
                f"max von Mises = {fmt(res.get('max_von_mises_MPa'), 'MPa')}, "
                f"max displacement = {fmt(res.get('max_displacement_mm'), 'mm')} "
                f"({res.get('node_count')} nodes)."
            ),
            **res,
        })
        return add_screenshot_if_available(response, screenshot, only_text_feedback)
    return json_response({
        "summary": f"FEM analysis '{analysis_name}' failed: {res.get('error')}",
        **res,
    })


@safe_operation
def undo_operation(freecad: FreeCADConnection, doc_name: str, steps: int = 1) -> ToolResponse:
    res = freecad.undo(doc_name, steps)
    if res.get("success"):
        return text_response(
            f"Undid {res.get('undone_steps', steps)} transaction(s) in '{doc_name}'."
        )
    return text_response(f"Failed to undo: {res.get('error', 'unknown error')}")


@safe_operation
def redo_operation(freecad: FreeCADConnection, doc_name: str, steps: int = 1) -> ToolResponse:
    res = freecad.redo(doc_name, steps)
    if res.get("success"):
        return text_response(
            f"Redid {res.get('redone_steps', steps)} transaction(s) in '{doc_name}'."
        )
    return text_response(f"Failed to redo: {res.get('error', 'unknown error')}")


@safe_operation
def save_document_operation(freecad: FreeCADConnection, doc_name: str, path: str | None = None) -> ToolResponse:
    res = freecad.save_document(doc_name, path)
    if res.get("success"):
        return text_response(f"Saved '{doc_name}' to {res.get('path')}.")
    return text_response(f"Failed to save: {res.get('error', 'unknown error')}")


@safe_operation
def export_object_operation(
    freecad: FreeCADConnection, doc_name: str, obj_name: str, path: str, fmt: str | None = None,
) -> ToolResponse:
    res = freecad.export_object(doc_name, obj_name, path, fmt)
    if res.get("success"):
        return text_response(
            f"Exported '{obj_name}' to {res.get('path')} as {res.get('format')}."
        )
    return text_response(f"Failed to export: {res.get('error', 'unknown error')}")


@safe_operation
def get_active_view_operation(freecad: FreeCADConnection) -> ToolResponse:
    res = freecad.get_active_view()
    if not res.get("success"):
        return text_response(f"Failed to get active view: {res.get('error', 'unknown error')}")
    return json_response(res)


@safe_operation
def health_check_operation(
    freecad: FreeCADConnection,
    metrics: MetricsRegistry | None = None,
) -> ToolResponse:
    """Liveness/readiness probe.

    Composes:
    * The FreeCAD RPC ``health_check`` (uptime, queue sizes, settings path).
    * The MCP circuit-breaker state and counters.
    * The local :class:`MetricsRegistry` snapshot (tool calls,
      validation failures, histogram counts).

    The metrics block is always included; the operator dashboard or
    log aggregator consumes it as JSON.
    """
    fc_status = freecad.health_check()
    breaker = freecad.breaker_metrics()
    # Update the registry's gauges from the live breaker state.
    if metrics is not None:
        state_value = {"closed": 0, "half_open": 1, "open": 2}.get(breaker["state"], -1)
        metrics.circuit_state.set(state_value)
        # v0.4.0 fix: the breaker's ``total_short_circuits`` is already an
        # absolute cumulative count, so the metric must be ``set`` (not
        # ``inc``). Using ``inc`` here would multiply the value by the
        # number of ``health_check`` calls per scrape interval and drown
        # any rate-based alert (e.g. ``rate(...[5m]) > 0``).
        metrics.circuit_short_circuits.set(
            float(breaker.get("total_short_circuits", 0))
        )
        metrics.uptime_seconds.set(metrics.uptime())
        payload = {**fc_status, "circuit_breaker": breaker, "metrics": metrics.as_dict()}
    else:
        payload = {**fc_status, "circuit_breaker": breaker}
    return json_response(payload)
