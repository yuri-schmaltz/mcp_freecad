"""Tests for src/freecad_mcp/streaming.py."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from freecad_mcp.streaming import (
    OutputBuffer,
    ProgressDebouncer,
    stream_output,
)


def test_output_buffer_ingests_success_message() -> None:
    buf = OutputBuffer()
    buf.ingest({"success": True, "message": "Output: hello\nworld\n"})
    lines = buf.pop_emitted()
    assert "hello" in lines
    assert "world" in lines
    assert not buf.failed


def test_output_buffer_ingests_failure() -> None:
    buf = OutputBuffer()
    buf.ingest({"success": False, "error": "boom", "output": "trace1\ntrace2\n"})
    assert buf.failed
    assert buf.error == "boom"
    lines = buf.pop_emitted()
    assert "trace1" in lines
    assert "trace2" in lines


def test_output_buffer_pop_emitted_only_returns_new_lines() -> None:
    buf = OutputBuffer()
    buf.ingest({"success": True, "message": "a\nb\n"})
    first = buf.pop_emitted()
    assert first == ["a", "b"]
    buf.ingest({"success": True, "message": "c\n"})
    second = buf.pop_emitted()
    assert second == ["c"]


def test_output_buffer_full_output_joined() -> None:
    buf = OutputBuffer()
    buf.ingest({"success": True, "message": "x\ny\nz\n"})
    assert buf.full_output() == "x\ny\nz"


def test_output_buffer_strips_output_prefix() -> None:
    buf = OutputBuffer()
    buf.ingest({"success": True, "message": "Output: only-line\n"})
    lines = buf.pop_emitted()
    assert lines == ["only-line"]


def test_progress_debouncer_throttles() -> None:
    times = iter([0.0, 0.05, 0.1, 0.2])
    db = ProgressDebouncer(min_interval_s=0.1, clock=lambda: next(times))
    assert db.should_emit() is True
    db.mark_emitted()
    assert db.should_emit() is False
    assert db.should_emit() is True


def test_progress_debouncer_reset() -> None:
    db = ProgressDebouncer(min_interval_s=10.0)
    db.mark_emitted()
    db.reset()
    assert db.should_emit() is True


def test_progress_debouncer_negative_interval_rejected() -> None:
    with pytest.raises(ValueError):
        ProgressDebouncer(min_interval_s=-1.0)


def test_stream_output_reports_progress() -> None:
    buf = OutputBuffer()
    buf.ingest({"success": True, "message": "hello\n"})
    reported: list[tuple[float, float]] = []

    class _StubCtx:
        async def report_progress(self, progress, total):
            reported.append((progress, total))

    asyncio.run(stream_output(buf, _StubCtx(), debounce_s=0.0))
    assert reported
    assert reported[-1] == (100.0, 100.0)
