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


@safe_operation
def mesh_import_operation(
    freecad: FreeCADConnection,
    path: str,
    doc_name: str | None = None,
    label: str | None = None,
) -> ToolResponse:
    """Import a mesh file (.stl/.obj/.ply/.off/.mesh/.smf/.wrl/.3ds/.dae).

    Returns a JSON object with ``object_name``, ``label``,
    ``triangle_count`` and ``vertex_count`` on success.
    """
    res = freecad.mesh_import(path=path, doc_name=doc_name, label=label)
    if res.get("success"):
        return json_response(res)
    return text_response(f"mesh_import failed: {res.get('reason', 'unknown error')}")


@safe_operation
def mesh_simplify_operation(
    freecad: FreeCADConnection,
    doc_name: str,
    mesh_name: str,
    target_faces: int = 5_000,
) -> ToolResponse:
    """Decimate a ``Mesh::Feature`` to approximately *target_faces* triangles.

    The FreeCAD ``Mesh.Mesh.decimate`` API takes a *reduction ratio*,
    not a target count; we convert internally.
    """
    res = freecad.mesh_simplify(
        doc_name=doc_name, mesh_name=mesh_name, target_faces=target_faces
    )
    if res.get("success"):
        return json_response(res)
    return text_response(f"mesh_simplify failed: {res.get('reason', 'unknown error')}")


@safe_operation
def mesh_to_solid_operation(
    freecad: FreeCADConnection,
    doc_name: str,
    mesh_name: str,
    new_name: str | None = None,
    repair: bool = True,
    sew_tolerance: float = 1e-3,
    max_triangles_before_simplify: int = 50_000,
    target_faces_after_simplify: int = 5_000,
) -> ToolResponse:
    """Convert a ``Mesh::Feature`` into a parametric ``Part::Feature``.

    The resulting solid is editable like any other FreeCAD solid:
    dimensions can be modified (within the precision of the input
    mesh), booleans can be applied, and FEM can be run on it.

    Returns a JSON object with ``object_name``, ``shell_faces``,
    ``solid`` (bool), ``volume``, ``triangle_count`` and
    ``decimated``.
    """
    res = freecad.mesh_to_solid(
        doc_name=doc_name,
        mesh_name=mesh_name,
        new_name=new_name,
        repair=repair,
        sew_tolerance=sew_tolerance,
        max_triangles_before_simplify=max_triangles_before_simplify,
        target_faces_after_simplify=target_faces_after_simplify,
    )
    if res.get("success"):
        return json_response(res)
    return text_response(f"mesh_to_solid failed: {res.get('reason', 'unknown error')}")


@safe_operation
def step_extract_metadata_operation(freecad: FreeCADConnection, path: str) -> ToolResponse:
    """Extract AP214 metadata from a STEP Part 21 file.

    Returns a JSON object with ``success``, ``path``, ``description``,
    ``schema``, ``implementation_level``, ``name``, ``author``,
    ``organization``, ``preprocessor_version``, ``originating_system``,
    ``authorization`` and ``size_bytes``.
    """
    res = freecad.step_extract_metadata(path=path)
    if res.get("success"):
        return json_response(res)
    return text_response(f"step_extract_metadata failed: {res.get('reason', 'unknown error')}")


@safe_operation
def bom_export_operation(
    freecad: FreeCADConnection,
    doc_name: str,
    fmt: str = "json",
    include_extras: bool = False,
    group_by_type: bool = True,
) -> ToolResponse:
    """Export a Bill of Materials from *doc_name*.

    ``fmt`` is ``"json"`` (default) or ``"csv"``. ``include_extras``
    adds every non-dimension property to each JSON entry. When
    ``group_by_type`` is True (default), identical parts are
    collapsed and ``quantity`` increments.
    """
    res = freecad.bom_export(
        doc_name=doc_name,
        fmt=fmt,
        include_extras=include_extras,
        group_by_type=group_by_type,
    )
    if res.get("success"):
        # The data may already be a CSV string; surface it as-is.
        if res.get("format") == "csv":
            return text_response(res["data"])
        return json_response(res)
    return text_response(f"bom_export failed: {res.get('reason', 'unknown error')}")


@safe_operation
def fem_post_process_operation(freecad: FreeCADConnection, path: str) -> ToolResponse:
    """Parse a CalculiX ``.frd`` result file.

    Returns a JSON object with ``success``, ``path``, ``step``,
    ``node_count``, ``displacement_count``, ``stress_count``,
    ``summary`` (max/min/mean displacement, max von Mises) plus
    the worst-case node and element entries.

    PNG contour plots are not produced in this version (see v1.4
    roadmap); only numerical tables.
    """
    res = freecad.fem_post_process(path=path)
    if res.get("success"):
        return json_response(res)
    return text_response(f"fem_post_process failed: {res.get('reason', 'unknown error')}")


# ---------------------------------------------------------------------------
# v1.1.2 — Inspection & Measurement suite
# ---------------------------------------------------------------------------


@safe_operation
def list_faces_operation(
    freecad: FreeCADConnection,
    doc_name: str,
    obj_name: str,
    type_filter: str | None = None,
    limit: int = 100,
) -> ToolResponse:
    """Return per-face type / normal / area / centroid.

    ``type_filter`` keeps only faces whose geometric type contains
    that substring (case-insensitive). Useful with ``"Cylinder"``
    to find holes.
    """
    res = freecad.list_faces(
        doc_name=doc_name, obj_name=obj_name,
        type_filter=type_filter, limit=limit,
    )
    if res.get("success"):
        return json_response(res)
    return text_response(f"list_faces failed: {res.get('reason', 'unknown error')}")


@safe_operation
def measure_operation(
    freecad: FreeCADConnection,
    doc_name: str,
    obj_name: str,
    properties: list[str] | None = None,
) -> ToolResponse:
    """Return geometric measurements: volume, area, bbox, COM, length."""
    res = freecad.measure(
        doc_name=doc_name, obj_name=obj_name, properties=properties,
    )
    if res.get("success"):
        return json_response(res)
    return text_response(f"measure failed: {res.get('reason', 'unknown error')}")


@safe_operation
def measure_distance_operation(
    freecad: FreeCADConnection,
    doc_name: str,
    obj_a: str,
    obj_b: str,
) -> ToolResponse:
    """Return minimum distance between two shapes."""
    res = freecad.measure_distance(
        doc_name=doc_name, obj_a=obj_a, obj_b=obj_b,
    )
    if res.get("success"):
        return json_response(res)
    return text_response(f"measure_distance failed: {res.get('reason', 'unknown error')}")


@safe_operation
def geometric_verification_operation(
    freecad: FreeCADConnection,
    doc_name: str,
    obj_name: str,
    handedness_tol: float = 1e-3,
) -> ToolResponse:
    """Check shape validity, handedness, normal consistency."""
    res = freecad.geometric_verification(
        doc_name=doc_name, obj_name=obj_name, handedness_tol=handedness_tol,
    )
    if res.get("success"):
        return json_response(res)
    return text_response(f"geometric_verification failed: {res.get('reason', 'unknown error')}")


@safe_operation
def analyze_shape_operation(
    freecad: FreeCADConnection,
    doc_name: str,
    obj_name: str,
) -> ToolResponse:
    """Return counts of each surface type (Plane/Cylinder/Cone/Sphere...)."""
    res = freecad.analyze_shape(doc_name=doc_name, obj_name=obj_name)
    if res.get("success"):
        return json_response(res)
    return text_response(f"analyze_shape failed: {res.get('reason', 'unknown error')}")


@safe_operation
def spatial_query_operation(
    freecad: FreeCADConnection,
    doc_name: str,
    obj_a: str,
    obj_b: str,
    mode: str = "interference",
    clearance_tol: float = 0.05,
) -> ToolResponse:
    """Modes: interference, clearance, containment."""
    res = freecad.spatial_query(
        doc_name=doc_name, obj_a=obj_a, obj_b=obj_b,
        mode=mode, clearance_tol=clearance_tol,
    )
    if res.get("success"):
        return json_response(res)
    return text_response(f"spatial_query failed: {res.get('reason', 'unknown error')}")


@safe_operation
def recompute_diff_operation(
    freecad: FreeCADConnection,
    doc_name: str,
    obj_name: str,
    expected_volume: float | None = None,
) -> ToolResponse:
    """Recompute and return before/after metrics."""
    res = freecad.recompute_diff(
        doc_name=doc_name, obj_name=obj_name,
        expected_volume=expected_volume,
    )
    if res.get("success"):
        return json_response(res)
    return text_response(f"recompute_diff failed: {res.get('reason', 'unknown error')}")


@safe_operation
def sketch_diagnostics_operation(
    freecad: FreeCADConnection,
    doc_name: str,
    sketch_name: str,
) -> ToolResponse:
    """Return DOF / conflicts / redundancies for a sketch."""
    res = freecad.sketch_diagnostics(
        doc_name=doc_name, sketch_name=sketch_name,
    )
    if res.get("success"):
        return json_response(res)
    return text_response(f"sketch_diagnostics failed: {res.get('reason', 'unknown error')}")


# ---------------------------------------------------------------------------
# v1.1.2 — Multi-instance management
# ---------------------------------------------------------------------------


@safe_operation
def list_freecad_instances_operation(
    freecad: FreeCADConnection,
    max_age_seconds: float = 604800.0,
) -> ToolResponse:
    """List all live FreeCAD instances (UUID, host, port, PID, label)."""
    res = freecad.list_freecad_instances(max_age_seconds=max_age_seconds)
    if res.get("success"):
        return json_response(res)
    return text_response(f"list_freecad_instances failed: {res.get('reason', 'unknown error')}")


@safe_operation
def spawn_freecad_instance_operation(
    freecad: FreeCADConnection,
    label: str | None = None,
    host: str = "localhost",
    port: int = 9875,
    is_headless: bool = False,
    command: str = "",
    freecad_version: str = "unknown",
) -> ToolResponse:
    """Register a new FreeCAD instance and mark it as active."""
    res = freecad.spawn_freecad_instance(
        label=label, host=host, port=port,
        is_headless=is_headless, command=command,
        freecad_version=freecad_version,
    )
    if res.get("success"):
        return json_response(res)
    return text_response(f"spawn_freecad_instance failed: {res.get('reason', 'unknown error')}")


@safe_operation
def select_freecad_instance_operation(
    freecad: FreeCADConnection,
    uuid_str: str,
) -> ToolResponse:
    """Switch the active instance by UUID."""
    res = freecad.select_freecad_instance(uuid_str)
    if res.get("success") is False:
        return text_response(f"select_freecad_instance failed: {res.get('reason', 'unknown error')}")
    return json_response(res)


@safe_operation
def stop_freecad_instance_operation(
    freecad: FreeCADConnection,
    uuid_str: str,
) -> ToolResponse:
    """Unregister an instance from discovery."""
    res = freecad.stop_freecad_instance(uuid_str)
    if res.get("success"):
        return json_response(res)
    return text_response(f"stop_freecad_instance failed: {res.get('reason', 'unknown error')}")


@safe_operation
def instance_status_operation(
    freecad: FreeCADConnection,
    uuid_str: str | None = None,
) -> ToolResponse:
    """Health + latency of an instance (or active if uuid_str is None)."""
    res = freecad.instance_status(uuid_str)
    if res.get("ok") is False and res.get("reason"):
        return text_response(f"instance_status failed: {res.get('reason')}")
    return json_response(res)


# ---------------------------------------------------------------------------
# v1.1.2 — Async execute + job management
# ---------------------------------------------------------------------------


@safe_operation
def execute_code_async_operation(
    freecad: FreeCADConnection,
    code: str,
    label: str = "",
) -> ToolResponse:
    """Submit code to the background runner. Returns job_id."""
    res = freecad.execute_code_async(code=code, label=label)
    if res.get("success"):
        return json_response(res)
    return text_response(f"execute_code_async failed: {res.get('reason', 'unknown error')}")


@safe_operation
def poll_job_operation(
    freecad: FreeCADConnection,
    job_id: str,
) -> ToolResponse:
    """Return status + result for a job."""
    res = freecad.poll_job(job_id)
    if res.get("success"):
        return json_response(res)
    return text_response(f"poll_job failed: {res.get('reason', 'unknown error')}")


@safe_operation
def list_jobs_operation(
    freecad: FreeCADConnection,
    include_terminal: bool = True,
) -> ToolResponse:
    """List all known jobs (running + done + error + cancelled)."""
    res = freecad.list_jobs(include_terminal=include_terminal)
    if res.get("success"):
        return json_response(res)
    return text_response(f"list_jobs failed: {res.get('reason', 'unknown error')}")


@safe_operation
def cancel_job_operation(
    freecad: FreeCADConnection,
    job_id: str,
) -> ToolResponse:
    """Mark a job as cancelled (cooperative)."""
    res = freecad.cancel_job(job_id)
    if res.get("success"):
        return json_response(res)
    return text_response(f"cancel_job failed: {res.get('reason', 'unknown error')}")


# ---------------------------------------------------------------------------
# v1.1.2 — Live API introspection
# ---------------------------------------------------------------------------


@safe_operation
def api_introspect_operation(
    freecad: FreeCADConnection,
    path: str,
) -> ToolResponse:
    """Return signature + docstring of any FreeCAD callable."""
    res = freecad.api_introspect(path=path)
    if res.get("success"):
        return json_response(res)
    return text_response(f"api_introspect failed: {res.get('reason', 'unknown error')}")


@safe_operation
def api_search_operation(
    freecad: FreeCADConnection,
    query: str,
    modules_filter: list[str] | None = None,
    limit: int = 25,
) -> ToolResponse:
    """Search FreeCAD API by name or docstring."""
    res = freecad.api_search(
        query=query, modules_filter=modules_filter, limit=limit,
    )
    if res.get("success"):
        return json_response(res)
    return text_response(f"api_search failed: {res.get('reason', 'unknown error')}")


# ---------------------------------------------------------------------------
# v1.1.2 — CAM / Path toolpath
# ---------------------------------------------------------------------------


@safe_operation
def cam_create_tool_operation(
    freecad: FreeCADConnection,
    doc_name: str,
    name: str,
    tool_type: str = "EndMill",
    diameter: float = 6.0,
    length: float = 50.0,
    material: str = "HighSpeedSteel",
) -> ToolResponse:
    """Create a tool entry in the document's tool library."""
    res = freecad.cam_create_tool(
        doc_name=doc_name, name=name, tool_type=tool_type,
        diameter=diameter, length=length, material=material,
    )
    if res.get("success"):
        return json_response(res)
    return text_response(f"cam_create_tool failed: {res.get('reason', 'unknown error')}")


@safe_operation
def cam_create_tool_controller_operation(
    freecad: FreeCADConnection,
    doc_name: str,
    name: str,
    tool_name: str,
    spindle_speed: float = 12000.0,
    feed_rate: float = 600.0,
    feed_rate_vertical: float = 300.0,
) -> ToolResponse:
    """Create a tool controller with spindle + feed rates."""
    res = freecad.cam_create_tool_controller(
        doc_name=doc_name, name=name, tool_name=tool_name,
        spindle_speed=spindle_speed, feed_rate=feed_rate,
        feed_rate_vertical=feed_rate_vertical,
    )
    if res.get("success"):
        return json_response(res)
    return text_response(f"cam_create_tool_controller failed: {res.get('reason', 'unknown error')}")


@safe_operation
def cam_create_job_operation(
    freecad: FreeCADConnection,
    doc_name: str,
    name: str,
    base_shape: str | None = None,
    tool_controller_name: str | None = None,
    stock_x: float = 100.0,
    stock_y: float = 100.0,
    stock_z: float = 25.0,
) -> ToolResponse:
    """Create a Path::Job with stock + tool controller."""
    res = freecad.cam_create_job(
        doc_name=doc_name, name=name, base_shape=base_shape,
        tool_controller_name=tool_controller_name,
        stock_x=stock_x, stock_y=stock_y, stock_z=stock_z,
    )
    if res.get("success"):
        return json_response(res)
    return text_response(f"cam_create_job failed: {res.get('reason', 'unknown error')}")


@safe_operation
def cam_add_operation_operation(
    freecad: FreeCADConnection,
    doc_name: str,
    job_name: str,
    op_type: str,
    name: str,
    base_shape: str | None = None,
    side: str = "Outside",
    step_down: float = 1.0,
    tool_controller_name: str | None = None,
) -> ToolResponse:
    """Add a Path operation (profile/pocket/adaptive/drilling/face) to a job."""
    res = freecad.cam_add_operation(
        doc_name=doc_name, job_name=job_name, op_type=op_type, name=name,
        base_shape=base_shape, side=side, step_down=step_down,
        tool_controller_name=tool_controller_name,
    )
    if res.get("success"):
        return json_response(res)
    return text_response(f"cam_add_operation failed: {res.get('reason', 'unknown error')}")


@safe_operation
def cam_post_process_operation(
    freecad: FreeCADConnection,
    doc_name: str,
    job_name: str,
    post_processor: str = "linuxcnc",
    output_path: str | None = None,
) -> ToolResponse:
    """Run post-processor on a Path::Job and return the G-code."""
    res = freecad.cam_post_process(
        doc_name=doc_name, job_name=job_name,
        post_processor=post_processor, output_path=output_path,
    )
    if res.get("success"):
        return json_response(res)
    return text_response(f"cam_post_process failed: {res.get('reason', 'unknown error')}")


@safe_operation
def cam_simulate_toolpath_operation(
    freecad: FreeCADConnection,
    doc_name: str,
    job_name: str,
    max_segments: int = 5000,
) -> ToolResponse:
    """Return a downsampled backplot of the tool path."""
    res = freecad.cam_simulate_toolpath(
        doc_name=doc_name, job_name=job_name, max_segments=max_segments,
    )
    if res.get("success"):
        return json_response(res)
    return text_response(f"cam_simulate_toolpath failed: {res.get('reason', 'unknown error')}")
