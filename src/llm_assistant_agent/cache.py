"""Response caching for offline mode and cost reduction.

Caches model responses to:
- Reduce API costs for repeated queries
- Enable offline operation with cached responses
- Speed up development/testing

Cache is stored in ``~/.assist/cache/`` and keyed by:
- Model ID
- Messages (full conversation context)
- Tool schemas
- Sampling parameters

Example::

    cache = ResponseCache()
    response = cache.get_or_set(
        model="Qwen/Qwen3-Coder-30B-A3B-Instruct",
        messages=[...],
        tools=[...],
        temperature=0.1,
    )
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

__all__ = ["ResponseCache", "CacheStats"]


@dataclass
class CacheStats:
    """Statistics about cache usage."""

    hits: int = 0
    misses: int = 0
    size_bytes: int = 0
    entry_count: int = 0
    oldest_entry: datetime | None = None
    newest_entry: datetime | None = None

    @property
    def hit_rate(self) -> float:
        """Cache hit rate as a percentage."""
        total = self.hits + self.misses
        return (self.hits / total * 100) if total > 0 else 0.0


@dataclass
class CachedResponse:
    """A cached model response."""

    created: datetime
    model: str
    messages_hash: str
    content: str
    tool_calls: list[dict[str, Any]] | None = None
    finish_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "created": self.created.isoformat(),
            "model": self.model,
            "messages_hash": self.messages_hash,
            "content": self.content,
            "tool_calls": self.tool_calls,
            "finish_reason": self.finish_reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CachedResponse:
        return cls(
            created=datetime.fromisoformat(data["created"]),
            model=data["model"],
            messages_hash=data["messages_hash"],
            content=data["content"],
            tool_calls=data.get("tool_calls"),
            finish_reason=data.get("finish_reason"),
        )


class ResponseCache:
    """Caches model responses for reuse."""

    def __init__(
        self,
        root: Path | None = None,
        max_age_days: int = 7,
        max_size_mb: int = 100,
        enabled: bool = True,
    ) -> None:
        self.root = root or _default_cache_root()
        self.max_age = timedelta(days=max_age_days)
        self.max_size_bytes = max_size_mb * 1024 * 1024
        self.enabled = enabled
        self._stats = CacheStats()
        self.root.mkdir(parents=True, exist_ok=True)

    def _hash_key(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        temperature: float,
        top_p: float,
        max_tokens: int,
    ) -> str:
        """Create a unique hash for a request."""
        key_data = {
            "model": model,
            "messages": messages,
            "tools": tools or [],
            "temperature": temperature,
            "top_p": top_p,
            "max_tokens": max_tokens,
        }
        key_json = json.dumps(key_data, sort_keys=True, default=str)
        return hashlib.sha256(key_json.encode()).hexdigest()

    def _cache_path(self, key: str) -> Path:
        """Get the file path for a cache entry."""
        # Use first 2 chars as subdirectory to avoid too many files in one dir
        return self.root / key[:2] / f"{key}.json"

    def get(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        temperature: float,
        top_p: float,
        max_tokens: int,
    ) -> tuple[str, list[dict[str, Any]] | None, str | None] | None:
        """Get a cached response if available.

        Returns (content, tool_calls, finish_reason) or None if not cached.
        """
        if not self.enabled:
            return None

        key = self._hash_key(model, messages, tools, temperature, top_p, max_tokens)
        path = self._cache_path(key)

        if not path.is_file():
            self._stats.misses += 1
            return None

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            cached = CachedResponse.from_dict(data)

            # Check age
            if datetime.now(UTC) - cached.created > self.max_age:
                path.unlink(missing_ok=True)
                self._stats.misses += 1
                return None

            self._stats.hits += 1
            return cached.content, cached.tool_calls, cached.finish_reason

        except (OSError, json.JSONDecodeError, KeyError):
            path.unlink(missing_ok=True)
            self._stats.misses += 1
            return None

    def set(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        temperature: float,
        top_p: float,
        max_tokens: int,
        content: str,
        tool_calls: list[dict[str, Any]] | None,
        finish_reason: str | None,
    ) -> None:
        """Cache a model response."""
        if not self.enabled:
            return

        key = self._hash_key(model, messages, tools, temperature, top_p, max_tokens)
        path = self._cache_path(key)

        cached = CachedResponse(
            created=datetime.now(UTC),
            model=model,
            messages_hash=key,
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
        )

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(cached.to_dict()), encoding="utf-8")
            self._enforce_size_limit()
        except OSError:
            pass  # Silently ignore cache write failures

    def _enforce_size_limit(self) -> None:
        """Remove oldest entries if cache exceeds size limit."""
        try:
            total_size = sum(
                p.stat().st_size for p in self.root.rglob("*.json") if p.is_file()
            )

            if total_size <= self.max_size_bytes:
                return

            # Remove oldest files until under limit
            files = sorted(
                self.root.rglob("*.json"),
                key=lambda p: p.stat().st_mtime,
            )

            for path in files:
                if total_size <= self.max_size_bytes:
                    break
                size = path.stat().st_size
                path.unlink(missing_ok=True)
                total_size -= size

        except OSError:
            pass

    def clear(self) -> int:
        """Clear all cached responses. Returns count of entries removed."""
        count = 0
        for path in self.root.rglob("*.json"):
            if path.is_file():
                path.unlink()
                count += 1
        return count

    def stats(self) -> CacheStats:
        """Get cache statistics."""
        try:
            files = [p for p in self.root.rglob("*.json") if p.is_file()]
            self._stats.size_bytes = sum(p.stat().st_size for p in files)
            self._stats.entry_count = len(files)

            if files:
                times = [p.stat().st_mtime for p in files]
                self._stats.oldest_entry = datetime.fromtimestamp(min(times), tz=UTC)
                self._stats.newest_entry = datetime.fromtimestamp(max(times), tz=UTC)
        except OSError:
            pass

        return self._stats

    def list_entries(self, limit: int = 20) -> list[dict[str, Any]]:
        """List recent cache entries."""
        entries = []
        try:
            files = sorted(
                self.root.rglob("*.json"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )[:limit]

            for path in files:
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    entries.append(
                        {
                            "model": data.get("model", "unknown"),
                            "created": data.get("created", ""),
                            "size": path.stat().st_size,
                        }
                    )
                except (OSError, json.JSONDecodeError):
                    continue
        except OSError:
            pass

        return entries


def _default_cache_root() -> Path:
    home = os.environ.get("ASSIST_HOME")
    return Path(home).expanduser() / "cache" if home else Path.home() / ".assist" / "cache"
