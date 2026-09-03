"""Metrics collection.

The numbers that matter for a single-box deployment are latency percentiles and
time-to-first-token, not counters: "is it slow?" is always about the tail. Both
are kept in a bounded ring buffer so the process cannot grow without limit on a
long-lived server.

Exposed twice - as JSON for humans reading ``curl``, and in Prometheus text
format for anything scraping. vLLM's own ``/metrics`` (queue depth, KV-cache
utilisation, preemptions) is proxied alongside by ``routes/health.py``, because
tuning decisions need both halves.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

#: How many completed requests to keep for percentile maths. At a couple of
#: requests a second this is about twenty minutes of history, which is the
#: window you actually look at when something feels slow.
_HISTORY = 1024


@dataclass
class RequestMetrics:
    """Tracks metrics for a single in-flight request."""

    request_id: str
    endpoint: str
    model: str
    start_time: float
    response_time_ms: float | None = None
    status_code: int | None = None
    streamed: bool = False
    #: Time to first byte of the response body. For a streamed completion this
    #: is time-to-first-token, the number that decides whether the editor feels
    #: responsive; total duration mostly measures how long the answer was.
    ttft_ms: float | None = None

    def first_byte(self) -> None:
        if self.ttft_ms is None:
            self.ttft_ms = (time.perf_counter() - self.start_time) * 1000

    def finish(self, status_code: int) -> None:
        self.response_time_ms = (time.perf_counter() - self.start_time) * 1000
        self.status_code = status_code


def _percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile. ``values`` must be sorted and non-empty."""
    index = min(len(values) - 1, int(len(values) * fraction))
    return values[index]


class MetricsCollector:
    """Collects and aggregates metrics for the application.

    Guarded by a lock because uvicorn may run several worker threads, and a
    ``defaultdict`` mutated from two of them at once loses counts.
    """

    def __init__(self) -> None:
        self._active: dict[str, RequestMetrics] = {}
        self._completed: deque[RequestMetrics] = deque(maxlen=_HISTORY)
        self._endpoint_counts: defaultdict[str, int] = defaultdict(int)
        self._status_code_counts: defaultdict[int, int] = defaultdict(int)
        self._model_usage: defaultdict[str, int] = defaultdict(int)
        self._total_requests = 0
        self._lock = threading.Lock()

    def start_request(
        self, request_id: str, endpoint: str, model: str, streamed: bool = False
    ) -> RequestMetrics:
        """Start tracking a new request."""
        metrics = RequestMetrics(
            request_id=request_id,
            endpoint=endpoint,
            model=model,
            start_time=time.perf_counter(),
            streamed=streamed,
        )
        with self._lock:
            self._active[request_id] = metrics
            self._total_requests += 1
            self._endpoint_counts[endpoint] += 1
        return metrics

    def finish_request(self, request_id: str, status_code: int) -> None:
        """Finish tracking a request and retain it for percentile maths."""
        with self._lock:
            metrics = self._active.pop(request_id, None)
            if metrics is None:
                return
            if metrics.response_time_ms is None:
                metrics.finish(status_code)
            self._status_code_counts[status_code] += 1
            # Retained rather than discarded: throwing the record away here was
            # why the latency summary could only ever report zero.
            self._completed.append(metrics)

    def record_model_usage(self, model: str) -> None:
        """Record usage of a specific model."""
        with self._lock:
            self._model_usage[model] += 1

    def get_summary(self) -> dict[str, Any]:
        """Get a summary of collected metrics."""
        with self._lock:
            return {
                "total_requests": self._total_requests,
                "endpoint_counts": dict(self._endpoint_counts),
                "status_code_counts": dict(self._status_code_counts),
                "model_usage": dict(self._model_usage),
                "active_requests": len(self._active),
                "timestamp": datetime.now(UTC).isoformat(),
            }

    def get_request_stats(self) -> dict[str, Any]:
        """Latency statistics over the retained window of completed requests."""
        with self._lock:
            active = len(self._active)
            durations = sorted(
                m.response_time_ms for m in self._completed if m.response_time_ms is not None
            )
            ttfts = sorted(m.ttft_ms for m in self._completed if m.ttft_ms is not None)

        if not durations:
            return {"average_response_time_ms": 0, "active_requests": active, "sample_size": 0}

        stats: dict[str, Any] = {
            "active_requests": active,
            "sample_size": len(durations),
            "average_response_time_ms": sum(durations) / len(durations),
            "min_response_time_ms": durations[0],
            "max_response_time_ms": durations[-1],
            "p50_response_time_ms": _percentile(durations, 0.50),
            "p95_response_time_ms": _percentile(durations, 0.95),
            "p99_response_time_ms": _percentile(durations, 0.99),
        }
        if ttfts:
            stats["p50_ttft_ms"] = _percentile(ttfts, 0.50)
            stats["p95_ttft_ms"] = _percentile(ttfts, 0.95)
        return stats

    def render_prometheus(self) -> str:
        """Exposition format, so this can be scraped rather than eyeballed."""
        summary = self.get_summary()
        stats = self.get_request_stats()
        lines: list[str] = []

        def metric(name: str, kind: str, help_text: str) -> None:
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} {kind}")

        metric("gateway_requests_total", "counter", "Requests accepted by the gateway.")
        lines.append(f"gateway_requests_total {summary['total_requests']}")

        metric("gateway_requests_in_flight", "gauge", "Requests currently being served.")
        lines.append(f"gateway_requests_in_flight {summary['active_requests']}")

        metric("gateway_requests_by_endpoint_total", "counter", "Requests per endpoint.")
        for endpoint, count in sorted(summary["endpoint_counts"].items()):
            lines.append(f'gateway_requests_by_endpoint_total{{endpoint="{endpoint}"}} {count}')

        metric("gateway_responses_by_status_total", "counter", "Responses per status code.")
        for status, count in sorted(summary["status_code_counts"].items()):
            lines.append(f'gateway_responses_by_status_total{{status="{status}"}} {count}')

        metric("gateway_response_time_ms", "gauge", "Response time over the retained window.")
        for quantile in ("p50", "p95", "p99"):
            value = stats.get(f"{quantile}_response_time_ms")
            if value is not None:
                lines.append(f'gateway_response_time_ms{{quantile="{quantile}"}} {value:.3f}')

        metric("gateway_ttft_ms", "gauge", "Time to first token over the retained window.")
        for quantile in ("p50", "p95"):
            value = stats.get(f"{quantile}_ttft_ms")
            if value is not None:
                lines.append(f'gateway_ttft_ms{{quantile="{quantile}"}} {value:.3f}')

        return "\n".join(lines) + "\n"


# Global metrics collector instance
metrics_collector = MetricsCollector()
