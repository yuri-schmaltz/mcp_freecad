"""Tests for the mesh_import / mesh_simplify / mesh_to_solid operations.

Covers the operation wrappers in :mod:`freecad_mcp.operations.core`
that translate MCP tool calls into ``FreeCADConnection`` RPC calls
and back into MCP ``ToolResponse`` payloads. Uses a fake connection
that records each call and returns canned results.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from freecad_mcp.operations.core import (
    mesh_import_operation,
    mesh_simplify_operation,
    mesh_to_solid_operation,
)
from freecad_mcp.responses import ToolResponse


class _FakeConnection:
    def __init__(self) -> None:
        self.calls: list = []

    def mesh_import(self, path, doc_name=None, label=None):
        self.calls.append(("mesh_import", path, doc_name, label))
        return {
            "success": True,
            "object_name": label or "Mesh",
            "label": label or "Mesh",
            "triangle_count": 240,
            "vertex_count": 122,
        }

    def mesh_simplify(self, doc_name, mesh_name, target_faces):
        self.calls.append(("mesh_simplify", doc_name, mesh_name, target_faces))
        return {
            "success": True,
            "triangle_count_before": 12_000,
            "triangle_count_after": 5_000,
            "reduction_pct": 58.3,
        }

    def mesh_to_solid(
        self,
        doc_name,
        mesh_name,
        new_name=None,
        *,
        repair=True,
        sew_tolerance=1e-3,
        max_triangles_before_simplify=50_000,
        target_faces_after_simplify=5_000,
    ):
        self.calls.append(
            (
                "mesh_to_solid",
                doc_name,
                mesh_name,
                new_name,
                repair,
                sew_tolerance,
                max_triangles_before_simplify,
                target_faces_after_simplify,
            )
        )
        return {
            "success": True,
            "object_name": new_name or f"{mesh_name}_Solid",
            "shell_faces": 8,
            "solid": True,
            "volume": 42.0,
            "triangle_count": 5_000,
            "decimated": True,
        }


def test_mesh_import_operation_returns_text_payload():
    conn = _FakeConnection()
    res = mesh_import_operation(conn, path="/tmp/x.stl", doc_name="Doc", label="MyMesh")
    assert isinstance(res, list)
    assert conn.calls == [("mesh_import", "/tmp/x.stl", "Doc", "MyMesh")]


def test_mesh_import_operation_failure_returns_error_text():
    class _Conn:
        def mesh_import(self, **kwargs):
            return {"success": False, "reason": "boom"}

    res = mesh_import_operation(_Conn(), path="/x")
    text = res[0].text if hasattr(res[0], "text") else str(res[0])
    assert "boom" in text


def test_mesh_simplify_operation_forwards_args():
    conn = _FakeConnection()
    res = mesh_simplify_operation(conn, doc_name="Doc", mesh_name="M", target_faces=2_000)
    assert isinstance(res, list)
    assert conn.calls[0][0] == "mesh_simplify"
    assert conn.calls[0][3] == 2_000


def test_mesh_simplify_operation_failure_text():
    class _Conn:
        def mesh_simplify(self, **kwargs):
            return {"success": False, "reason": "not a mesh"}

    res = mesh_simplify_operation(_Conn(), doc_name="D", mesh_name="M")
    text = res[0].text if hasattr(res[0], "text") else str(res[0])
    assert "not a mesh" in text


def test_mesh_to_solid_operation_forwards_all_kwargs():
    conn = _FakeConnection()
    res = mesh_to_solid_operation(
        conn,
        doc_name="Doc",
        mesh_name="M",
        new_name="MySolid",
        repair=False,
        sew_tolerance=1e-4,
        max_triangles_before_simplify=10_000,
        target_faces_after_simplify=1_000,
    )
    assert isinstance(res, list)
    call = conn.calls[0]
    assert call[0] == "mesh_to_solid"
    assert call[1] == "Doc"
    assert call[2] == "M"
    assert call[3] == "MySolid"
    assert call[4] is False
    assert call[5] == 1e-4
    assert call[6] == 10_000
    assert call[7] == 1_000


def test_mesh_to_solid_operation_failure_text():
    class _Conn:
        def mesh_to_solid(self, **kwargs):
            return {"success": False, "reason": "sewing failed"}

    res = mesh_to_solid_operation(_Conn(), doc_name="D", mesh_name="M")
    text = res[0].text if hasattr(res[0], "text") else str(res[0])
    assert "sewing failed" in text


def test_operations_registered_in_all_tool_names():
    """Mesh tools must be discoverable through the policy gate."""
    from freecad_mcp.tool_policy import ALL_TOOL_NAMES
    assert "mesh_import" in ALL_TOOL_NAMES
    assert "mesh_simplify" in ALL_TOOL_NAMES
    assert "mesh_to_solid" in ALL_TOOL_NAMES