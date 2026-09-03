"""Unit tests for the sliding-window rate limiter and its wiring into auth.

The limiter reads the wall clock, so the window tests drive a fake clock
instead of sleeping - a real one-second window would make the suite slow and
the assertions timing-dependent.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from llm_assistant_api import deps
from llm_assistant_api.rate_limiting import RateLimitConfig, RateLimiter
from tests.conftest import FakeUpstream, GatewayFactory, make_settings

CHAT_URL = "http://vllm.test:8999/v1/chat/completions"
CHAT_REQUEST = {"model": "anything", "messages": [{"role": "user", "content": "hi"}]}


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Callable[[float], None]:
    """Freeze ``time.time`` for the limiter and return a setter to advance it."""
    now = 1_000.0

    def advance(seconds: float) -> None:
        nonlocal now
        now += seconds

    monkeypatch.setattr("llm_assistant_api.rate_limiting.time.time", lambda: now)
    return advance


# --------------------------------------------------------------------------
# RateLimitConfig
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"max_requests": 0}, "max_requests must be positive"),
        ({"max_requests": -1}, "max_requests must be positive"),
        ({"window_seconds": 0}, "window_seconds must be positive"),
        ({"window_seconds": -5}, "window_seconds must be positive"),
    ],
)
async def test_nonsensical_config_is_rejected_at_construction(
    kwargs: dict[str, int], message: str
) -> None:
    """A zero limit would lock everyone out; a zero window would never evict."""
    with pytest.raises(ValueError, match=message):
        RateLimitConfig(**kwargs)


async def test_default_config_is_accepted() -> None:
    config = RateLimitConfig()

    assert config.max_requests == 100
    assert config.window_seconds == 60


async def test_limiter_defaults_its_config_when_none_is_given() -> None:
    assert RateLimiter().config == RateLimitConfig()


# --------------------------------------------------------------------------
# RateLimiter
# --------------------------------------------------------------------------


async def test_requests_under_the_limit_are_allowed(clock: Callable[[float], None]) -> None:
    limiter = RateLimiter(RateLimitConfig(max_requests=3, window_seconds=60))

    assert [limiter.is_allowed("caller") for _ in range(3)] == [True, True, True]


async def test_the_request_over_the_limit_is_refused(clock: Callable[[float], None]) -> None:
    limiter = RateLimiter(RateLimitConfig(max_requests=2, window_seconds=60))

    assert limiter.is_allowed("caller") is True
    assert limiter.is_allowed("caller") is True
    assert limiter.is_allowed("caller") is False


async def test_identifiers_get_independent_budgets(clock: Callable[[float], None]) -> None:
    """One noisy editor must not rate-limit everyone else on the box."""
    limiter = RateLimiter(RateLimitConfig(max_requests=1, window_seconds=60))

    assert limiter.is_allowed("noisy") is True
    assert limiter.is_allowed("noisy") is False
    assert limiter.is_allowed("quiet") is True


async def test_the_window_slides_rather_than_resetting_in_blocks(
    clock: Callable[[float], None],
) -> None:
    limiter = RateLimiter(RateLimitConfig(max_requests=2, window_seconds=60))

    limiter.is_allowed("caller")  # t=0
    clock(30)
    limiter.is_allowed("caller")  # t=30
    assert limiter.is_allowed("caller") is False

    # At t=61 only the first request has aged out, so exactly one slot frees up.
    clock(31)
    assert limiter.is_allowed("caller") is True
    assert limiter.is_allowed("caller") is False


async def test_a_refused_request_is_not_counted_against_the_window(
    clock: Callable[[float], None],
) -> None:
    """Rejections must not extend the ban, or a hot-looping client never recovers."""
    limiter = RateLimiter(RateLimitConfig(max_requests=1, window_seconds=60))

    limiter.is_allowed("caller")  # t=0
    clock(30)
    for _ in range(5):
        assert limiter.is_allowed("caller") is False

    clock(31)  # t=61: the only recorded request has aged out
    assert limiter.is_allowed("caller") is True


async def test_reset_time_is_when_the_oldest_request_ages_out(
    clock: Callable[[float], None],
) -> None:
    limiter = RateLimiter(RateLimitConfig(max_requests=1, window_seconds=60))

    limiter.is_allowed("caller")  # t=1000

    assert limiter.get_reset_time("caller") == 1_060.0


async def test_reset_time_for_an_unseen_caller_is_now(clock: Callable[[float], None]) -> None:
    assert RateLimiter().get_reset_time("never-seen") == 1_000.0


async def test_remaining_requests_counts_down_and_floors_at_zero(
    clock: Callable[[float], None],
) -> None:
    limiter = RateLimiter(RateLimitConfig(max_requests=2, window_seconds=60))

    assert limiter.get_remaining_requests("caller") == 2
    limiter.is_allowed("caller")
    assert limiter.get_remaining_requests("caller") == 1
    limiter.is_allowed("caller")
    assert limiter.get_remaining_requests("caller") == 0
    limiter.is_allowed("caller")
    assert limiter.get_remaining_requests("caller") == 0


async def test_remaining_requests_recovers_as_the_window_slides(
    clock: Callable[[float], None],
) -> None:
    limiter = RateLimiter(RateLimitConfig(max_requests=2, window_seconds=60))

    limiter.is_allowed("caller")
    limiter.is_allowed("caller")
    assert limiter.get_remaining_requests("caller") == 0

    clock(61)

    assert limiter.get_remaining_requests("caller") == 2


# --------------------------------------------------------------------------
# Wiring into require_api_key
# --------------------------------------------------------------------------


@pytest.fixture
def strict_limiter(monkeypatch: pytest.MonkeyPatch) -> RateLimiter:
    """Swap the process-wide limiter for a one-request-per-minute one.

    ``deps`` binds ``rate_limiter`` at import time, so the name is patched
    there rather than on the defining module.
    """
    limiter = RateLimiter(RateLimitConfig(max_requests=1, window_seconds=60))
    monkeypatch.setattr(deps, "rate_limiter", limiter)
    return limiter


async def test_exceeding_the_limit_is_a_429_with_an_openai_error_body(
    gateway_factory: GatewayFactory, upstream: FakeUpstream, strict_limiter: RateLimiter
) -> None:
    client = await gateway_factory(make_settings(api_keys="the-key"))
    upstream.json_response("POST", CHAT_URL, {"id": "x"})
    headers = {"authorization": "Bearer the-key"}

    first = await client.post("/v1/chat/completions", json=CHAT_REQUEST, headers=headers)
    second = await client.post("/v1/chat/completions", json=CHAT_REQUEST, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "rate_limit_exceeded"
    assert len(upstream.requests) == 1, "a throttled request must not reach the GPU"


async def test_a_429_tells_the_client_when_to_retry(
    gateway_factory: GatewayFactory, upstream: FakeUpstream, strict_limiter: RateLimiter
) -> None:
    """Editors back off on these headers; without them they hammer the gateway."""
    client = await gateway_factory(make_settings(api_keys="the-key"))
    upstream.json_response("POST", CHAT_URL, {"id": "x"})
    headers = {"authorization": "Bearer the-key"}

    await client.post("/v1/chat/completions", json=CHAT_REQUEST, headers=headers)
    throttled = await client.post("/v1/chat/completions", json=CHAT_REQUEST, headers=headers)

    assert 0 <= int(throttled.headers["retry-after"]) <= 60
    assert throttled.headers["x-ratelimit-limit"] == "1"
    assert throttled.headers["x-ratelimit-remaining"] == "0"


async def test_an_open_gateway_is_not_rate_limited(
    client: httpx.AsyncClient, upstream: FakeUpstream, strict_limiter: RateLimiter
) -> None:
    """With no API_KEYS the gateway short-circuits before the limiter."""
    upstream.json_response("POST", CHAT_URL, {"id": "x"})

    for _ in range(3):
        response = await client.post("/v1/chat/completions", json=CHAT_REQUEST)
        assert response.status_code == 200


async def test_distinct_keys_are_throttled_independently(
    gateway_factory: GatewayFactory, upstream: FakeUpstream, strict_limiter: RateLimiter
) -> None:
    client = await gateway_factory(make_settings(api_keys="key-one,key-two"))
    upstream.json_response("POST", CHAT_URL, {"id": "x"})

    first = await client.post(
        "/v1/chat/completions", json=CHAT_REQUEST, headers={"authorization": "Bearer key-one"}
    )
    other = await client.post(
        "/v1/chat/completions", json=CHAT_REQUEST, headers={"authorization": "Bearer key-two"}
    )

    assert (first.status_code, other.status_code) == (200, 200)


async def test_anonymous_callers_are_throttled_by_forwarded_ip(
    gateway_factory: GatewayFactory, strict_limiter: RateLimiter
) -> None:
    """Behind a proxy every caller shares one socket, so x-forwarded-for decides."""
    client = await gateway_factory(make_settings(api_keys="the-key"))

    first = await client.post(
        "/v1/chat/completions",
        json=CHAT_REQUEST,
        headers={"x-forwarded-for": "203.0.113.7, 10.0.0.1"},
    )
    repeat = await client.post(
        "/v1/chat/completions",
        json=CHAT_REQUEST,
        headers={"x-forwarded-for": "203.0.113.7, 10.0.0.1"},
    )
    other_ip = await client.post(
        "/v1/chat/completions",
        json=CHAT_REQUEST,
        headers={"x-forwarded-for": "198.51.100.4"},
    )

    # No credentials, so each is also a 401 - but the throttle lands first.
    assert first.status_code == 401
    assert repeat.status_code == 429
    assert other_ip.status_code == 401


async def test_throttling_is_logged_for_the_operator(
    gateway_factory: GatewayFactory,
    upstream: FakeUpstream,
    strict_limiter: RateLimiter,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = await gateway_factory(make_settings(api_keys="the-key"))
    upstream.json_response("POST", CHAT_URL, {"id": "x"})
    headers = {"authorization": "Bearer the-key"}

    await client.post("/v1/chat/completions", json=CHAT_REQUEST, headers=headers)
    with caplog.at_level("WARNING", logger="llm_assistant_api.deps"):
        await client.post("/v1/chat/completions", json=CHAT_REQUEST, headers=headers)

    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "rate limited request" in logged
    assert "the-key" not in logged, "the full token must never reach the log"
