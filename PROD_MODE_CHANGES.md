# Production Mode Implementation

This document describes the P0 production readiness features added to the LLM Assistant API.

## Overview

Three critical production features have been implemented:

1. **Graceful Shutdown** - Proper signal handling and request draining
2. **Configurable Rate Limiting** - Per-endpoint limits with Redis support
3. **Structured Logging** - JSON format for log aggregation

Enable all features with `PROD_MODE=true` in your environment.

---

## 1. Graceful Shutdown

### What it does
- Handles SIGTERM and SIGINT signals properly
- Stops accepting new requests when shutdown begins
- Drains in-flight requests (up to 30 second timeout)
- Logs shutdown progress and completion

### Configuration
```bash
# No configuration needed - enabled by default
# Shutdown timeout is 30 seconds (hardcoded)
```

### How it works
1. On SIGTERM/SIGINT, sets `_shutting_down = True`
2. New requests receive 503 with `Retry-After: 5` header
3. Waits for `active_requests` to reach 0 (max 30s)
4. Closes upstream connections cleanly
5. Logs completion

### Testing
```bash
# Send SIGTERM to test graceful shutdown
kill -TERM <pid>

# Watch logs for:
# "Received SIGTERM, initiating graceful shutdown..."
# "Shutting down, draining in-flight requests..."
# "Shutdown complete"
```

---

## 2. Configurable Rate Limiting

### What it does
- Per-endpoint rate limits (e.g., stricter limits on expensive agent endpoints)
- Sliding window algorithm with automatic cleanup
- Optional Redis backend for multi-instance deployments
- Proper rate limit headers in responses

### Configuration
```bash
# Global defaults
RATE_LIMIT_MAX_REQUESTS=100        # requests per window
RATE_LIMIT_WINDOW_SECONDS=60       # window size
RATE_LIMIT_CLEANUP_INTERVAL=300    # stale entry cleanup

# Per-endpoint rules (comma-separated)
# Format: endpoint:max_requests:window_seconds
RATE_LIMIT_RULES="/v1/chat/completions:50:60,/v1/autocomplete:500:60"

# Redis backend (optional, for distributed deployments)
# RATE_LIMIT_REDIS_URL=redis://localhost:6379
```

### New Settings in `config.py`
```python
rate_limit_rules: str = ""                    # Per-endpoint rules
rate_limit_max_requests: int = 100           # Global default
rate_limit_window_seconds: int = 60          # Global default  
rate_limit_cleanup_interval: int = 300       # Cleanup interval
```

### Rate Limit Headers
When rate limited, responses include:
```
HTTP/1.1 429 Too Many Requests
Retry-After: 45
X-RateLimit-Limit: 100
X-RateLimit-Remaining: 0
X-RateLimit-Reset: 1699900000
```

### Testing
```bash
# Test rate limiting
for i in {1..110}; do
  curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8000/v1/chat/completions
done

# Should see 429 after 100 requests (or configured limit)
```

---

## 3. Structured Logging

### What it does
- JSON format option for log aggregation (Datadog, Splunk, ELK, etc.)
- Includes timestamp, level, logger, message, and optional fields
- Proper exception and stack trace formatting
- Reduces noise from httpx/uvicorn access logs

### Configuration
```bash
LOG_LEVEL=INFO              # DEBUG, INFO, WARNING, ERROR, CRITICAL
LOG_FORMAT=text             # "text" or "json"
PROD_MODE=true              # Auto-sets LOG_FORMAT=json if not specified
```

### Text Format (default)
```
2024-01-15 10:30:45,123 INFO     llm_assistant_api.main LLM Assistant API v0.4.1 ready: model=Qwen/Qwen3-Coder-30B-A3B-Instruct upstream=http://vllm:8999/v1
```

### JSON Format
```json
{
  "timestamp": "2024-01-15T10:30:45.123Z",
  "level": "INFO",
  "logger": "llm_assistant_api.main",
  "message": "LLM Assistant API v0.4.1 ready: model=Qwen/Qwen3-Coder-30B-A3B-Instruct upstream=http://vllm:8999/v1"
}
```

### Testing
```bash
# Test JSON logging
LOG_FORMAT=json uvicorn src.llm_assistant_api.main:app --host 0.0.0.0 --port 8000

# Logs will be JSON formatted, parseable by log aggregators
```

---

## Production Mode

### What it does
`PROD_MODE=true` enables production-hardened defaults:
- Sets `LOG_FORMAT=json` (if not explicitly set to "text")
- Can be extended for other production defaults in the future

### Configuration
```bash
PROD_MODE=true
```

### Implementation
```python
# In config.py
@property
def is_prod(self) -> bool:
    """Whether running in production mode."""
    return self.prod_mode

# In main.py
if resolved.is_prod:
    log.info("Production mode enabled - applying hardened defaults")
    if resolved.log_format == "text":
        resolved.log_format = "json"  # Default to JSON in prod
```

---

## Additional Improvements

### Enhanced Health Endpoint
New `/readyz/detailed` endpoint includes:
- Upstream health status
- Rate limiter health (Redis connection status)
- Shutdown state
- Circuit breaker status (future)

```bash
curl http://localhost:8000/readyz/detailed
```

Response:
```json
{
  "status": "ready",
  "upstreams": {
    "Qwen/Qwen3-Coder-30B-A3B-Instruct": true
  },
  "rate_limiter": "ok",
  "shutting_down": false
}
```

### Rate Limiter Architecture
- In-memory by default (single instance)
- Redis backend available for horizontal scaling
- Per-endpoint configuration
- Thread-safe with proper locking
- Automatic stale entry cleanup

---

## Migration Guide

### Existing Deployments
No breaking changes. All new features are opt-in via environment variables.

### Recommended Production Configuration
```bash
# .env for production
PROD_MODE=true
LOG_LEVEL=INFO
LOG_FORMAT=json  # Auto-set by PROD_MODE=true if not specified
API_KEYS=your-secure-key-here

# Rate limiting (adjust based on your capacity)
RATE_LIMIT_MAX_REQUESTS=100
RATE_LIMIT_WINDOW_SECONDS=60
RATE_LIMIT_CLEANUP_INTERVAL=300
RATE_LIMIT_RULES="/v1/chat/completions:50:60,/v1/autocomplete:500:60"

# For multi-instance deployments
RATE_LIMIT_REDIS_URL=redis://your-redis-host:6379

# Request size limit (default 10MB, hardcoded in main.py)
# To change: modify _MAX_REQUEST_SIZE in src/llm_assistant_api/main.py

# Circuit breaker (defaults in proxy.py)
# _CIRCUIT_FAILURE_WINDOW=60.0
# _CIRCUIT_FAILURE_THRESHOLD=5
# _CIRCUIT_OPEN_TIMEOUT=30.0

# Graceful shutdown (default 30s, hardcoded)
# _SHUTDOWN_TIMEOUT=30.0
```

### Docker Compose Example
```yaml
services:
  api:
    image: llm-assistant-api:latest
    environment:
      - PROD_MODE=true
      - API_KEYS=${API_KEYS}
      - RATE_LIMIT_MAX_REQUESTS=100
      - RATE_LIMIT_WINDOW_SECONDS=60
    ports:
      - "8000:8000"
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/readyz"]
      interval: 10s
      timeout: 5s
      retries: 3
      start_period: 30s
    stop_grace_period: 30s  # Match shutdown timeout
```

### Kubernetes Example
```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: llm-assistant-config
data:
  PROD_MODE: "true"
  LOG_FORMAT: "json"
  RATE_LIMIT_MAX_REQUESTS: "100"
  RATE_LIMIT_WINDOW_SECONDS: "60"
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: llm-assistant-api
spec:
  template:
    spec:
      terminationGracePeriodSeconds: 35  # > shutdown timeout
      containers:
      - name: api
        envFrom:
        - configMapRef:
            name: llm-assistant-config
        lifecycle:
          preStop:
            exec:
              command: ["sleep", "5"]  # Allow drain to start
```

---

## Testing Checklist

- [ ] Graceful shutdown: Send SIGTERM, verify 503 responses and clean exit
- [ ] Rate limiting: Exceed limit, verify 429 with proper headers
- [ ] JSON logging: Set `LOG_FORMAT=json`, verify parseable output
- [ ] Production mode: Set `PROD_MODE=true`, verify JSON logging enabled
- [ ] Per-endpoint limits: Configure different limits, verify they apply correctly
- [ ] Health endpoint: Verify `/readyz/detailed` returns all fields
- [ ] Redis backend (if using): Configure Redis URL, verify distributed limiting works

---

## P1 Features Now Implemented

All P1 features have been implemented:

### 1. Circuit Breaker in Health Checks
- `/readyz/detailed` now includes circuit breaker state
- Returns 503 if circuit is open for primary upstream
- New endpoint: `POST /admin/circuit-breaker/reset` to manually reset
- Circuit state exposed in metrics

### 2. Request Size Limits
- Maximum request size: 10MB (configurable via `_MAX_REQUEST_SIZE` in main.py)
- Returns 413 Payload Too Large for oversized requests
- Protects against DoS via large payloads

### 3. Complete Redis Rate Limiting
- Full async Redis implementation with sorted sets
- Fail-open behavior: allows requests if Redis is unavailable
- Auto-cleanup via Redis TTL
- Configuration: `RATE_LIMIT_REDIS_URL=redis://host:6379`

### 4. Enhanced Metrics
- Error rate tracking (overall and by endpoint)
- Upstream latency percentiles (separate from gateway latency)
- Rate limit hit counts by endpoint
- New Prometheus metrics:
  - `gateway_error_rate`
  - `gateway_upstream_latency_ms`
  - `gateway_rate_limit_hits_total`

### 5. Configuration Validation
- Fail-fast in production mode for invalid configs
- API key entropy validation (minimum 16 chars, random patterns)
- Timeout consistency checks
- Rate limit config validation
- Context guard validation

---

## Future Enhancements (P2/P3)

Remaining production features:

1. **Audit Logging** - Track admin operations and security events
2. **API Key Rotation** - Support for hot-reloading API keys
3. **Session Storage Abstraction** - Shared storage for HA deployments
4. **Agent Resource Quotas** - Prevent runaway sessions
5. **Sandbox Escape Detection** - Monitor for escape attempts
