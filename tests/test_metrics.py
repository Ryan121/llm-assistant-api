"""Metrics collection and exposition.

The regression worth guarding is the original one: ``get_request_stats``
computed its averages over the *in-flight* map, which ``finish_request`` had
already emptied, so the latency summary could only ever report zero.
"""

from __future__ import annotations

import httpx

from llm_assistant_api.metrics import MetricsCollector

from .conftest import UPSTREAM, FakeUpstream, GatewayFactory, make_settings

CHAT_URL = f"{UPSTREAM}/chat/completions"
METRICS_URL = f"{UPSTREAM.removesuffix('/v1')}/metrics"


def _completed(collector: MetricsCollector, count: int, status: int = 200) -> None:
    for index in range(count):
        request_id = f"rid-{index}"
        metrics = collector.start_request(request_id, "/v1/chat/completions", "model", False)
        metrics.first_byte()
        collector.finish_request(request_id, status)


def test_latency_stats_survive_request_completion() -> None:
    collector = MetricsCollector()
    _completed(collector, 10)

    stats = collector.get_request_stats()

    assert stats["sample_size"] == 10
    assert stats["average_response_time_ms"] > 0
    assert stats["active_requests"] == 0


def test_stats_report_percentiles() -> None:
    collector = MetricsCollector()
    _completed(collector, 100)

    stats = collector.get_request_stats()

    assert stats["p50_response_time_ms"] <= stats["p95_response_time_ms"]
    assert stats["p95_response_time_ms"] <= stats["p99_response_time_ms"]
    assert stats["p50_ttft_ms"] > 0


def test_stats_are_empty_before_any_request_completes() -> None:
    collector = MetricsCollector()
    collector.start_request("rid", "/v1/chat/completions", "model", False)

    stats = collector.get_request_stats()

    assert stats["sample_size"] == 0
    assert stats["active_requests"] == 1


def test_history_is_bounded() -> None:
    """A long-lived server must not accumulate one record per request forever."""
    collector = MetricsCollector()
    _completed(collector, 1500)

    assert collector.get_request_stats()["sample_size"] <= 1024
    # The lifetime counter is unaffected by the ring buffer.
    assert collector.get_summary()["total_requests"] == 1500


def test_finishing_an_unknown_request_is_a_no_op() -> None:
    collector = MetricsCollector()

    collector.finish_request("never-started", 200)

    assert collector.get_summary()["total_requests"] == 0


def test_prometheus_rendering_is_well_formed() -> None:
    collector = MetricsCollector()
    _completed(collector, 5)

    text = collector.render_prometheus()

    assert "# TYPE gateway_requests_total counter" in text
    assert "gateway_requests_total 5" in text
    assert 'gateway_response_time_ms{quantile="p95"}' in text
    assert text.endswith("\n")


def test_prometheus_rendering_works_with_no_traffic() -> None:
    assert "gateway_requests_total 0" in MetricsCollector().render_prometheus()


# --- through the gateway ---------------------------------------------------


async def test_metrics_endpoint_reports_completed_requests(
    client: httpx.AsyncClient, upstream: FakeUpstream
) -> None:
    upstream.json_response("POST", CHAT_URL, {"id": "c", "choices": []})
    await client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "x"}]})

    body = (await client.get("/metrics")).json()

    assert body["summary"]["total_requests"] >= 1
    assert body["request_stats"]["sample_size"] >= 1


async def test_model_usage_is_recorded(client: httpx.AsyncClient, upstream: FakeUpstream) -> None:
    """The middleware cannot see the model; the route has to report it."""
    upstream.json_response("POST", CHAT_URL, {"id": "c", "choices": []})
    await client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "x"}]})

    usage = (await client.get("/metrics")).json()["summary"]["model_usage"]

    assert sum(usage.values()) >= 1


async def test_metrics_endpoint_speaks_prometheus_on_request(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/metrics", params={"format": "prometheus"})

    assert response.status_code == httpx.codes.OK
    assert response.headers["content-type"].startswith("text/plain")
    assert "gateway_requests_total" in response.text


async def test_upstream_metrics_are_proxied(
    client: httpx.AsyncClient, upstream: FakeUpstream
) -> None:
    upstream.on(
        "GET",
        METRICS_URL,
        lambda _req: httpx.Response(
            200, text="vllm:gpu_cache_usage_perc 0.42\n", headers={"content-type": "text/plain"}
        ),
    )

    response = await client.get("/metrics/upstream")

    assert response.status_code == httpx.codes.OK
    assert "vllm:gpu_cache_usage_perc" in response.text


async def test_upstream_metrics_report_an_unreachable_engine(
    gateway_factory: GatewayFactory, upstream: FakeUpstream
) -> None:
    client = await gateway_factory(make_settings())
    upstream.failure("GET", METRICS_URL, httpx.ConnectError("refused"))

    response = await client.get("/metrics/upstream")

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "upstream_unavailable"
