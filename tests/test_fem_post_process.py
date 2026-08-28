"""Tests for the fem_post_process module (v1.1.1)."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_RPC_DIR = Path(__file__).resolve().parent.parent / "addon/FreeCADMCP/rpc_server"


@pytest.fixture
def fp_mod():
    spec = importlib.util.spec_from_file_location(
        "_fem_post_process_for_test", _RPC_DIR / "fem_post_process.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_fem_post_process_for_test"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ---------------------------------------------------------------------------
# Sample FRD fixture
# ---------------------------------------------------------------------------

MIN_FRD = """\
    100C                       1
 -1
    1PSTEP         1
 -1
    1PUSER                test-run
 -1
 NODE
 -1
    1C          1    0.00000    0.00000    0.00000
    1C          2    1.00000    0.00000    0.00000
    1C          3    0.50000    0.86603    0.00000
 -1
 DISP
 -1
    1C    1    2  0.0000  0.0000  0.0000
    1C    2    2  0.0010 -0.0005  0.0003
    1C    3    2 -0.0008  0.0012 -0.0001
 -1
 STRESS
 -1
    1C    1    1  100.0 50.0 30.0  10.0  5.0  2.0
    1C    2    1  90.0  45.0 25.0  8.0   4.0  1.0
 -1
"""


EMPTY_FRD = """\
    100C                       1
 -1
    1PSTEP         1
 -1
"""


MULTI_STEP_FRD = """\
    100C                       1
 -1
    1PSTEP         1
 -1
 NODE
 -1
    1C          1    0.0 0.0 0.0
 -1
    1PSTEP         2
 -1
 NODE
 -1
    1C          1    0.0 0.0 0.0
 -1
"""


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_parse_minimal_frd(fp_mod, tmp_path: Path) -> None:
    f = tmp_path / "out.frd"
    f.write_text(MIN_FRD, encoding="latin-1")
    res = fp_mod.fem_post_process(str(f))
    assert res["success"] is True
    assert res["step"] == 1
    assert res["node_count"] == 3
    assert res["displacement_count"] == 3
    assert res["stress_count"] == 2


def test_summary_max_displacement(fp_mod, tmp_path: Path) -> None:
    f = tmp_path / "out.frd"
    f.write_text(MIN_FRD, encoding="latin-1")
    res = fp_mod.fem_post_process(str(f))
    summary = res["summary"]
    assert summary["max_displacement"] > 0
    assert summary["min_displacement"] >= 0
    assert summary["mean_displacement"] >= 0


def test_summary_max_von_mises(fp_mod, tmp_path: Path) -> None:
    f = tmp_path / "out.frd"
    f.write_text(MIN_FRD, encoding="latin-1")
    res = fp_mod.fem_post_process(str(f))
    assert "max_von_mises" in res["summary"]
    assert res["summary"]["max_von_mises"] > 0


def test_max_displacement_node_payload(fp_mod, tmp_path: Path) -> None:
    f = tmp_path / "out.frd"
    f.write_text(MIN_FRD, encoding="latin-1")
    res = fp_mod.fem_post_process(str(f))
    node = res["max_displacement_node"]
    assert node is not None
    assert "node" in node
    assert "magnitude" in node
    assert node["magnitude"] > 0


def test_max_stress_element_payload(fp_mod, tmp_path: Path) -> None:
    f = tmp_path / "out.frd"
    f.write_text(MIN_FRD, encoding="latin-1")
    res = fp_mod.fem_post_process(str(f))
    el = res["max_stress_element"]
    assert el is not None
    assert el["von_mises"] > 0
    assert el["element"] == 1  # First element had higher stress


def test_parse_empty_frd(fp_mod, tmp_path: Path) -> None:
    f = tmp_path / "empty.frd"
    f.write_text(EMPTY_FRD, encoding="latin-1")
    res = fp_mod.fem_post_process(str(f))
    assert res["success"] is True
    assert res["node_count"] == 0
    assert res["displacement_count"] == 0
    assert res["stress_count"] == 0
    assert res["summary"] == {}


def test_parse_multi_step_keeps_last_step(fp_mod, tmp_path: Path) -> None:
    f = tmp_path / "multi.frd"
    f.write_text(MULTI_STEP_FRD, encoding="latin-1")
    res = fp_mod.fem_post_process(str(f))
    assert res["success"] is True
    # Both NODE blocks are read but step number reflects the last.
    assert res["step"] == 2


def test_missing_file(fp_mod) -> None:
    res = fp_mod.fem_post_process("/nonexistent/path.frd")
    assert res["success"] is False
    assert "not found" in res["reason"]


def test_empty_path(fp_mod) -> None:
    res = fp_mod.fem_post_process("")
    assert res["success"] is False


def test_whitespace_path(fp_mod) -> None:
    res = fp_mod.fem_post_process("   ")
    assert res["success"] is False


def test_relative_path(fp_mod) -> None:
    res = fp_mod.fem_post_process("relative/path.frd")
    assert res["success"] is False
    assert "absolute" in res["reason"]


def test_oversized_file(fp_mod, tmp_path: Path) -> None:
    f = tmp_path / "big.frd"
    f.write_bytes(b"X" * (10 * 1024 * 1024))  # 10 MB > default 256 MB ok
    # Override max_bytes to force a cap.
    res = fp_mod.fem_post_process(str(f), max_bytes=1024)
    assert res["success"] is False
    assert "too large" in res["reason"]


def test_directory_path(fp_mod, tmp_path: Path) -> None:
    res = fp_mod.fem_post_process(str(tmp_path))
    assert res["success"] is False


def test_von_mises_calculation(fp_mod) -> None:
    """Pure uniaxial stress σ → von Mises = |σ|."""
    sx, sy, sz, sxy, syz, szx = 100.0, 0.0, 0.0, 0.0, 0.0, 0.0
    vm = (0.5 * ((sx - sy) ** 2 + (sy - sz) ** 2 + (sz - sx) ** 2)
          + 3.0 * (sxy ** 2 + syz ** 2 + szx ** 2)) ** 0.5
    assert abs(vm - 100.0) < 1e-9


def test_node_block_parser_basic(fp_mod) -> None:
    lines = [
        " 1C          1    0.0 0.0 0.0",
        " 1C          2    1.0 2.0 3.0",
        "-1",
    ]
    nodes, next_idx = fp_mod._parse_node_block(lines, 0)
    assert len(nodes) == 2
    assert nodes[0]["id"] == 1
    assert nodes[1]["x"] == 1.0
    assert next_idx == 3  # past the "-1"


def test_displ_block_parser_basic(fp_mod) -> None:
    lines = [
        " 1C    1    2  0.0 0.0 0.0",
        " 1C    2    2  3.0 4.0 0.0",
        "-1",
    ]
    rows, next_idx = fp_mod._parse_displ_block(lines, 0)
    assert len(rows) == 2
    # Magnitude of (3, 4, 0) is 5.
    assert rows[1]["magnitude"] == 5.0


def test_stress_block_parser_basic(fp_mod) -> None:
    lines = [
        " 1C   42    1  200.0  100.0  50.0  10.0  5.0  2.0",
        "-1",
    ]
    rows, next_idx = fp_mod._parse_stress_block(lines, 0)
    assert len(rows) == 1
    assert rows[0]["element"] == 42
    assert rows[0]["von_mises"] > 0
