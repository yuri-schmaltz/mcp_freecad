"""Tool allow/deny policy for the MCP server.

Why this exists
---------------
A professional deployment must be able to **turn off** dangerous tools
without forking the code. The default install ships with all 18 tools
enabled because the LLM needs them for design work, but a production
deployment (e.g. a service that exposes FreeCAD to multiple users) should
be able to disable ``execute_code`` while keeping ``create_object``.

Two env vars are honoured:

* ``FREECAD_MCP_DISABLED_TOOLS`` — comma-separated tool names to
  disable. Disabled tools are removed from the MCP tool list and
  answer with a clear error if a client tries to call them by name
  (e.g. via a stale prompt or a hostile model).
* ``FREECAD_MCP_REQUIRED_TOOLS`` — when set, the server starts in
  whitelist mode and ONLY the listed tools are exposed. Use this for
  the most restrictive deployments.

The two are mutually exclusive: setting both raises at startup. The
policy is enforced at server start-up; runtime toggling would require
a restart.

Validation
----------
* Unknown tool names are reported at start-up and the process refuses
  to start (typos in env vars are worse than a missing tool).
* The policy is logged in the boot banner so operators can confirm
  what was applied.

Tests
-----
See ``tests/test_tool_policy.py`` for the full matrix.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger("FreeCADMCPtool_policy")


# All tools that this server may expose. Kept as a constant so the
# ``tests/test_tool_policy.py`` validation has a single source of truth.
ALL_TOOL_NAMES: frozenset[str] = frozenset({
    "create_document",
    "create_object",
    "edit_object",
    "delete_object",
    "execute_code",
    "get_view",
    "get_active_view",
    "insert_part_from_library",
    "get_objects",
    "get_object",
    "get_parts_list",
    "list_documents",
    "run_fem_analysis",
    "undo",
    "redo",
    "save_document",
    "export_object",
    "health_check",
})


@dataclass
class ToolPolicy:
    """Resolved allow/deny set.

    ``enabled`` is the final set of tool names the server will expose.
    ``disabled_requested`` keeps the raw user-supplied list for the boot
    log; ``unknown_requested`` is a list of names that were supplied but
    do not match a known tool (typo / fork mismatch).
    """

    enabled: frozenset[str]
    disabled_requested: tuple[str, ...] = ()
    required_requested: tuple[str, ...] = ()
    unknown_requested: tuple[str, ...] = ()


def _parse_csv(env_var: str) -> tuple[str, ...]:
    raw = os.environ.get(env_var, "").strip()
    if not raw:
        return ()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def resolve_tool_policy() -> ToolPolicy:
    """Compute the effective tool policy from the environment.

    Raises ``ValueError`` on configuration errors so the server fails
    to start rather than running with the wrong tools.
    """
    disabled = _parse_csv("FREECAD_MCP_DISABLED_TOOLS")
    required = _parse_csv("FREECAD_MCP_REQUIRED_TOOLS")

    if disabled and required:
        raise ValueError(
            "FREECAD_MCP_DISABLED_TOOLS and FREECAD_MCP_REQUIRED_TOOLS are "
            "mutually exclusive. Set at most one of them."
        )

    unknown: list[str] = []

    if required:
        for name in required:
            if name not in ALL_TOOL_NAMES:
                unknown.append(name)
        if unknown:
            raise ValueError(
                f"FREECAD_MCP_REQUIRED_TOOLS contains unknown tool names: {unknown}. "
                f"Known tools: {sorted(ALL_TOOL_NAMES)}"
            )
        return ToolPolicy(
            enabled=frozenset(required),
            required_requested=required,
        )

    if disabled:
        for name in disabled:
            if name not in ALL_TOOL_NAMES:
                unknown.append(name)
        if unknown:
            raise ValueError(
                f"FREECAD_MCP_DISABLED_TOOLS contains unknown tool names: {unknown}. "
                f"Known tools: {sorted(ALL_TOOL_NAMES)}"
            )
        enabled = frozenset(ALL_TOOL_NAMES - set(disabled))
        return ToolPolicy(
            enabled=enabled,
            disabled_requested=disabled,
        )

    return ToolPolicy(enabled=ALL_TOOL_NAMES)


# Tools that, when exposed, additionally require an explicit elevation
# token. The token is checked at the per-tool wrapper level so a stolen
# ``FREECAD_MCP_DISABLED_TOOLS`` env var alone cannot unlock these.
ELEVATED_TOOLS: frozenset[str] = frozenset({"execute_code", "run_fem_analysis"})


def is_tool_elevated(name: str) -> bool:
    """Return True if *name* is one of the elevated/dangerous tools."""
    return name in ELEVATED_TOOLS


def elevated_tools_enabled() -> bool:
    """``True`` when the operator opted into elevated tools.

    Controlled by ``FREECAD_MCP_ALLOW_ELEVATED_TOOLS=1``. Default is
    **disabled** — operators must opt in explicitly. This is separate
    from ``FREECAD_MCP_DISABLED_TOOLS`` so a misconfigured denylist
    cannot re-enable ``execute_code`` by accident.
    """
    flag = os.environ.get("FREECAD_MCP_ALLOW_ELEVATED_TOOLS", "").strip().lower()
    return flag in ("1", "true", "yes", "on")


def validate_elevated_tool_call(name: str, has_token: bool) -> str | None:
    """Return ``None`` if *name* is allowed to run, else a human-readable reason.

    The check is layered: even if ``elevated_tools_enabled()`` is True,
    an elevated tool still needs ``has_token=True`` (the client must
    have called :meth:`FreeCADConnection.set_bearer_token`). A
    non-elevated tool always passes; a missing token on an
    enabled-elevated tool produces a message that names the env var
    the operator must set.

    Tests should assert against ``reason is None`` / ``"token" in reason``
    rather than the exact wording so future rewording doesn't break
    the suite.
    """
    if not is_tool_elevated(name):
        return None
    if not elevated_tools_enabled():
        return (
            f"Tool '{name}' is elevated and is disabled because "
            f"FREECAD_MCP_ALLOW_ELEVATED_TOOLS is not set to 1."
        )
    if not has_token:
        return (
            f"Tool '{name}' is elevated and requires a bearer token. "
            f"Set FREECAD_MCP_AUTH_TOKEN on the FreeCAD server and call "
            f"FreeCADConnection.set_bearer_token(token) before invoking "
            f"this tool."
        )
    return None


def format_policy_for_log(policy: ToolPolicy) -> str:
    """One-line description for the boot log."""
    n_total = len(ALL_TOOL_NAMES)
    n_enabled = len(policy.enabled)
    if policy.required_requested:
        return f"tool policy: WHITELIST ({n_enabled}/{n_total}) {sorted(policy.enabled)}"
    if policy.disabled_requested:
        return (
            f"tool policy: DENYLIST ({n_enabled}/{n_total} enabled, "
            f"disabled: {sorted(policy.disabled_requested)})"
        )
    return f"tool policy: OPEN ({n_enabled}/{n_total} all tools enabled)"


__all__ = [
    "ALL_TOOL_NAMES",
    "ELEVATED_TOOLS",
    "ToolPolicy",
    "resolve_tool_policy",
    "format_policy_for_log",
    "is_tool_elevated",
    "elevated_tools_enabled",
    "validate_elevated_tool_call",
]
