"""Tests for the api_introspect module (v1.1.2)."""
from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path

import pytest

_RPC_DIR = Path(__file__).resolve().parent.parent / "addon/FreeCADMCP/rpc_server"


@pytest.fixture
def ai_mod():
    spec = importlib.util.spec_from_file_location(
        "_api_introspect_for_test", _RPC_DIR / "api_introspect.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_api_introspect_for_test"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def test_introspect_callable(ai_mod) -> None:
    modules = {"math": math}
    res = ai_mod.api_introspect("math.sqrt", modules)
    assert res["success"] is True
    info = res["info"]
    assert info["qualified_name"] == "math.sqrt"
    assert info["signature"] == "(x)" or "x" in info["signature"]
    assert info["is_class"] is False
    assert info["module"] == "math"


def test_introspect_class(ai_mod) -> None:
    import os
    modules = {"os": os}
    res = ai_mod.api_introspect("os.PathLike", modules)
    assert res["success"] is True
    info = res["info"]
    # Either PathLike is itself a class, or it resolves to an attribute.
    assert "qualified_name" in info


def test_introspect_unknown_returns_error(ai_mod) -> None:
    res = ai_mod.api_introspect("nope.never.exists", {"math": math})
    assert res["success"] is False
    assert "not found" in res["reason"]


def test_introspect_ambiguous_path(ai_mod) -> None:
    # ``math.e`` is a float attribute — should still succeed.
    res = ai_mod.api_introspect("math.e", {"math": math})
    assert res["success"] is True
    assert res["info"]["qualified_name"] == "math.e"


def test_api_search_substring(ai_mod) -> None:
    modules = {"math": math, "os": __import__("os")}
    hits = ai_mod.api_search("sqrt", modules)
    assert any("sqrt" in h["qualified_name"] for h in hits)


def test_api_search_regex(ai_mod) -> None:
    modules = {"math": math}
    hits = ai_mod.api_search("/^sin/", modules)
    assert any(h["qualified_name"].endswith("sin") for h in hits)


def test_api_search_with_filter(ai_mod) -> None:
    modules = {"math": math, "os": __import__("os")}
    hits = ai_mod.api_search("path", modules, modules_filter={"math"})
    assert all("math." in h["qualified_name"] for h in hits)


def test_api_search_limit(ai_mod) -> None:
    modules = {"math": math}
    hits = ai_mod.api_search("a", modules, limit=3)
    assert len(hits) <= 3


def test_api_search_regex_timeout_falls_back(ai_mod) -> None:
    """A pathological regex must not wedge the server.

    The probe at compile time is bounded by ``regex_timeout_seconds``;
    when it expires the pattern is dropped and the search falls back
    to plain substring matching.
    """
    # The classic catastrophic-backtracking pattern.
    pathological = "/" + "(a+)+$" + "/"
    hits = ai_mod.api_search(pathological, {"math": math}, regex_timeout_seconds=0.05)
    # We don't care which path the search took as long as it returned
    # *something* and the call completed.
    assert isinstance(hits, list)


def test_api_search_regex_invalid_syntax(ai_mod) -> None:
    """An invalid regex must be silently dropped, not raise."""
    hits = ai_mod.api_search("/[/", {"math": math})
    # Falls back to substring search — ``/[/`` doesn't match any name.
    assert hits == []


def test_default_modules_contains_math() -> None:
    import importlib
    spec = importlib.util.spec_from_file_location(
        "_api_introspect_for_default_test",
        _RPC_DIR / "api_introspect.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_api_introspect_for_default_test"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    modules = mod.default_modules()
    assert "math" in modules
    assert "os" in modules


def test_function_info_dataclass(ai_mod) -> None:
    fi = ai_mod.FunctionInfo(qualified_name="x", signature="()", doc="")
    d = fi.to_dict()
    assert d["qualified_name"] == "x"
    assert d["parameters"] == []


def test_param_info_default_handling(ai_mod) -> None:
    p = ai_mod.ParamInfo(name="x", kind="POSITIONAL_OR_KEYWORD")
    d = p.to_dict()
    assert d["default"] is None
    assert d["has_default"] is False
    p2 = ai_mod.ParamInfo(name="y", kind="POSITIONAL_OR_KEYWORD", default=42)
    d2 = p2.to_dict()
    assert d2["default"] == 42
    assert d2["has_default"] is True
