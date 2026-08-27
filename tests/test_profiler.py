"""Tests for src/freecad_mcp/profiler.py."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from freecad_mcp.profiler import (
    PerformanceProfiler,
    ProfileEntry,
    _profile_decorator,
    get_profiler,
    reset_profiler_singleton,
)


def test_profiler_records_entry() -> None:
    p = PerformanceProfiler(max_entries=10)
    p.record(ProfileEntry(tool_name="x", start_time=0.0, duration_ms=10.0, success=True))
    assert len(p) == 1


def test_profiler_ring_buffer_evicts_oldest() -> None:
    p = PerformanceProfiler(max_entries=2)
    for i in range(5):
        p.record(ProfileEntry(tool_name=f"t{i}", start_time=0.0, duration_ms=1.0, success=True))
    assert len(p) == 2
    recent = p.get_recent(10)
    assert [e.tool_name for e in recent] == ["t3", "t4"]


def test_profiler_stats_percentiles() -> None:
    p = PerformanceProfiler(max_entries=100)
    for ms in range(1, 101):
        p.record(ProfileEntry(tool_name="x", start_time=0.0, duration_ms=float(ms), success=True))
    stats = p.get_stats()
    assert "x" in stats
    s = stats["x"]
    assert s["count"] == 100.0
    assert 49 < s["p50_ms"] < 52
    assert 94 < s["p95_ms"] < 96
    assert 98 < s["p99_ms"] < 100
    assert s["max_ms"] == 100.0


def test_profiler_stats_degenerate_single_sample() -> None:
    p = PerformanceProfiler(max_entries=10)
    p.record(ProfileEntry(tool_name="solo", start_time=0.0, duration_ms=42.0, success=True))
    stats = p.get_stats()
    assert stats["solo"]["count"] == 1.0
    assert stats["solo"]["mean_ms"] == 42.0


def test_profiler_get_recent_zero_or_negative() -> None:
    p = PerformanceProfiler()
    p.record(ProfileEntry(tool_name="a", start_time=0.0, duration_ms=1.0, success=True))
    assert p.get_recent(0) == []
    assert p.get_recent(-5) == []


def test_profiler_get_slow_calls() -> None:
    p = PerformanceProfiler()
    p.record(ProfileEntry(tool_name="slow", start_time=0.0, duration_ms=2500.0, success=True))
    p.record(ProfileEntry(tool_name="fast", start_time=0.0, duration_ms=10.0, success=True))
    slow = p.get_slow_calls(threshold_ms=1000)
    assert len(slow) == 1
    assert slow[0].tool_name == "slow"


def test_profiler_get_slow_calls_rejects_non_positive() -> None:
    p = PerformanceProfiler()
    with pytest.raises(ValueError):
        p.get_slow_calls(threshold_ms=0)


def test_profiler_export_flamegraph() -> None:
    p = PerformanceProfiler()
    p.record(ProfileEntry(tool_name="t1", start_time=0.0, duration_ms=10.0, success=True))
    p.record(ProfileEntry(tool_name="t1", start_time=0.0, duration_ms=20.0, success=True))
    p.record(ProfileEntry(tool_name="t2", start_time=0.0, duration_ms=5.0, success=True))
    out = p.export_flamegraph_data()
    assert "t1 2 15.0" in out
    assert "t2 1 5.0" in out


def test_profiler_export_flamegraph_empty() -> None:
    p = PerformanceProfiler()
    assert p.export_flamegraph_data() == ""


def test_profiler_reset() -> None:
    p = PerformanceProfiler()
    p.record(ProfileEntry(tool_name="x", start_time=0.0, duration_ms=10.0, success=True))
    p.reset()
    assert len(p) == 0


def test_profile_decorator_records_duration_and_success() -> None:
    reset_profiler_singleton()
    p = PerformanceProfiler(max_entries=10)
    reset_profiler_singleton()
    # Inject our profiler
    import freecad_mcp.profiler as P

    P._profiler_singleton = p

    @_profile_decorator
    def add(a, b):
        return a + b

    assert add(2, 3) == 5
    assert len(p) == 1
    assert p.get_recent(1)[0].tool_name == "add"
    assert p.get_recent(1)[0].success is True


def test_profile_decorator_records_exceptions() -> None:
    import freecad_mcp.profiler as P

    p = PerformanceProfiler(max_entries=10)
    P._profiler_singleton = p

    @_profile_decorator
    def boom():
        raise RuntimeError("kapow")

    with pytest.raises(RuntimeError):
        boom()
    assert len(p) == 1
    assert p.get_recent(1)[0].success is False
    assert "kapow" in (p.get_recent(1)[0].error or "")


def test_get_profiler_singleton_idempotent() -> None:
    reset_profiler_singleton()
    a = get_profiler()
    b = get_profiler()
    assert a is b


def test_max_entries_property() -> None:
    p = PerformanceProfiler(max_entries=42)
    assert p.max_entries == 42


def test_invalid_max_entries() -> None:
    with pytest.raises(ValueError):
        PerformanceProfiler(max_entries=0)
