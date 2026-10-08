"""Rate limiting utilities for the LLM Assistant API."""

import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class RateLimitConfig:
    """Configuration for rate limiting."""

    max_requests: int = 100  # Maximum requests per time window
    window_seconds: int = 60  # Time window in seconds
    cleanup_interval: int = 300  # How often to clean stale entries (seconds)

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
    """

    def __init__(self, config: RateLimitConfig | None = None) -> None:
        if config is None:
            config = RateLimitConfig()
        self.config = config
        self._requests: dict[str, deque[float]] = defaultdict(deque)
        self._last_cleanup = time.time()
        self._lock = __import__("threading").Lock()

    def is_allowed(self, identifier: str) -> bool:
        """
        Check if a request from the given identifier is allowed.

        Args:
            identifier: Unique identifier for the requester (e.g., IP address, API key)

        Returns:
            True if request is allowed, False if rate limited
        """
        now = time.time()
        with self._lock:
            self._maybe_cleanup(now)
            request_times = self._requests[identifier]

            # Remove requests outside the current window
            while request_times and request_times[0] <= now - self.config.window_seconds:
                request_times.popleft()

            # Check if we're under the limit
            if len(request_times) < self.config.max_requests:
                request_times.append(now)
                return True

        return False

    def get_reset_time(self, identifier: str) -> float:
        """Get the time when the rate limit will reset for this identifier."""
        now = time.time()
        with self._lock:
            request_times = self._requests[identifier]

            if not request_times:
                return now

            # Return the earliest time when a request will fall outside the window
            return request_times[0] + self.config.window_seconds

    def get_remaining_requests(self, identifier: str) -> int:
        """Get number of remaining requests before hitting the limit."""
        now = time.time()
        with self._lock:
            request_times = self._requests[identifier]

            # Remove outdated requests
            while request_times and request_times[0] <= now - self.config.window_seconds:
                request_times.popleft()

            return max(0, self.config.max_requests - len(request_times))

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


# Global rate limiter instance
rate_limiter = RateLimiter()
