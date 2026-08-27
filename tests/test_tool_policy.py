"""Unit tests for the tool allow/deny policy.

The policy is the public mechanism operators use to disable dangerous
tools (notably ``execute_code``) in production deployments.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from freecad_mcp.tool_policy import (  # noqa: E402
    ALL_TOOL_NAMES,
    format_policy_for_log,
    resolve_tool_policy,
)


def _with_env(monkeypatch, **kwargs):
    """Tiny helper to set env vars for a single test and restore on exit."""
    for key, value in kwargs.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)


def test_default_policy_enables_everything(monkeypatch):
    _with_env(
        monkeypatch,
        FREECAD_MCP_DISABLED_TOOLS=None,
        FREECAD_MCP_REQUIRED_TOOLS=None,
    )
    policy = resolve_tool_policy()
    assert policy.enabled == ALL_TOOL_NAMES
    assert policy.disabled_requested == ()
    assert policy.required_requested == ()


def test_disabled_tools_filter(monkeypatch):
    _with_env(
        monkeypatch,
        FREECAD_MCP_DISABLED_TOOLS="execute_code,run_fem_analysis",
        FREECAD_MCP_REQUIRED_TOOLS=None,
    )
    policy = resolve_tool_policy()
    assert "execute_code" not in policy.enabled
    assert "run_fem_analysis" not in policy.enabled
    assert "create_object" in policy.enabled
    assert policy.disabled_requested == ("execute_code", "run_fem_analysis")


def test_disabled_tools_whitespace_tolerated(monkeypatch):
    _with_env(
        monkeypatch,
        FREECAD_MCP_DISABLED_TOOLS=" execute_code , run_fem_analysis ",
    )
    policy = resolve_tool_policy()
    assert "execute_code" not in policy.enabled
    assert "run_fem_analysis" not in policy.enabled


def test_required_tools_whitelist(monkeypatch):
    _with_env(
        monkeypatch,
        FREECAD_MCP_REQUIRED_TOOLS="create_object,get_view",
        FREECAD_MCP_DISABLED_TOOLS=None,
    )
    policy = resolve_tool_policy()
    assert policy.enabled == frozenset({"create_object", "get_view"})
    assert policy.required_requested == ("create_object", "get_view")


def test_mutually_exclusive_raises(monkeypatch):
    _with_env(
        monkeypatch,
        FREECAD_MCP_DISABLED_TOOLS="execute_code",
        FREECAD_MCP_REQUIRED_TOOLS="create_object",
    )
    try:
        resolve_tool_policy()
    except ValueError as e:
        assert "mutually exclusive" in str(e)
    else:
        raise AssertionError("expected ValueError")


def test_unknown_tool_in_disabled_raises(monkeypatch):
    _with_env(
        monkeypatch,
        FREECAD_MCP_DISABLED_TOOLS="execute_code,NotATool",
    )
    try:
        resolve_tool_policy()
    except ValueError as e:
        assert "NotATool" in str(e)
        assert "unknown" in str(e).lower()
    else:
        raise AssertionError("expected ValueError")


def test_unknown_tool_in_required_raises(monkeypatch):
    _with_env(
        monkeypatch,
        FREECAD_MCP_REQUIRED_TOOLS="create_object,definitely_not_a_real_tool",
    )
    try:
        resolve_tool_policy()
    except ValueError as e:
        assert "definitely_not_a_real_tool" in str(e)
    else:
        raise AssertionError("expected ValueError")


def test_empty_env_string_is_ignored(monkeypatch):
    _with_env(
        monkeypatch,
        FREECAD_MCP_DISABLED_TOOLS="",
        FREECAD_MCP_REQUIRED_TOOLS="",
    )
    policy = resolve_tool_policy()
    assert policy.enabled == ALL_TOOL_NAMES


def test_format_policy_open():
    from freecad_mcp.tool_policy import ToolPolicy
    p = ToolPolicy(enabled=ALL_TOOL_NAMES)
    out = format_policy_for_log(p)
    assert "OPEN" in out
    assert f"{len(ALL_TOOL_NAMES)}/{len(ALL_TOOL_NAMES)}" in out


def test_format_policy_denylist():
    from freecad_mcp.tool_policy import ToolPolicy
    p = ToolPolicy(
        enabled=ALL_TOOL_NAMES - {"execute_code"},
        disabled_requested=("execute_code",),
    )
    out = format_policy_for_log(p)
    assert "DENYLIST" in out
    assert "execute_code" in out


def test_format_policy_whitelist():
    from freecad_mcp.tool_policy import ToolPolicy
    p = ToolPolicy(
        enabled=frozenset({"create_object", "get_view"}),
        required_requested=("create_object", "get_view"),
    )
    out = format_policy_for_log(p)
    assert "WHITELIST" in out
    assert "create_object" in out
    assert "get_view" in out


def test_all_tool_names_known():
    """Sanity: the 18 tools we list in ALL_TOOL_NAMES must include
    every tool the server actually exposes today.
    """
    # Read the server module's tool list by importing it.
    expected = {
        "create_document", "create_object", "edit_object", "delete_object",
        "execute_code", "get_view", "get_active_view", "insert_part_from_library",
        "get_objects", "get_object", "get_parts_list", "list_documents",
        "run_fem_analysis", "undo", "redo", "save_document", "export_object",
        "health_check",
    }
    assert ALL_TOOL_NAMES == frozenset(expected)


# ----------------------------------------------------------------------
# Elevated-tools gating (v1.0.4 gauntlet)
# ----------------------------------------------------------------------


def test_non_elevated_tool_always_passes(monkeypatch):
    monkeypatch.delenv("FREECAD_MCP_ALLOW_ELEVATED_TOOLS", raising=False)
    from src.freecad_mcp.tool_policy import validate_elevated_tool_call

    assert validate_elevated_tool_call("create_document", has_token=False) is None
    assert validate_elevated_tool_call("list_documents", has_token=True) is None


def test_elevated_tool_disabled_when_opt_in_missing(monkeypatch):
    monkeypatch.delenv("FREECAD_MCP_ALLOW_ELEVATED_TOOLS", raising=False)
    from src.freecad_mcp.tool_policy import validate_elevated_tool_call

    reason = validate_elevated_tool_call("execute_code", has_token=True)
    assert reason is not None
    assert "FREECAD_MCP_ALLOW_ELEVATED_TOOLS" in reason


def test_elevated_tool_with_opt_in_but_no_token(monkeypatch):
    monkeypatch.setenv("FREECAD_MCP_ALLOW_ELEVATED_TOOLS", "1")
    from src.freecad_mcp.tool_policy import validate_elevated_tool_call

    reason = validate_elevated_tool_call("execute_code", has_token=False)
    assert reason is not None
    assert "token" in reason.lower()


def test_elevated_tool_with_opt_in_and_token_passes(monkeypatch):
    monkeypatch.setenv("FREECAD_MCP_ALLOW_ELEVATED_TOOLS", "1")
    from src.freecad_mcp.tool_policy import validate_elevated_tool_call

    assert validate_elevated_tool_call("execute_code", has_token=True) is None
    assert validate_elevated_tool_call("run_fem_analysis", has_token=True) is None


def test_run_fem_analysis_is_elevated(monkeypatch):
    from src.freecad_mcp.tool_policy import is_tool_elevated

    assert is_tool_elevated("run_fem_analysis") is True
    assert is_tool_elevated("execute_code") is True


if __name__ == "__main__":
    import sys
    # Allow running this test file directly: monkeypatch isn't available
    # outside pytest, so the env-var tests would fail. Use pytest.
    print("Run with pytest; direct invocation is not supported.")
    sys.exit(0)
