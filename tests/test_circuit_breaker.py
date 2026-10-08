"""Integration tests for circuit breaker functionality.

Tests the per-upstream circuit breaker implementation including:
- Circuit breaker state transitions
- Per-upstream isolation
- Retry behavior with circuit breaker
- Health endpoint integration
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import pytest
from fastapi import status

from llm_assistant_api.proxy import (
    _CircuitBreaker,
    get_all_circuit_breakers,
    get_circuit_breaker,
    reset_circuit_breaker,
)


class TestCircuitBreakerUnit:
    """Unit tests for the circuit breaker itself."""

    def test_initial_state(self) -> None:
        """Circuit breaker starts in closed state."""
        cb = _CircuitBreaker()
        assert cb.state == "closed"
        assert cb.failure_count == 0
        can_proceed, reason = cb.can_proceed()
        assert can_proceed is True
        assert reason == ""

    def test_record_success_resets(self) -> None:
        """Recording success clears failures and closes circuit."""
        cb = _CircuitBreaker(failure_threshold=3)

        # Add some failures
        for _ in range(2):
            cb.record_failure()

        assert cb.failure_count == 2

        # Record success
        cb.record_success()

        assert cb.failure_count == 0
        assert cb.state == "closed"

    def test_record_failure_within_window(self) -> None:
        """Failures within window accumulate."""
        cb = _CircuitBreaker(failure_window=10.0, failure_threshold=3)

        cb.record_failure()
        cb.record_failure()

        assert cb.failure_count == 2
        assert cb.state == "closed"

    def test_circuit_opens_on_threshold(self) -> None:
        """Circuit opens when failures reach threshold."""
        cb = _CircuitBreaker(failure_threshold=3)

        for _ in range(3):
            cb.record_failure()

        assert cb.state == "open"
        can_proceed, reason = cb.can_proceed()
        assert can_proceed is False
        assert "circuit breaker open" in reason

    def test_circuit_transitions_to_half_open(self) -> None:
        """Circuit transitions to half-open after timeout."""
        cb = _CircuitBreaker(failure_threshold=2, open_timeout=0.1)

        # Trip the circuit
        cb.record_failure()
        cb.record_failure()
        assert cb.state == "open"

        # Wait for timeout
        time.sleep(0.15)

        can_proceed, reason = cb.can_proceed()
        assert can_proceed is True
        assert cb.state == "half-open"
        assert "half-open" in reason

    def test_half_open_success_closes_circuit(self) -> None:
        """Success in half-open state closes circuit."""
        cb = _CircuitBreaker(failure_threshold=2, open_timeout=0.1)

        # Trip and wait
        cb.record_failure()
        cb.record_failure()
        time.sleep(0.15)
        cb.can_proceed()  # Transition to half-open

        # Record success
        cb.record_success()

        assert cb.state == "closed"
        assert cb.failure_count == 0

    def test_half_open_failure_reopens_circuit(self) -> None:
        """Failure in half-open state reopens circuit."""
        cb = _CircuitBreaker(failure_threshold=2, open_timeout=0.1)

        # Trip and wait
        cb.record_failure()
        cb.record_failure()
        time.sleep(0.15)
        cb.can_proceed()  # Transition to half-open

        # Record failure
        cb.record_failure()

        assert cb.state == "open"

    def test_failure_window_expires(self) -> None:
        """Failures outside the window don't count."""
        cb = _CircuitBreaker(failure_window=0.1, failure_threshold=3)

        cb.record_failure()
        cb.record_failure()

        # Wait for window to expire
        time.sleep(0.15)

        # Should have expired
        assert cb.failure_count == 0

        # Add new failures
        cb.record_failure()
        cb.record_failure()
        assert cb.failure_count == 2


class TestPerUpstreamCircuitBreakers:
    """Tests for per-upstream circuit breaker isolation."""

    def test_get_circuit_breaker_creates_new(self) -> None:
        """Getting circuit breaker for new URL creates it."""
        reset_circuit_breaker()  # Clean slate

        cb = get_circuit_breaker("http://upstream1.test")
        assert cb is not None
        assert cb.state == "closed"

    def test_get_circuit_breaker_returns_same(self) -> None:
        """Getting circuit breaker for same URL returns same instance."""
        reset_circuit_breaker()

        cb1 = get_circuit_breaker("http://upstream1.test")
        cb2 = get_circuit_breaker("http://upstream1.test")

        assert cb1 is cb2

    def test_different_upstreams_isolated(self) -> None:
        """Different upstreams have independent circuit breakers."""
        reset_circuit_breaker()

        cb1 = get_circuit_breaker("http://upstream1.test")
        cb2 = get_circuit_breaker("http://upstream2.test")

        # Trip cb1
        for _ in range(5):
            cb1.record_failure()

        assert cb1.state == "open"
        assert cb2.state == "closed"

    def test_reset_specific_upstream(self) -> None:
        """Resetting specific upstream doesn't affect others."""
        reset_circuit_breaker()

        cb1 = get_circuit_breaker("http://upstream1.test")
        cb2 = get_circuit_breaker("http://upstream2.test")

        # Trip both
        for _ in range(5):
            cb1.record_failure()
            cb2.record_failure()

        assert cb1.state == "open"
        assert cb2.state == "open"

        # Reset only upstream1
        reset_circuit_breaker("http://upstream1.test")

        assert cb1.state == "closed"
        assert cb2.state == "open"

    def test_reset_all_upstreams(self) -> None:
        """Reset without argument resets all."""
        reset_circuit_breaker()

        cb1 = get_circuit_breaker("http://upstream1.test")
        cb2 = get_circuit_breaker("http://upstream2.test")

        # Trip both
        for _ in range(5):
            cb1.record_failure()
            cb2.record_failure()

        # Reset all
        reset_circuit_breaker()

        assert cb1.state == "closed"
        assert cb2.state == "closed"

    def test_get_all_circuit_breakers(self) -> None:
        """Get all circuit breakers returns dict."""
        reset_circuit_breaker()

        get_circuit_breaker("http://upstream1.test")
        get_circuit_breaker("http://upstream2.test")

        all_cbs = get_all_circuit_breakers()

        assert len(all_cbs) == 2
        assert "http://upstream1.test" in all_cbs
        assert "http://upstream2.test" in all_cbs


class TestCircuitBreakerIntegration:
    """Integration tests with the gateway."""

    @pytest.mark.asyncio
    async def test_circuit_breaker_rejects_when_open(
        self, gateway_factory: Any, upstream: Any
    ) -> None:
        """Gateway rejects requests when circuit is open."""
        from llm_assistant_api.config import Settings

        settings = Settings(
            upstream_base_url="http://upstream1.test",
            model_id="test-model",
            api_keys="",
        )

        # Manually trip the circuit breaker
        cb = get_circuit_breaker("http://upstream1.test")
        for _ in range(5):
            cb.record_failure()

        assert cb.state == "open"

        # Setup upstream handler (shouldn't be called)
        upstream.json_response("POST", "/v1/chat/completions", {"choices": []})

        gateway = await gateway_factory(settings)

        # Request should be rejected
        response = await gateway.post(
            "/v1/chat/completions",
            json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
        )

        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        data = response.json()
        assert "circuit breaker" in data["error"]["message"].lower()

    @pytest.mark.asyncio
    async def test_circuit_breaker_allows_when_closed(
        self, gateway_factory: Any, upstream: Any
    ) -> None:
        """Gateway allows requests when circuit is closed."""
        from llm_assistant_api.config import Settings

        settings = Settings(
            upstream_base_url="http://upstream1.test",
            model_id="test-model",
            api_keys="",
        )

        # Ensure circuit is closed
        reset_circuit_breaker("http://upstream1.test")

        # Setup upstream handler
        upstream.json_response(
            "POST",
            "/v1/chat/completions",
            {"choices": [{"message": {"content": "hello"}}]},
        )

        gateway = await gateway_factory(settings)

        response = await gateway.post(
            "/v1/chat/completions",
            json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
        )

        assert response.status_code == status.HTTP_200_OK

    @pytest.mark.asyncio
    async def test_circuit_breaker_records_failures(
        self, gateway_factory: Any, upstream: Any
    ) -> None:
        """Gateway records failures in circuit breaker."""
        from llm_assistant_api.config import Settings

        settings = Settings(
            upstream_base_url="http://upstream1.test",
            model_id="test-model",
            api_keys="",
        )

        reset_circuit_breaker("http://upstream1.test")
        cb = get_circuit_breaker("http://upstream1.test")

        # Setup upstream to fail
        upstream.failure(
            "POST",
            "/v1/chat/completions",
            httpx.ConnectError("Connection refused"),
        )

        gateway = await gateway_factory(settings)

        # Make failing requests
        import contextlib

        for _ in range(3):
            with contextlib.suppress(Exception):
                await gateway.post(
                    "/v1/chat/completions",
                    json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
                )

        # Should have recorded failures
        assert cb.failure_count >= 1


class TestHealthEndpointIntegration:
    """Tests for health endpoints with circuit breakers."""

    @pytest.mark.asyncio
    async def test_circuit_breakers_endpoint(
        self, gateway_factory: Any, upstream: Any
    ) -> None:
        """Health endpoint returns circuit breaker status."""
        from llm_assistant_api.config import Settings

        settings = Settings(
            upstream_base_url="http://upstream1.test",
            model_id="test-model",
            api_keys="",
        )

        reset_circuit_breaker()

        # Create some circuit breakers
        get_circuit_breaker("http://upstream1.test")
        get_circuit_breaker("http://upstream2.test")

        gateway = await gateway_factory(settings)

        response = await gateway.get("/admin/circuit-breakers")

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert "http://upstream1.test" in data
        assert "http://upstream2.test" in data
        assert data["http://upstream1.test"]["state"] == "closed"

    @pytest.mark.asyncio
    async def test_circuit_breaker_reset_endpoint(
        self, gateway_factory: Any, upstream: Any
    ) -> None:
        """Health endpoint can reset circuit breakers."""
        from llm_assistant_api.config import Settings

        settings = Settings(
            upstream_base_url="http://upstream1.test",
            model_id="test-model",
            api_keys="",
        )

        reset_circuit_breaker()
        cb = get_circuit_breaker("http://upstream1.test")

        # Trip the circuit
        for _ in range(5):
            cb.record_failure()

        assert cb.state == "open"

        gateway = await gateway_factory(settings)

        # Reset via endpoint
        response = await gateway.post("/admin/circuit-breaker/reset")

        assert response.status_code == status.HTTP_200_OK
        assert cb.state == "closed"

    @pytest.mark.asyncio
    async def test_readyz_detailed_includes_circuit_breakers(
        self, gateway_factory: Any, upstream: Any
    ) -> None:
        """Detailed readiness includes circuit breaker status."""
        from llm_assistant_api.config import Settings

        settings = Settings(
            upstream_base_url="http://upstream1.test",
            model_id="test-model",
            api_keys="",
        )

        reset_circuit_breaker()

        # Setup upstream health
        upstream.on("GET", "http://vllm.test:8999/health", lambda _req: httpx.Response(200))

        gateway = await gateway_factory(settings)

        response = await gateway.get("/readyz/detailed")

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert "circuit_breakers" in data
        assert "http://upstream1.test" in data["circuit_breakers"]
