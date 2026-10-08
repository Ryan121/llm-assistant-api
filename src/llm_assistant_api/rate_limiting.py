"""Rate limiting utilities for the LLM Assistant API."""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class RateLimitConfig:
    """Configuration for rate limiting."""

    max_requests: int = 100  # Maximum requests per time window
    window_seconds: int = 60  # Time window in seconds
    cleanup_interval: int = 300  # How often to clean stale entries (seconds)
    redis_url: str | None = None  # If set, use Redis for distributed rate limiting

    def __post_init__(self) -> None:
        if self.max_requests <= 0:
            raise ValueError("max_requests must be positive")
        if self.window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        if self.cleanup_interval <= 0:
            raise ValueError("cleanup_interval must be positive")


class RateLimiter:
    """Simple sliding window rate limiter with automatic cleanup.

    To prevent memory growth from stale entries, the limiter tracks the last
    cleanup time and periodically removes identifiers that have no recent requests.

    Supports per-endpoint configuration and optional Redis backend for
    multi-instance deployments.
    """

    def __init__(self, config: RateLimitConfig | None = None) -> None:
        if config is None:
            config = RateLimitConfig()
        self.config = config
        self._requests: dict[str, deque[float]] = defaultdict(deque)
        self._endpoint_configs: dict[str, tuple[int, int]] = {}
        self._last_cleanup = time.time()
        self._lock = __import__("threading").Lock()
        self._redis_client: Any | None = None
        if config.redis_url:
            self._init_redis(config.redis_url)

    def _init_redis(self, redis_url: str) -> None:
        """Initialize Redis client for distributed rate limiting."""
        try:
            import redis.asyncio as redis
            self._redis_client = redis.from_url(redis_url, decode_responses=True)
            log.info("Redis rate limiter initialized")
        except ImportError:
            log.warning(
                "Redis URL provided but redis package not installed. "
                "Using in-memory limiter."
            )
            self._redis_client = None
        except Exception as exc:
            log.warning(
                "Failed to connect to Redis: %s. Using in-memory limiter.", exc
            )
            self._redis_client = None

    def configure_endpoint(
        self, endpoint: str, max_requests: int, window_seconds: int
    ) -> None:
        """Configure rate limits for a specific endpoint."""
        with self._lock:
            self._endpoint_configs[endpoint] = (max_requests, window_seconds)
            log.debug(
                "Rate limit configured for %s: %d requests per %ds",
                endpoint,
                max_requests,
                window_seconds,
            )

    def _get_config_for_endpoint(self, endpoint: str) -> tuple[int, int]:
        """Get rate limit config for an endpoint, or global defaults."""
        with self._lock:
            return self._endpoint_configs.get(
                endpoint, (self.config.max_requests, self.config.window_seconds)
            )

    def is_allowed(self, identifier: str, endpoint: str = "default") -> bool:
        """
        Check if a request from the given identifier is allowed.

        Args:
            identifier: Unique identifier for the requester (e.g., IP address, API key)
            endpoint: The endpoint being accessed (for per-endpoint limits)

        Returns:
            True if request is allowed, False if rate limited
        """
        max_requests, window_seconds = self._get_config_for_endpoint(endpoint)
        now = time.time()

        # Use Redis if available for distributed rate limiting
        if self._redis_client is not None:
            return self._is_allowed_redis(identifier, endpoint, max_requests, window_seconds, now)

        with self._lock:
            self._maybe_cleanup(now)
            key = f"{endpoint}:{identifier}"
            request_times = self._requests[key]

            # Remove requests outside the current window
            while request_times and request_times[0] <= now - window_seconds:
                request_times.popleft()

            # Check if we're under the limit
            if len(request_times) < max_requests:
                request_times.append(now)
                return True

        return False

    async def _is_allowed_redis(
        self,
        identifier: str,
        endpoint: str,
        max_requests: int,
        window_seconds: int,
        now: float,
    ) -> bool:
        """Check rate limit using Redis for distributed deployments.

        Uses a sorted set (ZSET) where scores are timestamps. This allows
        efficient removal of old entries and counting of current ones.

        Fail-open: if Redis is unavailable, requests are allowed to prevent
        blocking all traffic.
        """
        import redis.asyncio as redis

        key = f"ratelimit:{endpoint}:{identifier}"
        window_start = now - window_seconds

        try:
            async with self._redis_client.pipeline() as pipe:
                # Remove old entries outside the window
                pipe.zremrangebyscore(key, "-inf", window_start)
                # Count current entries in the window
                pipe.zcard(key)
                results = await pipe.execute()

            current_count = results[1]

            if current_count < max_requests:
                # Add this request and set expiry
                async with self._redis_client.pipeline() as pipe:
                    pipe.zadd(key, {str(now): now})
                    pipe.expire(key, window_seconds + 1)  # Auto-cleanup
                    await pipe.execute()
                return True

            return False
        except redis.RedisError as exc:
            log.warning("Redis rate limit check failed: %s. Allowing request.", exc)
            return True  # Fail open to prevent blocking all traffic
        except Exception as exc:
            log.warning("Unexpected error in Redis rate limit: %s. Allowing request.", exc)
            return True  # Fail open

    def get_reset_time(self, identifier: str, endpoint: str = "default") -> float:
        """Get the time when the rate limit will reset for this identifier."""
        _, window_seconds = self._get_config_for_endpoint(endpoint)
        now = time.time()

        if self._redis_client is not None:
            # Redis implementation would go here (simplified for now)
            pass

        with self._lock:
            key = f"{endpoint}:{identifier}"
            request_times = self._requests[key]

            if not request_times:
                return now

            # Return the earliest time when a request will fall outside the window
            return request_times[0] + window_seconds

    def get_remaining_requests(self, identifier: str, endpoint: str = "default") -> int:
        """Get number of remaining requests before hitting the limit."""
        max_requests, window_seconds = self._get_config_for_endpoint(endpoint)
        now = time.time()

        if self._redis_client is not None:
            # Redis implementation would go here (simplified for now)
            pass

        with self._lock:
            key = f"{endpoint}:{identifier}"
            request_times = self._requests[key]

            # Remove outdated requests
            while request_times and request_times[0] <= now - window_seconds:
                request_times.popleft()

            return max(0, max_requests - len(request_times))

    def _maybe_cleanup(self, now: float) -> None:
        """Remove stale entries to prevent memory growth.

        Called automatically during normal operations. Only runs if enough
        time has passed since the last cleanup.
        """
        if now - self._last_cleanup < self.config.cleanup_interval:
            return

        log.debug("Cleaning up stale rate limit entries")
        cutoff = now - self.config.window_seconds
        stale_keys = [
            key for key, times in self._requests.items()
            if not times or times[-1] < cutoff
        ]
        for key in stale_keys:
            del self._requests[key]

        self._last_cleanup = now
        if stale_keys:
            log.debug("Removed %d stale rate limit entries", len(stale_keys))

    def clear(self) -> None:
        """Clear all rate limit data. Useful for testing."""
        with self._lock:
            self._requests.clear()
            self._last_cleanup = time.time()


# Global rate limiter instance - configured at app startup
_rate_limiter: RateLimiter | None = None


def init_rate_limiter(config: RateLimitConfig) -> RateLimiter:
    """Initialize the global rate limiter with the given config."""
    global _rate_limiter
    _rate_limiter = RateLimiter(config)
    return _rate_limiter


def get_rate_limiter() -> RateLimiter:
    """Get the global rate limiter instance."""
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = RateLimiter()
    return _rate_limiter


# Backwards compatibility alias
rate_limiter = get_rate_limiter()
