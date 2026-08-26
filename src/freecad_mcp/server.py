import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Literal

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ImageContent, TextContent

from .freecad_client import FreeCADConnection
from .operations import (
    create_document_operation,
    create_object_operation,
    delete_object_operation,
    edit_object_operation,
    execute_code_operation,
    export_object_operation,
    get_active_view_operation,
    get_object_operation,
    get_objects_operation,
    get_parts_list_operation,
    get_view_operation,
    health_check_operation,
    insert_part_from_library_operation,
    list_documents_operation,
    redo_operation,
    run_fem_analysis_operation,
    save_document_operation,
    undo_operation,
)
from .prompt_text import ASSET_CREATION_STRATEGY
from .tool_policy import format_policy_for_log, resolve_tool_policy
from .utils import text_response as _text_response_helper


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
    """Decorator that blocks the wrapped tool when *tool_name* is disabled.

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
            return fn(*args, **kwargs)
        return wrapper
    return decorator


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


def get_freecad_connection() -> FreeCADConnection:
    """Get or create a persistent FreeCAD connection"""
    if state.freecad_connection is None:
        state.freecad_connection = FreeCADConnection(host=state.rpc_host, port=9875)
        if not state.freecad_connection.ping():
            logger.error("Failed to ping FreeCAD")
            state.freecad_connection = None
            raise Exception(
                "Failed to connect to FreeCAD. Make sure the FreeCAD addon is running."
            )
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


@_guard_tool("execute_code")
@mcp.tool()
def execute_code(ctx: Context, code: str) -> list[TextContent | ImageContent]:
    """Execute arbitrary Python code in FreeCAD.

    Args:
        code: The Python code to execute.

    Returns:
        A message indicating the success or failure of the code execution, the output of the code execution, and a screenshot of the object.
    """
    return execute_code_operation(get_freecad_connection(), state.only_text_feedback, code)


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


@_guard_tool("list_documents")
@mcp.tool()
def list_documents(ctx: Context) -> list[TextContent]:
    """Get the list of open documents in FreeCAD.

    Returns:
        A list of document names.
    """
    return list_documents_operation(get_freecad_connection())


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
