import logging
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Literal

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ImageContent, TextContent

from .diff import DocumentDiff, diff_documents
from .freecad_client import FreeCADConnection
from .operations import (
    bom_export_operation,
    create_document_operation,
    create_object_operation,
    delete_object_operation,
    edit_object_operation,
    execute_code_operation,
    export_object_operation,
    fem_post_process_operation,
    get_active_view_operation,
    get_object_operation,
    get_objects_operation,
    get_parts_list_operation,
    get_view_operation,
    health_check_operation,
    insert_part_from_library_operation,
    list_documents_operation,
    mesh_import_operation,
    mesh_simplify_operation,
    mesh_to_solid_operation,
    redo_operation,
    run_fem_analysis_operation,
    save_document_operation,
    step_extract_metadata_operation,
    undo_operation,
)
from .profiler import PerformanceProfiler, _profile_decorator, get_profiler
from .prompt_text import ASSET_CREATION_STRATEGY
from .replay import (
    SessionRecorder,
    default_replay_dir,
)
from .responses import ToolResponse
from .streaming import OutputBuffer, ProgressDebouncer
from .tool_policy import (
    ALL_TOOL_NAMES,
    format_policy_for_log,
    resolve_tool_policy,
    validate_elevated_tool_call,
)
from .utils import text_response as _text_response_helper
from .workflows import list_workflows_operation, run_workflow_operation


def _locale_suggests_pt_br() -> bool:
    """Return True if the system locale suggests PT-BR.

    Looks at ``LC_ALL`` / ``LC_MESSAGES`` / ``LANG`` env vars (in that
    priority order) and matches the prefix ``pt``. The check is loose
    on purpose (``pt``, ``pt_BR``, ``pt-BR``, ``pt_PT`` all match) so
    that any Portuguese-speaking operator gets the gabarito without
    having to set a separate env var.

    v1.0.3 — this is a soft default. Operators who want explicit
    control can still use ``FREECAD_MCP_LOAD_GABARITO=1`` (force on)
    or ``FREECAD_MCP_NO_DIRECTIVE_PREFIX=1`` (force off).
    """
    for name in ("LC_ALL", "LC_MESSAGES", "LANG"):
        raw = os.environ.get(name, "").strip()
        if not raw:
            continue
        # POSIX form: ``pt_BR.UTF-8``; Windows form: ``pt-BR``.
        first = raw.replace("-", "_").split("_", 1)[0].lower()
        if first == "pt":
            return True
    return False


def _gabarito_enabled() -> bool:
    """Return True if the gabarito (PT-BR directive set) should be loaded.

    Default since v0.4.0 is OFF. Operators who need the previous
    always-on behaviour set ``FREECAD_MCP_LOAD_GABARITO=1``. The legacy
    ``FREECAD_MCP_NO_DIRECTIVE_PREFIX=1`` still wins as an override and
    forces the gabarito OFF even if the opt-in env var is set, so
    deployments that relied on the old knob to suppress the prefix keep
    working unchanged.

    v1.0.3 — a PT-BR locale (``LC_ALL``/``LC_MESSAGES``/``LANG`` starting
    with ``pt``) flips the default to ON, so a Portuguese-speaking
    operator running ``uvx mcp-freecad`` gets the gabarito without
    extra configuration. Explicit env vars always win over the
    locale-based default.
    """
    if os.environ.get("FREECAD_MCP_NO_DIRECTIVE_PREFIX", "").strip().lower() in {"1", "true", "yes", "on"}:
        return False
    explicit = os.environ.get("FREECAD_MCP_LOAD_GABARITO", "").strip().lower() in {"1", "true", "yes", "on"}
    if explicit:
        return True
    return _locale_suggests_pt_br()


def _load_system_directives() -> str:
    """Load system-level directives from docs/gabarito_ia_extracted.txt if present.

    Opt-in since v0.4.0 — see :func:`_gabarito_enabled`. When disabled,
    returns a short English fallback so the MCP server has *something*
    to put in ``instructions=`` but no Portuguese text leaks into
    English-language deployments.
    """
    if not _gabarito_enabled():
        return (
            "FreeCAD integration through the Model Context Protocol. "
            "Use the provided tools to drive FreeCAD; do not invent tool names."
        )
    # Use repository root as base (two levels up from this file: src/freecad_mcp)
    p = Path(__file__).resolve().parents[2] / "docs" / "gabarito_ia_extracted.txt"
    try:
        if p.exists():
            return p.read_text(encoding="utf-8")
    except Exception:
        # We haven't configured logging yet here; fall back silently
        pass
    return "FreeCAD integration through the Model Context Protocol"


def configure_logging() -> None:
    """Configure root logging with console and rotating file handlers.

    Idempotent: re-importing or reloading the module will not stack duplicate
    handlers (which would otherwise inflate logs and confuse rotation).

    v0.4.0: ``FREECAD_MCP_LOG_FORMAT=json`` switches to a JSON line
    formatter (one record per line) suitable for ingestion by log
    shippers (Loki, Elasticsearch, CloudWatch). The default remains
    the human-readable text format.
    """
    root = logging.getLogger()
    if getattr(root, "_freecad_mcp_configured", False):
        return

    log_level_name = os.getenv("FREECAD_MCP_LOGLEVEL", "INFO").upper()
    level = getattr(logging, log_level_name, logging.INFO)

    log_format = os.getenv("FREECAD_MCP_LOG_FORMAT", "text").strip().lower()
    if log_format == "json":
        from .json_logging import JsonLogFormatter
        formatter: logging.Formatter = JsonLogFormatter()
    else:
        formatter = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s", "%Y-%m-%dT%H:%M:%SZ"
        )

    root.setLevel(level)

    # Console handler
    ch = logging.StreamHandler()
    ch.setLevel(level)
    ch.setFormatter(formatter)
    root.addHandler(ch)

    # File handler (rotating)
    try:
        log_dir = Path(__file__).resolve().parents[2] / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(log_dir / "freecad_mcp.log", maxBytes=5 * 1024 * 1024, backupCount=3)
        fh.setLevel(level)
        fh.setFormatter(formatter)
        root.addHandler(fh)
    except Exception:
        # If file handler cannot be created, continue with console only
        pass

    root._freecad_mcp_configured = True


configure_logging()
from .server_state import ServerState  # noqa: E402 — after configure_logging on purpose

logger = logging.getLogger("FreeCADMCPserver")

state = ServerState()
recorder = SessionRecorder.new()

# Tool policy resolved once at import time. Operators control it via
# ``FREECAD_MCP_DISABLED_TOOLS`` (denylist) or ``FREECAD_MCP_REQUIRED_TOOLS``
# (whitelist); see ``src/freecad_mcp/tool_policy.py`` for the contract.
try:
    _tool_policy = resolve_tool_policy()
except ValueError as _policy_err:
    # Fail fast on misconfiguration: a typo in an env var should never
    # silently flip the policy. We can't use logger yet at this point
    # in some import paths, so write directly to stderr.
    import sys
    print(f"FATAL: {_policy_err}", file=sys.stderr)
    raise SystemExit(2) from _policy_err
logger.info(format_policy_for_log(_tool_policy))


def _guard_tool(tool_name: str):
    """Decorator that enforces the tool policy AND the elevated-tool auth gate.

    Two-stage guard, in this order:

    1. Tool policy (FREECAD_MCP_DISABLED_TOOLS /
       FREECAD_MCP_REQUIRED_TOOLS) — disables the tool entirely.
    2. Elevated-tool auth — when *tool_name* is in
       :data:`tool_policy.ELEVATED_TOOLS`, the operator must have
       enabled the feature (``FREECAD_MCP_ALLOW_ELEVATED_TOOLS=1``)
       AND the live FreeCADConnection must have a bearer token
       configured via :meth:`FreeCADConnection.set_bearer_token`.
       We probe this *here* (before delegating to the tool body) so
       the LLM sees a clear, unified error message instead of a
       protocol-level RPC failure.

    Disabled tools return a ``text_response`` with an actionable error
    so the LLM gets a clear signal that the tool is unavailable (and
    why), rather than an opaque protocol error.

    Use as the OUTER decorator — i.e. ``@_guard_tool("foo")`` above
    ``@mcp.tool()`` — so the FastMCP layer sees the wrapped (guarded)
    function and the original function only runs when the policy
    allows it.
    """
    from functools import wraps

    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if tool_name not in _tool_policy.enabled:
                msg = (
                    f"Tool '{tool_name}' is disabled by the server's tool policy. "
                    "Either remove it from the request or ask the operator to "
                    "enable it via FREECAD_MCP_DISABLED_TOOLS / FREECAD_MCP_REQUIRED_TOOLS."
                )
                logger.warning("blocked call to disabled tool: %s", tool_name)
                return _text_response_helper(msg)
            # Elevated-tool auth gate (only when the tool is enabled).
            conn = state.freecad_connection
            has_token = bool(conn and conn._bearer_token)
            reason = validate_elevated_tool_call(tool_name, has_token)
            if reason is not None:
                logger.warning("blocked elevated tool %s: %s", tool_name, reason)
                return _text_response_helper(reason)
            return fn(*args, **kwargs)

        return wrapper

    return decorator


def _observe_tool_call(tool_name: str, args: dict[str, Any]) -> Any:
    """Record tool args into the in-process replay buffer.

    Called from tool wrappers *before* the body runs so the replay log
    captures both successful and aborted invocations. Failures here
    are swallowed — the replay recorder must never break a tool call.
    """
    try:
        recorder.record(tool_name, args or {}, "<in-flight>")
    except Exception:
        logger.debug("recorder failed for %s", tool_name, exc_info=True)


def _finalize_tool_call(tool_name: str, args: dict[str, Any], result: Any) -> Any:
    """Update the most recent replay entry with the final result."""
    try:
        recorder.record(tool_name, args or {}, result)
    except Exception:
        logger.debug("recorder finalize failed for %s", tool_name, exc_info=True)
    return result


def profile_tool(fn):
    """Decorator that records elapsed time via the in-process profiler.

    The decorator wraps the FastMCP-visible function so each tool call
    is observed by :func:`freecad_mcp.profiler.get_profiler`. Combine
    with ``@_guard_tool`` and ``@mcp.tool()`` in any order — the
    profiler only inspects timing.
    """
    return _profile_decorator(fn)


@asynccontextmanager
async def server_lifespan(server: FastMCP) -> AsyncIterator[dict[str, Any]]:
    try:
        logger.info("FreeCADMCP server starting up")
        try:
            _ = get_freecad_connection()
            logger.info("Successfully connected to FreeCAD on startup")
        except Exception as e:
            logger.warning(f"Could not connect to FreeCAD on startup: {str(e)}")
            logger.warning(
                "Make sure the FreeCAD addon is running before using FreeCAD resources or tools"
            )
        yield {}
    finally:
        if state.freecad_connection:
            logger.info("Disconnecting from FreeCAD on shutdown")
            state.freecad_connection.disconnect()
            state.freecad_connection = None
        logger.info("FreeCADMCP server shut down")


mcp_instructions = _load_system_directives()
if ASSET_CREATION_STRATEGY:
    mcp_instructions = mcp_instructions + "\n\n" + ASSET_CREATION_STRATEGY

# Cap the instructions to keep token cost predictable across long sessions.
# Default 8KB — well under Claude's 200K context but large enough to fit
# the gabarito (≈2.6KB) plus the asset strategy (≈1KB) plus headroom for
# future additions. Override via env if you need more.
_MAX_INSTRUCTIONS_CHARS = int(os.environ.get("FREECAD_MCP_MAX_INSTRUCTIONS_CHARS", "8192"))
if len(mcp_instructions) > _MAX_INSTRUCTIONS_CHARS:
    logger.warning(
        f"mcp_instructions is {len(mcp_instructions)} chars; truncating to {_MAX_INSTRUCTIONS_CHARS}. "
        "Set FREECAD_MCP_MAX_INSTRUCTIONS_CHARS to adjust."
    )
    mcp_instructions = mcp_instructions[:_MAX_INSTRUCTIONS_CHARS]
logger.info(f"mcp_instructions size: {len(mcp_instructions)} chars (cap {_MAX_INSTRUCTIONS_CHARS})")

mcp = FastMCP(
    "FreeCADMCP",
    instructions=mcp_instructions,
    lifespan=server_lifespan,
)


# Re-validate the cached connection at most this often. The default
# ``ping()`` is a single round-trip on the local socket; cheap enough
# that once-per-N-seconds is invisible to users but stops a stale
# connection from hanging the first call after FreeCAD restart.
_LIVENESS_CHECK_S = float(os.environ.get("FREECAD_MCP_LIVENESS_CHECK_S", "30"))
_LIVENESS_LAST_OK: dict[str, float] = {}


def get_freecad_connection() -> FreeCADConnection:
    """Get or create a persistent FreeCAD connection.

    Probes the connection with a ``ping()`` at most every
    ``FREECAD_MCP_LIVENESS_CHECK_S`` seconds (default 30). Without this
    check, a stale connection from a previous FreeCAD session would
    hang every tool call until the breaker finally opened.
    """
    now = time.monotonic()
    last_ok = _LIVENESS_LAST_OK.get("t", 0.0)
    if (
        state.freecad_connection is not None
        and (now - last_ok) >= _LIVENESS_CHECK_S
        and not state.freecad_connection.ping()
    ):
        logger.warning(
            "FreeCAD connection stale; reconnecting (last ok %.1fs ago)",
            now - last_ok,
        )
        with suppress(Exception):
            state.freecad_connection.disconnect()
        state.freecad_connection = None

    if state.freecad_connection is None:
        state.freecad_connection = FreeCADConnection(host=state.rpc_host, port=9875)
        if not state.freecad_connection.ping():
            logger.error("Failed to ping FreeCAD")
            state.freecad_connection = None
            raise Exception(
                "Failed to connect to FreeCAD. Make sure the FreeCAD addon is running."
            )

    _LIVENESS_LAST_OK["t"] = now
    return state.freecad_connection


@_guard_tool("create_document")
@mcp.tool()
def create_document(ctx: Context, name: str) -> list[TextContent]:
    """Create a new document in FreeCAD.

    Args:
        name: The name of the document to create.

    Returns:
        A message indicating the success or failure of the document creation.

    Examples:
        If you want to create a document named "MyDocument", you can use the following data.
        ```json
        {
            "name": "MyDocument"
        }
        ```
    """
    return create_document_operation(get_freecad_connection(), name)


@_guard_tool("create_object")
@mcp.tool()
def create_object(
    ctx: Context,
    doc_name: str,
    obj_type: str,
    obj_name: str,
    analysis_name: str | None = None,
    obj_properties: dict[str, Any] | None = None,
) -> list[TextContent | ImageContent]:
    """Create a new object in FreeCAD.
    Object type is starts with "Part::" or "Draft::" or "PartDesign::" or "Fem::".

    Args:
        doc_name: The name of the document to create the object in.
        obj_type: The type of the object to create (e.g. 'Part::Box', 'Part::Cylinder', 'Draft::Circle', 'PartDesign::Body', etc.).
        obj_name: The name of the object to create.
        obj_properties: The properties of the object to create.

    Returns:
        A message indicating the success or failure of the object creation and a screenshot of the object.

    Examples:
        If you want to create a cylinder with a height of 30 and a radius of 10, you can use the following data.
        ```json
        {
            "doc_name": "MyCylinder",
            "obj_name": "Cylinder",
            "obj_type": "Part::Cylinder",
            "obj_properties": {
                "Height": 30,
                "Radius": 10,
                "Placement": {
                    "Base": {
                        "x": 10,
                        "y": 10,
                        "z": 0
                    },
                    "Rotation": {
                        "Axis": {
                            "x": 0,
                            "y": 0,
                            "z": 1
                        },
                        "Angle": 45
                    }
                },
                "ViewObject": {
                    "ShapeColor": [0.5, 0.5, 0.5, 1.0]
                }
            }
        }
        ```

        If you want to create a circle with a radius of 10, you can use the following data.
        ```json
        {
            "doc_name": "MyCircle",
            "obj_name": "Circle",
            "obj_type": "Draft::Circle",
        }
        ```

        If you want to create a FEM analysis, you can use the following data.
        ```json
        {
            "doc_name": "MyFEMAnalysis",
            "obj_name": "FemAnalysis",
            "obj_type": "Fem::AnalysisPython",
        }
        ```

        If you want to create a FEM constraint, you can use the following data.
        ```json
        {
            "doc_name": "MyFEMConstraint",
            "obj_name": "FemConstraint",
            "obj_type": "Fem::ConstraintFixed",
            "analysis_name": "MyFEMAnalysis",
            "obj_properties": {
                "References": [
                    {
                        "object_name": "MyObject",
                        "face": "Face1"
                    }
                ]
            }
        }
        ```

        If you want to create a FEM mechanical material, you can use the following data.
        ```json
        {
            "doc_name": "MyFEMAnalysis",
            "obj_name": "FemMechanicalMaterial",
            "obj_type": "Fem::MaterialCommon",
            "analysis_name": "MyFEMAnalysis",
            "obj_properties": {
                "Material": {
                    "Name": "MyMaterial",
                    "Density": "7900 kg/m^3",
                    "YoungModulus": "210 GPa",
                    "PoissonRatio": 0.3
                }
            }
        }
        ```

        If you want to create a FEM mesh, you can use the following data.
        The `Shape` property is required (legacy `Part` is also accepted).
        On FreeCAD 1.x the size limits are `CharacteristicLengthMax/Min`;
        the legacy `ElementSizeMax/Min` keys are also accepted.
        ```json
        {
            "doc_name": "MyFEMMesh",
            "obj_name": "FemMesh",
            "obj_type": "Fem::FemMeshGmsh",
            "analysis_name": "MyFEMAnalysis",
            "obj_properties": {
                "Shape": "MyObject",
                "CharacteristicLengthMax": 10,
                "CharacteristicLengthMin": 0.1
            }
        }
        ```
    """
    return create_object_operation(
        get_freecad_connection(),
        state.only_text_feedback,
        doc_name,
        obj_type,
        obj_name,
        analysis_name,
        obj_properties,
    )


@_guard_tool("edit_object")
@mcp.tool()
def edit_object(
    ctx: Context, doc_name: str, obj_name: str, obj_properties: dict[str, Any]
) -> list[TextContent | ImageContent]:
    """Edit an object in FreeCAD.
    This tool is used when the `create_object` tool cannot handle the object creation.

    Args:
        doc_name: The name of the document to edit the object in.
        obj_name: The name of the object to edit.
        obj_properties: The properties of the object to edit.

    Returns:
        A message indicating the success or failure of the object editing and a screenshot of the object.
    """
    return edit_object_operation(
        get_freecad_connection(),
        state.only_text_feedback,
        doc_name,
        obj_name,
        obj_properties,
    )


@_guard_tool("delete_object")
@mcp.tool()
def delete_object(ctx: Context, doc_name: str, obj_name: str) -> list[TextContent | ImageContent]:
    """Delete an object in FreeCAD.

    Args:
        doc_name: The name of the document to delete the object from.
        obj_name: The name of the object to delete.

    Returns:
        A message indicating the success or failure of the object deletion and a screenshot of the object.
    """
    return delete_object_operation(
        get_freecad_connection(),
        state.only_text_feedback,
        doc_name,
        obj_name,
    )


@profile_tool
@_guard_tool("execute_code")
@mcp.tool()
def execute_code(ctx: Context, code: str) -> list[TextContent | ImageContent]:
    """Execute arbitrary Python code in FreeCAD.

    Args:
        code: The Python code to execute.

    Returns:
        A message indicating the success or failure of the code execution, the output of the code execution, and a screenshot of the object.
    """
    _observe_tool_call("execute_code", {"code": code})
    conn = get_freecad_connection()
    buffer = OutputBuffer()
    debouncer = ProgressDebouncer(min_interval_s=0.1)
    try:
        result = execute_code_operation(conn, state.only_text_feedback, code)
    except Exception as e:
        buffer.ingest({"success": False, "error": str(e)})
        _finalize_tool_call("execute_code", {"code": code}, buffer.full_output())
        raise
    if isinstance(result, list):
        for item in result:
            text = getattr(item, "text", None)
            if isinstance(text, str):
                buffer.ingest({"success": True, "message": text})
                break
    else:
        buffer.ingest({"success": True, "message": str(result)})
    if debouncer.should_emit():
        debouncer.mark_emitted()
    _finalize_tool_call("execute_code", {"code": code}, buffer.full_output())
    return result


@_guard_tool("get_view")
@mcp.tool()
def get_view(
    ctx: Context,
    view_name: Literal["Isometric", "Front", "Top", "Right", "Back", "Left", "Bottom", "Dimetric", "Trimetric"],
    width: int | None = None,
    height: int | None = None,
    focus_object: str | None = None,
    image_format: str = "png",
) -> list[ImageContent | TextContent]:
    """Get a screenshot of the active view.

    Args:
        view_name: The name of the view to get the screenshot of.
        The following views are available:
        - "Isometric"
        - "Front"
        - "Top"
        - "Right"
        - "Back"
        - "Left"
        - "Bottom"
        - "Dimetric"
        - "Trimetric"
        width: The width of the screenshot in pixels. If not specified, uses the viewport width.
        height: The height of the screenshot in pixels. If not specified, uses the viewport height.
        focus_object: The name of the object to focus on. If not specified, fits all objects in the view.
        image_format: One of ``png`` (default, no extra dependency), ``jpeg``/``jpg``,
            or ``webp``. JPEG/WebP require Pillow on the FreeCAD host.

    Returns:
        A screenshot of the active view in the requested format.
    """
    return get_view_operation(get_freecad_connection(), view_name, width, height, focus_object, image_format)


@_guard_tool("insert_part_from_library")
@mcp.tool()
def insert_part_from_library(ctx: Context, relative_path: str) -> list[TextContent | ImageContent]:
    """Insert a part from the parts library addon.

    Args:
        relative_path: The relative path of the part to insert.

    Returns:
        A message indicating the success or failure of the part insertion and a screenshot of the object.
    """
    return insert_part_from_library_operation(
        get_freecad_connection(),
        state.only_text_feedback,
        relative_path,
    )


@_guard_tool("get_objects")
@mcp.tool()
def get_objects(ctx: Context, doc_name: str) -> list[TextContent | ImageContent]:
    """Get all objects in a document.
    You can use this tool to get the objects in a document to see what you can check or edit.

    Args:
        doc_name: The name of the document to get the objects from.

    Returns:
        A list of objects in the document and a screenshot of the document.
    """
    return get_objects_operation(get_freecad_connection(), state.only_text_feedback, doc_name)


@_guard_tool("get_object")
@mcp.tool()
def get_object(ctx: Context, doc_name: str, obj_name: str) -> list[TextContent | ImageContent]:
    """Get an object from a document.
    You can use this tool to get the properties of an object to see what you can check or edit.

    Args:
        doc_name: The name of the document to get the object from.
        obj_name: The name of the object to get.

    Returns:
        The object and a screenshot of the object.
    """
    return get_object_operation(
        get_freecad_connection(),
        state.only_text_feedback,
        doc_name,
        obj_name,
    )


@_guard_tool("get_parts_list")
@mcp.tool()
def get_parts_list(ctx: Context) -> list[TextContent]:
    """Get the list of parts in the parts library addon.
    """
    return get_parts_list_operation(get_freecad_connection())


@profile_tool
@_guard_tool("mesh_import")
@mcp.tool()
def mesh_import(
    ctx: Context,
    path: str,
    doc_name: str | None = None,
    label: str | None = None,
) -> ToolResponse:
    """Import a mesh file (.stl/.obj/.ply/.off/.mesh/.smf/.wrl/.3ds/.dae)
    into the FreeCAD document as a ``Mesh::Feature``.

    Args:
        path: Absolute path to the mesh file.
        doc_name: Target document name (defaults to the active one).
        label: Object label (defaults to the file stem).

    Returns:
        A JSON object with ``success``, ``object_name``, ``label``,
        ``triangle_count`` and ``vertex_count``.
    """
    return mesh_import_operation(
        get_freecad_connection(), path=path, doc_name=doc_name, label=label
    )


@profile_tool
@_guard_tool("mesh_simplify")
@mcp.tool()
def mesh_simplify(
    ctx: Context,
    doc_name: str,
    mesh_name: str,
    target_faces: int = 5_000,
) -> ToolResponse:
    """Decimate a ``Mesh::Feature`` using quadric edge-collapse.

    Args:
        doc_name: Document that owns the mesh.
        mesh_name: Name of the ``Mesh::Feature`` to simplify.
        target_faces: Desired triangle count after decimation
            (approximate).

    Returns:
        A JSON object with ``success``, ``triangle_count_before``,
        ``triangle_count_after``, ``reduction_pct`` and optionally
        ``skipped: True`` when the mesh is already small enough.
    """
    return mesh_simplify_operation(
        get_freecad_connection(),
        doc_name=doc_name,
        mesh_name=mesh_name,
        target_faces=target_faces,
    )


@profile_tool
@_guard_tool("mesh_to_solid")
@mcp.tool()
def mesh_to_solid(
    ctx: Context,
    doc_name: str,
    mesh_name: str,
    new_name: str | None = None,
    repair: bool = True,
    sew_tolerance: float = 1e-3,
    max_triangles_before_simplify: int = 50_000,
    target_faces_after_simplify: int = 5_000,
) -> ToolResponse:
    """Convert a ``Mesh::Feature`` into a parametric ``Part::Feature`` solid.

    This is the inverse-modeling pipeline: triangles → coplanar faces
    → sewn shell → solid. The resulting object is editable in the
    FreeCAD GUI (move faces, change dimensions, apply booleans, run
    FEM).

    Curved surfaces are recovered as triangle-facet approximations;
    this tool cannot recover the original NURBS from a mesh.

    Args:
        doc_name: Document that owns the source mesh.
        mesh_name: Name of the ``Mesh::Feature`` to convert.
        new_name: Name of the resulting ``Part::Feature``. Defaults
            to ``f"{mesh_name}_Solid"``.
        repair: Run ``Part.fix`` + ``Shape.sewShape`` after
            construction (highly recommended).
        sew_tolerance: Sewing tolerance; smaller = stricter.
        max_triangles_before_simplify: If the mesh has more triangles
            than this, decimate first.
        target_faces_after_simplify: Decimation target.

    Returns:
        A JSON object with ``success``, ``object_name``, ``shell_faces``,
        ``solid`` (bool), ``volume``, ``triangle_count``,
        ``decimated`` and ``repair_applied``.
    """
    return mesh_to_solid_operation(
        get_freecad_connection(),
        doc_name=doc_name,
        mesh_name=mesh_name,
        new_name=new_name,
        repair=repair,
        sew_tolerance=sew_tolerance,
        max_triangles_before_simplify=max_triangles_before_simplify,
        target_faces_after_simplify=target_faces_after_simplify,
    )


@_guard_tool("list_documents")
@mcp.tool()
def list_documents(ctx: Context) -> list[TextContent]:
    """Get the list of open documents in FreeCAD.

    Returns:
        A list of document names.
    """
    return list_documents_operation(get_freecad_connection())


@profile_tool
@_guard_tool("step_extract_metadata")
@mcp.tool()
def step_extract_metadata(ctx: Context, path: str) -> ToolResponse:
    """Extract AP214 metadata from a STEP Part 21 file (``.step`` / ``.stp``).

    Reads the file directly (no FreeCAD subprocess needed) and
    returns the structured ``FILE_DESCRIPTION`` / ``FILE_SCHEMA``
    cards plus the most common HEADER fields (name, author,
    organization, preprocessor_version, originating_system,
    authorization).

    Args:
        path: Absolute path to a ``.step`` or ``.stp`` file.

    Returns:
        A JSON object with the metadata fields above.
    """
    return step_extract_metadata_operation(get_freecad_connection(), path=path)


@profile_tool
@_guard_tool("bom_export")
@mcp.tool()
def bom_export(
    ctx: Context,
    doc_name: str,
    fmt: str = "json",
    include_extras: bool = False,
    group_by_type: bool = True,
) -> ToolResponse:
    """Export a Bill of Materials for *doc_name*.

    Args:
        doc_name: Document to introspect.
        fmt: ``"json"`` (default) or ``"csv"``.
        include_extras: Include non-dimension properties (JSON only).
        group_by_type: When True (default), identical parts are
            collapsed and ``quantity`` increments.

    Returns:
        A JSON object (``fmt="json"``) or raw CSV text (``fmt="csv"``).
    """
    return bom_export_operation(
        get_freecad_connection(),
        doc_name=doc_name,
        fmt=fmt,
        include_extras=include_extras,
        group_by_type=group_by_type,
    )


@profile_tool
@_guard_tool("fem_post_process")
@mcp.tool()
def fem_post_process(ctx: Context, path: str) -> ToolResponse:
    """Parse a CalculiX ``.frd`` result file and extract numerical data.

    Returns per-node displacements (with the |U| vector magnitude)
    and per-element stresses (including the von Mises value), plus
    a summary block with max/min/mean displacement and max von
    Mises stress.

    PNG contour plots are not produced in this version (see v1.4
    roadmap); the LLM gets a numerical table it can reason about.

    Args:
        path: Absolute path to a ``.frd`` file written by CalculiX.

    Returns:
        A JSON object with ``success``, ``step``, ``node_count``,
        ``displacement_count``, ``stress_count``, ``summary`` and
        the worst-case node and element entries.
    """
    return fem_post_process_operation(get_freecad_connection(), path=path)


@_guard_tool("run_fem_analysis")
@mcp.tool()
def run_fem_analysis(
    ctx: Context,
    doc_name: str,
    analysis_name: str,
    timeout: int = 600,
) -> list[TextContent | ImageContent]:
    """Run the CalculiX solver on an existing Fem::FemAnalysis container and return summary results.

    Prerequisites in the document:
    - A Part-derived solid (e.g. Part::Box, PartDesign::Body) acting as the geometry.
    - A Fem::AnalysisPython container created via `create_object`.
    - A Fem::MaterialCommon assigned to the geometry, added to the analysis.
    - A Fem::FemMeshGmsh referencing the geometry, added to the analysis (the
      mesh is generated automatically when created via `create_object`).
    - At least one Fem::ConstraintFixed and one Fem::ConstraintForce (or
      ConstraintPressure) bound to faces of the geometry, added to the analysis.

    A SolverCcxTools is auto-created if the analysis has none.

    The solver runs synchronously on the FreeCAD GUI thread and blocks all
    other RPC calls for its duration; do not fan out parallel requests.

    Returns max von Mises stress (MPa), max/min displacement (mm), node count,
    and the working directory CalculiX wrote to. On failure, returns the
    prerequisite-check or solver error along with the working directory for
    triage.

    Args:
        doc_name: Name of the FreeCAD document.
        analysis_name: Name of the Fem::AnalysisPython object.
        timeout: Seconds to wait for the solver (default 600).
    """
    return run_fem_analysis_operation(
        get_freecad_connection(),
        state.only_text_feedback,
        doc_name,
        analysis_name,
        timeout,
    )


@_guard_tool("undo")
@mcp.tool()
def undo(ctx: Context, doc_name: str, steps: int = 1) -> list[TextContent | ImageContent]:
    """Undo one or more transactions in a FreeCAD document.

    Args:
        doc_name: Name of the FreeCAD document.
        steps: How many transactions to undo (default 1).

    Returns:
        A message reporting the number of transactions undone.
    """
    return undo_operation(get_freecad_connection(), doc_name, steps)


@_guard_tool("redo")
@mcp.tool()
def redo(ctx: Context, doc_name: str, steps: int = 1) -> list[TextContent | ImageContent]:
    """Redo one or more previously-undone transactions in a FreeCAD document.

    Args:
        doc_name: Name of the FreeCAD document.
        steps: How many transactions to redo (default 1).

    Returns:
        A message reporting the number of transactions redone.
    """
    return redo_operation(get_freecad_connection(), doc_name, steps)


@_guard_tool("save_document")
@mcp.tool()
def save_document(ctx: Context, doc_name: str, path: str | None = None) -> list[TextContent | ImageContent]:
    """Save a FreeCAD document to disk.

    Args:
        doc_name: Name of the FreeCAD document.
        path: Destination file path. If omitted, saves to the document's
            current file path (FCStd).

    Returns:
        A message reporting success and the saved path.
    """
    return save_document_operation(get_freecad_connection(), doc_name, path)


@_guard_tool("export_object")
@mcp.tool()
def export_object(
    ctx: Context,
    doc_name: str,
    obj_name: str,
    path: str,
    fmt: str | None = None,
) -> list[TextContent | ImageContent]:
    """Export a single object from a FreeCAD document to a file.

    Args:
        doc_name: Name of the FreeCAD document.
        obj_name: Name of the object inside the document.
        path: Destination file path. The extension determines the
            format if ``fmt`` is not given.
        fmt: Optional explicit format (``stl``, ``step``, ``iges``,
            ``obj``, ...). Overrides the extension inference.

    Returns:
        A message reporting success and the format written.
    """
    return export_object_operation(get_freecad_connection(), doc_name, obj_name, path, fmt)


@_guard_tool("get_active_view")
@mcp.tool()
def get_active_view(ctx: Context) -> list[TextContent | ImageContent]:
    """Return metadata about the currently active FreeCAD view.

    Useful before calling `get_view` to check whether a screenshot is
    possible, or to inspect the current rendering target.

    Returns:
        A JSON object with view_type, width, height, has_save_image.
    """
    return get_active_view_operation(get_freecad_connection())


@_guard_tool("health_check")
@mcp.tool()
def health_check(ctx: Context) -> list[TextContent | ImageContent]:
    """Lightweight liveness/readiness probe for monitoring.

    Returns the server's uptime, queue sizes, cached-response count,
    and the resolved settings directory. Safe to call repeatedly.

    Returns:
        A JSON object with diagnostic fields.
    """
    return health_check_operation(get_freecad_connection(), state.metrics)


# ----------------------------------------------------------------------------
# v1.1.0 — New high-value features
# ----------------------------------------------------------------------------


@mcp.tool()
def diff_documents_tool(ctx: Context, doc_a: str, doc_b: str) -> ToolResponse:
    """Compute a structured diff between two FreeCAD documents.

    Args:
        doc_a: First document name.
        doc_b: Second document name.

    Returns:
        A JSON document with per-object diff categories.
    """
    diff: DocumentDiff = diff_documents(get_freecad_connection(), doc_a, doc_b)
    import json as _json

    payload = diff.as_dict(detailed=True)
    return _text_response_helper(_json.dumps(payload, ensure_ascii=False, indent=2))


@mcp.tool()
def list_workflows(ctx: Context) -> ToolResponse:
    """List all registered workflows (built-in + custom).

    Returns:
        A JSON array of ``{name, description, step_count}`` entries.
    """
    return list_workflows_operation()


@mcp.tool()
def run_workflow(
    ctx: Context,
    name: str,
    args: dict[str, Any] | None = None,
) -> ToolResponse:
    """Execute a registered workflow by name.

    Args:
        name: Workflow name (from ``list_workflows``).
        args: Initial argument dictionary used to expand ``{var}``
            placeholders in each step's args template.

    Returns:
        A JSON object with the workflow name and per-step results.
    """
    _observe_tool_call("run_workflow", {"name": name, "args": args or {}})
    result = run_workflow_operation(get_freecad_connection(), name, args or {})
    _finalize_tool_call("run_workflow", {"name": name, "args": args or {}}, result)
    return result


@mcp.tool()
def get_profiler_stats(ctx: Context) -> ToolResponse:
    """Return per-tool percentile stats from the in-process profiler.

    Returns:
        A JSON object mapping tool name to ``{count, mean_ms, p50_ms,
        p95_ms, p99_ms, max_ms}``.
    """
    import json as _json

    profiler: PerformanceProfiler = get_profiler()
    stats = profiler.get_stats()
    payload = {
        "buffer_size": len(profiler),
        "buffer_max": profiler.max_entries,
        "slow_threshold_ms": profiler.slow_threshold_ms,
        "stats": stats,
        "flamegraph": profiler.export_flamegraph_data(),
    }
    return _text_response_helper(_json.dumps(payload, ensure_ascii=False, indent=2))


@mcp.tool()
def list_replays(ctx: Context) -> ToolResponse:
    """List every recorded session replay available on disk.

    Returns:
        A JSON array of ``{session_id, path, step_count, size_bytes}``.
    """
    import json as _json

    from .replay import SessionRecorder

    base = default_replay_dir()
    entries: list[dict[str, Any]] = []
    if base.exists():
        for child in sorted(base.glob("*.json")):
            sid = child.stem
            try:
                rec = SessionRecorder.load(sid)
                count = len(rec.steps)
            except Exception:
                count = 0
            entries.append({
                "session_id": sid,
                "path": str(child),
                "step_count": count,
                "size_bytes": child.stat().st_size,
            })
    return _text_response_helper(
        _json.dumps({"count": len(entries), "replays": entries}, indent=2)
    )


@mcp.tool()
def get_replay(
    ctx: Context,
    session_id: str,
    format: str = "json",
    dry_run: bool = True,
) -> ToolResponse:
    """Fetch a recorded session replay.

    Args:
        session_id: Replay identifier (from ``list_replays``).
        format: Either ``"json"`` or ``"markdown"``.
        dry_run: For ``format="replay"``, whether to skip destructive
            tool calls (default ``True``). Has no effect on
            ``"json"``/``"markdown"`` formats.

    Returns:
        The replay contents as text (JSON or Markdown) plus, for the
        ``"replay"`` format, a per-step replay report.
    """
    from .replay import SessionRecorder

    rec = SessionRecorder.load(session_id)
    fmt = format.lower()
    if fmt == "json":
        return _text_response_helper(rec.export_json())
    if fmt == "markdown":
        return _text_response_helper(rec.export_markdown())
    if fmt == "replay":
        results = rec.replay(
            get_freecad_connection(),
            dry_run=dry_run,
        )
        import json as _json

        return _text_response_helper(
            _json.dumps(
                {
                    "session_id": session_id,
                    "dry_run": dry_run,
                    "results": [r.to_dict() for r in results],
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    return _text_response_helper(f"unknown format: {format!r}; use json|markdown|replay")


# ----------------------------------------------------------------------------
# v1.1.0 — MCP resources (read-only data surfaces)
# ----------------------------------------------------------------------------


@mcp.resource("freecad://server/policy")
def resource_server_policy() -> str:
    """Return the currently effective tool policy (enabled set + gate)."""
    import json as _json

    payload = {
        "enabled": sorted(_tool_policy.enabled),
        "denied": sorted(_tool_policy.denied),
        "elevated_enabled": _tool_policy.elevated_enabled,
        "elevated_tools": sorted(_tool_policy.elevated_tools),
        "all_tool_names": sorted(ALL_TOOL_NAMES),
    }
    return _json.dumps(payload, indent=2)


@mcp.resource("freecad://server/metrics")
def resource_server_metrics() -> str:
    """Return a snapshot of the in-process Prometheus-style metrics."""
    return state.metrics.snapshot_text()


@mcp.resource("freecad://server/profiler")
def resource_server_profiler() -> str:
    """Return the profiler's per-tool stats as JSON."""
    return get_profiler_stats(ctx=None)[0].text  # type: ignore[index]


@mcp.resource("freecad://server/replay-dir")
def resource_replay_dir() -> str:
    """Return the on-disk directory used to persist session replays."""
    return str(default_replay_dir())


@mcp.prompt()
def asset_creation_strategy() -> str:
    return ASSET_CREATION_STRATEGY


def _validate_host(value: str) -> str:
    """Validate that *value* is a valid IP address or hostname.

    Used as the ``type`` callback for the ``--host`` argparse argument.
    Raises ``argparse.ArgumentTypeError`` on invalid input.
    """
    import argparse

    import validators

    if validators.ipv4(value) or validators.ipv6(value) or validators.hostname(value):
        return value
    raise argparse.ArgumentTypeError(
        f"Invalid host: '{value}'. Must be a valid IP address or hostname."
    )


def main():
    """Run the MCP server"""
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--only-text-feedback", action="store_true", help="Only return text feedback")
    parser.add_argument("--host", type=_validate_host, default="localhost", help="Host address of the FreeCAD RPC server to connect to (default: localhost)")
    args = parser.parse_args()
    state.only_text_feedback = args.only_text_feedback
    state.rpc_host = args.host
    logger.info(f"Only text feedback: {state.only_text_feedback}")
    logger.info(f"Connecting to FreeCAD RPC server at: {state.rpc_host}")
    mcp.run()
