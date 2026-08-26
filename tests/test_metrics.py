"""Unit tests for the Prometheus-style metrics registry."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from freecad_mcp.metrics import (  # noqa: E402
    Counter,
    Gauge,
    Histogram,
    MetricsRegistry,
    format_prometheus,
)


# --- Counter -------------------------------------------------------------

def test_counter_increments():
    c = Counter("test_total", "test", labelnames=("tool",))
    c.inc("a")
    c.inc("a")
    c.inc("b", amount=5)
    assert c.value("a") == 2
    assert c.value("b") == 5
    assert c.value("c") == 0


def test_counter_rejects_negative():
    c = Counter("test_total", "test")
    try:
        c.inc(amount=-1)
    except ValueError:
        return
    raise AssertionError("expected ValueError on negative inc")


def test_counter_label_count_mismatch():
    c = Counter("test_total", "test", labelnames=("a", "b"))
    try:
        c.inc("only-one")
    except ValueError:
        return
    raise AssertionError("expected ValueError on label mismatch")


# --- Histogram -----------------------------------------------------------

def test_histogram_observe_buckets():
    h = Histogram("test_seconds", "test", labelnames=("tool",), buckets=(0.1, 0.5, 1.0))
    h.observe(0.05, "create_object")  # le_0.1
    h.observe(0.3, "create_object")   # le_0.5
    h.observe(0.8, "create_object")   # le_1.0
    h.observe(2.0, "create_object")   # le_inf
    snap = h.snapshot("create_object")
    assert snap["count"] == 4
    assert snap["sum"] == 0.05 + 0.3 + 0.8 + 2.0
    assert snap["le_0.1"] == 1
    assert snap["le_0.5"] == 2
    assert snap["le_1.0"] == 3
    assert snap["le_inf"] == 4


def test_histogram_rejects_bad_buckets():
    try:
        Histogram("x", "x", buckets=(0, 1, 2))
    except ValueError:
        return
    raise AssertionError("expected ValueError for non-positive bucket")
    try:
        Histogram("x", "x", buckets=(1, 0.5, 2))
    except ValueError:
        return
    raise AssertionError("expected ValueError for non-monotonic bucket")


# --- Gauge ---------------------------------------------------------------

def test_gauge_set_and_read():
    g = Gauge("test_state", "test")
    g.set(42)
    assert g.value() == 42
    g.set(7.5)
    assert g.value() == 7.5


# --- Registry + Prometheus output ---------------------------------------

def test_registry_has_default_metrics():
    r = MetricsRegistry()
    assert r.tool_calls is not None
    assert r.tool_duration is not None
    assert r.validation_failures is not None
    assert r.circuit_state is not None
    assert r.circuit_short_circuits is not None
    assert r.uptime_seconds is not None


def test_registry_records_calls():
    r = MetricsRegistry()
    r.tool_calls.inc("create_object", "success")
    r.tool_calls.inc("create_object", "success")
    r.tool_calls.inc("create_object", "error")
    r.tool_duration.observe(0.123, "create_object")
    r.validation_failures.inc("create_object")
    snap = r.as_dict()
    assert snap["tool_calls"][("create_object|success" if False else "create_object|success")] == 2 or \
        snap["tool_calls"].get("create_object|success") == 2
    assert snap["tool_calls"].get("create_object|error") == 1
    assert snap["validation_failures"].get("create_object") == 1


def test_format_prometheus_contains_counters_and_histogram():
    r = MetricsRegistry()
    r.tool_calls.inc("create_object", "success")
    r.tool_calls.inc("create_object", "error")
    r.tool_duration.observe(0.5, "create_object")
    out = format_prometheus(r)
    # Header for the tool_calls counter
    assert "freecad_mcp_tool_calls_total" in out
    assert 'tool="create_object"' in out
    assert 'status="success"' in out
    # Histogram has bucket and sum lines
    assert "freecad_mcp_tool_duration_seconds_bucket" in out
    assert "freecad_mcp_tool_duration_seconds_sum" in out
    assert "freecad_mcp_tool_duration_seconds_count" in out
    # Gauges
    assert "freecad_mcp_circuit_state" in out
    assert "freecad_mcp_uptime_seconds" in out


def test_format_prometheus_uses_cumulative_buckets():
    """In Prometheus, bucket counts are cumulative (le_1.0 >= le_0.5)."""
    import re as _re
    r = MetricsRegistry()
    r.tool_duration.observe(0.05, "x")
    r.tool_duration.observe(0.3, "x")
    r.tool_duration.observe(2.0, "x")
    out = format_prometheus(r)
    # Parse the bucket lines for the 'x' tool and ensure cumulativity.
    bucket_counts: dict[float, int] = {}
    for line in out.splitlines():
        if 'freecad_mcp_tool_duration_seconds_bucket' not in line:
            continue
        if 'tool="x"' not in line:
            continue
        if 'le="+Inf"' in line:
            continue
        # Format: ``name{tool="x",le="0.5"} 2``
        m = _re.search(r'le="([^"]+)"\}\s+(\d+)', line)
        assert m is not None, line
        le = float(m.group(1))
        count = int(m.group(2))
        bucket_counts[le] = count
    # Bucket values must be non-decreasing.
    le_values = sorted(bucket_counts)
    counts = [bucket_counts[le] for le in le_values]
    assert counts == sorted(counts), f"non-cumulative buckets: {bucket_counts}"


def test_health_check_operation_includes_metrics(monkeypatch):
    """The health_check tool should expose the metrics block in JSON."""
    from freecad_mcp.operations import core as ops

    class _FakeConn:
        def health_check(self_inner):
            return {"success": True, "uptime_seconds": 1.0, "rpc_server_running": True}

        def breaker_metrics(self_inner):
            return {
                "state": "closed",
                "consecutive_failures": 0,
                "threshold": 3,
                "total_calls": 0,
                "total_failures": 0,
                "total_short_circuits": 0,
            }

    r = MetricsRegistry()
    r.tool_calls.inc("create_object", "success")
    result = ops.health_check_operation(_FakeConn(), r)
    import json as _json
    payload = _json.loads(result[0].text)
    assert "circuit_breaker" in payload
    assert "metrics" in payload
    assert payload["circuit_breaker"]["state"] == "closed"
    assert payload["metrics"]["tool_calls"].get("create_object|success") == 1


def test_health_check_circuit_short_circuits_is_absolute(monkeypatch):
    """v0.4.0 fix: the metric must be ``set`` (not ``inc``) from the breaker's
    absolute counter. Two consecutive health_checks with the same breaker
    state must yield the same metric value, not a doubled one.
    """
    from freecad_mcp.operations import core as ops

    class _FakeConn:
        def __init__(self):
            self.health_check_calls = 0

        def health_check(self_inner):
            self_inner.health_check_calls += 1
            return {"success": True, "uptime_seconds": 1.0, "rpc_server_running": True}

        def breaker_metrics(self_inner):
            # Absolute counts — should NOT grow with the number of health_check calls.
            return {
                "state": "closed",
                "consecutive_failures": 0,
                "threshold": 3,
                "total_calls": 100,
                "total_failures": 5,
                "total_short_circuits": 7,
            }

    r = MetricsRegistry()
    fake = _FakeConn()
    ops.health_check_operation(fake, r)
    first = r.circuit_short_circuits.value()
    assert first == 7.0, f"expected 7, got {first}"

    ops.health_check_operation(fake, r)
    second = r.circuit_short_circuits.value()
    assert second == 7.0, f"BUG: counter doubled on second call: {second}"

    ops.health_check_operation(fake, r)
    third = r.circuit_short_circuits.value()
    assert third == 7.0, f"BUG: counter tripled on third call: {third}"


# ---------------------------------------------------------------------------
# v1.0.3 — direct coverage of MetricsRegistry / format_prometheus
# ---------------------------------------------------------------------------

def test_registry_uptime_is_monotonic():
    import time as _time
    r = MetricsRegistry()
    first = r.uptime()
    _time.sleep(0.01)
    second = r.uptime()
    assert second >= first


def test_as_dict_includes_uptime_seconds():
    r = MetricsRegistry()
    snap = r.as_dict()
    assert "uptime_seconds" in snap
    assert isinstance(snap["uptime_seconds"], float)
    assert snap["uptime_seconds"] >= 0


def test_as_dict_circuit_short_circuits_serialised_as_total():
    r = MetricsRegistry()
    r.circuit_short_circuits.set(42)
    snap = r.as_dict()
    assert snap["circuit_short_circuits_total"] == 42


def test_as_dict_histogram_empty_no_tool_duration_count_keys():
    r = MetricsRegistry()
    snap = r.as_dict()
    # No observations recorded -> no per-tool histogram entries.
    assert snap["tool_duration_count"] == {}


def test_format_prometheus_includes_help_and_type_for_all_metrics():
    r = MetricsRegistry()
    r.tool_calls.inc("create_object", "success")
    r.validation_failures.inc("create_object")
    r.circuit_short_circuits.set(3)
    r.circuit_state.set(1)
    out = format_prometheus(r)
    for metric_name in (
        "freecad_mcp_tool_calls_total",
        "freecad_mcp_tool_duration_seconds",
        "freecad_mcp_validation_failures_total",
        "freecad_mcp_circuit_state",
        "freecad_mcp_circuit_short_circuits_total",
        "freecad_mcp_uptime_seconds",
    ):
        assert f"# HELP {metric_name}" in out, f"missing HELP for {metric_name}"
        assert f"# TYPE {metric_name}" in out, f"missing TYPE for {metric_name}"


def test_format_prometheus_includes_inf_bucket():
    r = MetricsRegistry()
    r.tool_duration.observe(100.0, "create_object")  # > 60s default max
    out = format_prometheus(r)
    assert 'le="+Inf"' in out


def test_format_prometheus_ends_with_newline():
    """Prometheus expects each line LF-terminated and a final newline."""
    r = MetricsRegistry()
    out = format_prometheus(r)
    assert out.endswith("\n")


def test_format_prometheus_empty_registry_is_well_formed():
    """An empty registry still renders valid Prometheus text."""
    r = MetricsRegistry()
    out = format_prometheus(r)
    # Every HELP/TYPE pair should be present, even with zero samples.
    assert "# HELP freecad_mcp_tool_calls_total" in out
    assert "# TYPE freecad_mcp_tool_calls_total counter" in out
    # No actual sample lines yet, but the HELP/TYPE headers must exist.
    assert "freecad_mcp_uptime_seconds " in out  # uptime is sampled


def test_histogram_zero_count_snapshot_is_all_zeros():
    """snapshot() for an unobserved label returns zeros, not KeyError."""
    h = Histogram("h", "h", labelnames=("k",))
    snap = h.snapshot("k")
    assert snap["count"] == 0
    assert snap["sum"] == 0.0
    assert snap["le_inf"] == 0


def test_histogram_invalid_buckets_rejected():
    """v1.0.3 — explicit guard against non-monotonic / non-positive buckets."""
    for bad in [(0.0, 1.0), (1.0, 0.5), (-1.0, 1.0), (1.0, 1.0)]:
        try:
            Histogram("x", "x", buckets=bad)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for buckets={bad}")


def test_counter_set_replaces_increments():
    """v1.0.3 — ``set`` overwrites the absolute value (used by the
    breaker snapshot path)."""
    c = Counter("x_total", "x", labelnames=("k",))
    c.inc("a", amount=5)
    assert c.value("a") == 5
    c.set(42, "a")
    assert c.value("a") == 42


def test_counter_set_rejects_negative():
    c = Counter("x_total", "x", labelnames=("k",))
    try:
        c.set(-1, "a")
    except ValueError:
        return
    raise AssertionError("expected ValueError for negative set")


def test_gauge_value_default_is_zero():
    g = Gauge("g", "g", labelnames=("k",))
    assert g.value("nope") == 0.0


def test_gauge_overwrites_on_set():
    g = Gauge("g", "g", labelnames=("k",))
    g.set(1, "a")
    g.set(2, "a")
    g.set(3, "a")
    assert g.value("a") == 3


if __name__ == "__main__":
    print("Run with pytest; direct invocation is not supported.")
