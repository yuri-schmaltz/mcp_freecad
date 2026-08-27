"""Lightweight Prometheus-style metrics for the MCP server.

Why a home-grown implementation?
--------------------------------
The ``prometheus_client`` package is the obvious choice but it
introduces a runtime dependency and (more importantly) a global
default registry that fights pytest fixture isolation. For the
six metrics we actually need (counters, histograms, gauges) a
200-line implementation is simpler, has no third-party surface, and
is fully unit-testable.

Public surface
--------------
* :class:`Counter` \u2014 monotonically increasing integer with optional labels.
* :class:`Histogram` \u2014 fixed-bucket distribution with optional labels.
* :class:`Gauge` \u2014 settable integer/float.
* :class:`MetricsRegistry` \u2014 the container; one per server instance.
* :func:`format_prometheus` \u2014 render the registry in the
  Prometheus text exposition format (compatible with
  ``/metrics`` scraping).

The registry is exposed through the ``health_check`` MCP tool as a
JSON ``metrics`` block; the same registry can be exposed on a plain
HTTP endpoint by the deployment (see the README "Monitoring" section).
"""
from __future__ import annotations

import threading
import time
from collections.abc import Iterable


class Counter:
    """Monotonic counter with optional label tuples.

    Usage::

        c = Counter("freecad_mcp_tool_calls_total", "Total tool calls", labelnames=("tool", "status"))
        c.inc("create_object", "success")
    """

    def __init__(
        self,
        name: str,
        documentation: str,
        labelnames: tuple[str, ...] = (),
    ) -> None:
        self.name = name
        self.documentation = documentation
        self.labelnames = labelnames
        self._values: dict[tuple[str, ...], float] = {}
        self._lock = threading.Lock()

    def inc(self, *label_values: str, amount: float = 1.0) -> None:
        if len(label_values) != len(self.labelnames):
            raise ValueError(
                f"counter {self.name!r} expects {len(self.labelnames)} labels, "
                f"got {len(label_values)}"
            )
        if amount < 0:
            raise ValueError("Counter only increases; use Gauge for signed values")
        with self._lock:
            self._values[label_values] = self._values.get(label_values, 0.0) + amount

    def set(self, value: float, *label_values: str) -> None:
        """Set the counter to an absolute value.

        Use this when the value is observed from an external source
        (e.g. a circuit breaker's total short-circuits count) rather
        than incremented. Counter semantics normally only allow
        monotonic growth; ``set`` is provided for the snapshot case
        where the *upstream* is the source of truth.
        """
        if len(label_values) != len(self.labelnames):
            raise ValueError(
                f"counter {self.name!r} expects {len(self.labelnames)} labels, "
                f"got {len(label_values)}"
            )
        if value < 0:
            raise ValueError("Counter values cannot be negative; use Gauge for signed values")
        with self._lock:
            self._values[label_values] = float(value)

    def value(self, *label_values: str) -> float:
        return self._values.get(label_values, 0.0)

    def samples(self) -> Iterable[tuple[tuple[str, ...], float]]:
        with self._lock:
            return list(self._values.items())


class Histogram:
    """Fixed-bucket histogram with sum + count.

    Default buckets cover the latency range we care about (10ms to 60s)
    with logarithmic spacing. Operators can override via ``buckets``.
    """

    DEFAULT_BUCKETS: tuple[float, ...] = (
        0.01, 0.05, 0.1, 0.5, 1.0, 5.0, 10.0, 30.0, 60.0,
    )

    def __init__(
        self,
        name: str,
        documentation: str,
        labelnames: tuple[str, ...] = (),
        buckets: tuple[float, ...] = DEFAULT_BUCKETS,
        max_label_cardinality: int | None = None,
    ) -> None:
        if any(b <= 0 for b in buckets) or sorted(set(buckets)) != list(buckets):
            raise ValueError("buckets must be a strictly increasing sequence of positive numbers")
        self.name = name
        self.documentation = documentation
        self.labelnames = labelnames
        self.buckets = buckets
        # Hard cap on the number of distinct label tuples. When the cap
        # is reached, new label tuples are silently aggregated under a
        # single "__overflow__" bucket so a runaway label (e.g. tool
        # name with the wrong dimension) cannot grow the dicts without
        # bound. ``None`` = unlimited (legacy behaviour).
        self._max_cardinality = max_label_cardinality
        # bucket -> {labels -> count}
        self._bucket_counts: dict[float, dict[tuple[str, ...], int]] = {
            b: {} for b in self.buckets
        }
        self._sums: dict[tuple[str, ...], float] = {}
        self._counts: dict[tuple[str, ...], int] = {}
        # +Inf bucket
        self._inf: dict[tuple[str, ...], int] = {}
        self._overflow = 0
        self._lock = threading.Lock()

    def observe(self, value: float, *label_values: str) -> None:
        if len(label_values) != len(self.labelnames):
            raise ValueError(
                f"histogram {self.name!r} expects {len(self.labelnames)} labels, "
                f"got {len(label_values)}"
            )
        with self._lock:
            labels = label_values
            if (
                self._max_cardinality is not None
                and labels not in self._counts
                and len(self._counts) >= self._max_cardinality
            ):
                # Fold into the overflow bucket.
                self._overflow += 1
                return
            self._sums[labels] = self._sums.get(labels, 0.0) + value
            self._counts[labels] = self._counts.get(labels, 0) + 1
            self._inf[labels] = self._inf.get(labels, 0) + 1
            for b in self.buckets:
                if value <= b:
                    self._bucket_counts[b][labels] = (
                        self._bucket_counts[b].get(labels, 0) + 1
                    )

    def snapshot(self, *label_values: str) -> dict[str, float | int]:
        with self._lock:
            return {
                "count": self._counts.get(label_values, 0),
                "sum": self._sums.get(label_values, 0.0),
                **{f"le_{b}": self._bucket_counts[b].get(label_values, 0) for b in self.buckets},
                "le_inf": self._inf.get(label_values, 0),
            }

    def label_keys(self) -> list[tuple[str, ...]]:
        """Snapshot of distinct label tuples currently stored.

        Cheap copy taken under the same lock used by ``observe``, so
        callers (e.g. :func:`format_prometheus`) can iterate without
        racing with concurrent writes.
        """
        with self._lock:
            return list(self._counts.keys())

    @property
    def overflow(self) -> int:
        """Number of observations folded into the ``__overflow__`` bucket."""
        with self._lock:
            return self._overflow


class Gauge:
    """Settable gauge with optional labels."""

    def __init__(self, name: str, documentation: str, labelnames: tuple[str, ...] = ()) -> None:
        self.name = name
        self.documentation = documentation
        self.labelnames = labelnames
        self._values: dict[tuple[str, ...], float] = {}
        self._lock = threading.Lock()

    def set(self, value: float, *label_values: str) -> None:
        if len(label_values) != len(self.labelnames):
            raise ValueError(
                f"gauge {self.name!r} expects {len(self.labelnames)} labels, "
                f"got {len(label_values)}"
            )
        with self._lock:
            self._values[label_values] = value

    def value(self, *label_values: str) -> float:
        return self._values.get(label_values, 0.0)

    def samples(self) -> Iterable[tuple[tuple[str, ...], float]]:
        with self._lock:
            return list(self._values.items())


class MetricsRegistry:
    """Container for the MCP server's metrics.

    The default set of metrics is created eagerly so a Prometheus
    scraper sees them even before any traffic.
    """

    def __init__(self) -> None:
        self.tool_calls = Counter(
            "freecad_mcp_tool_calls_total",
            "Total tool calls received by the MCP server.",
            labelnames=("tool", "status"),
        )
        self.tool_duration = Histogram(
            "freecad_mcp_tool_duration_seconds",
            "Tool call duration in seconds.",
            labelnames=("tool",),
        )
        self.validation_failures = Counter(
            "freecad_mcp_validation_failures_total",
            "Pydantic validation failures, by tool.",
            labelnames=("tool",),
        )
        self.circuit_state = Gauge(
            "freecad_mcp_circuit_state",
            "Circuit breaker state (0=closed, 1=half_open, 2=open).",
        )
        self.circuit_short_circuits = Counter(
            "freecad_mcp_circuit_short_circuits_total",
            "Calls short-circuited by the breaker.",
        )
        self.uptime_seconds = Gauge(
            "freecad_mcp_uptime_seconds",
            "Server uptime in seconds.",
        )
        self._started_at = time.monotonic()

    def uptime(self) -> float:
        return time.monotonic() - self._started_at

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-serialisable snapshot.

        Used by the ``health_check`` MCP tool so the LLM (or a dashboard)
        can observe the server state without scraping a separate
        endpoint.
        """
        return {
            "uptime_seconds": round(self.uptime(), 3),
            "tool_calls": {
                "|".join(labels): value
                for labels, value in self.tool_calls.samples()
            },
            "tool_duration_count": {
                "|".join(labels): self.tool_duration.snapshot(*labels)["count"]
                for labels in self.tool_duration.label_keys()
            },
            "validation_failures": {
                "|".join(labels): value
                for labels, value in self.validation_failures.samples()
            },
            "circuit_state": self.circuit_state.value(),
            "circuit_short_circuits_total": self.circuit_short_circuits.value(),
        }


def format_prometheus(registry: MetricsRegistry) -> str:
    """Render the registry in Prometheus text exposition format (v0.0.4)."""
    lines: list[str] = []

    def _format_labels(label_values: tuple[str, ...], names: tuple[str, ...]) -> str:
        if not names:
            return ""
        parts = ",".join(f'{n}="{v}"' for n, v in zip(names, label_values, strict=True))
        return "{" + parts + "}"

    # Counters
    counter_metrics: tuple[Counter, ...] = (
        registry.tool_calls,
        registry.validation_failures,
        registry.circuit_short_circuits,
    )
    for metric in counter_metrics:
        lines.append(f"# HELP {metric.name} {metric.documentation}")
        lines.append(f"# TYPE {metric.name} counter")
        for labels, value in metric.samples():
            lines.append(f"{metric.name}{_format_labels(labels, metric.labelnames)} {value}")
    # Gauges
    gauge_metrics: tuple[Gauge, ...] = (registry.circuit_state, registry.uptime_seconds)
    for gauge in gauge_metrics:
        lines.append(f"# HELP {gauge.name} {gauge.documentation}")
        lines.append(f"# TYPE {gauge.name} gauge")
        # uptime is set per-call below
        if gauge is registry.uptime_seconds:
            gauge.set(registry.uptime())
        for labels, value in gauge.samples():
            lines.append(f"{gauge.name}{_format_labels(labels, gauge.labelnames)} {value}")
    # Histogram
    h = registry.tool_duration
    lines.append(f"# HELP {h.name} {h.documentation}")
    lines.append(f"# TYPE {h.name} histogram")
    for labels in h.label_keys():
        snap = h.snapshot(*labels)
        cumulative: float = 0.0
        for b in h.buckets:
            cumulative = float(snap[f"le_{b}"])
            le_labels = labels + (str(b),)
            lines.append(
                f"{h.name}_bucket{_format_labels(le_labels, h.labelnames + ('le',))} {cumulative}"
            )
        lines.append(
            f"{h.name}_bucket{_format_labels(labels + ('+Inf',), h.labelnames + ('le',))} "
            f"{snap['le_inf']}"
        )
        lines.append(f"{h.name}_sum{_format_labels(labels, h.labelnames)} {snap['sum']}")
        lines.append(f"{h.name}_count{_format_labels(labels, h.labelnames)} {snap['count']}")
    return "\n".join(lines) + "\n"


__all__ = [
    "Counter",
    "Histogram",
    "Gauge",
    "MetricsRegistry",
    "format_prometheus",
]
