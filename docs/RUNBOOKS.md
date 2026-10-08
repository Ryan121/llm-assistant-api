# Production Runbooks

This directory contains runbooks for common operational scenarios. Each runbook provides step-by-step instructions for diagnosing and resolving issues.

## Table of Contents

1. [Upstream Service Unavailable](#upstream-service-unavailable)
2. [Circuit Breaker Open](#circuit-breaker-open)
3. [High Error Rate](#high-error-rate)
4. [Slow Response Times](#slow-response-times)
5. [Rate Limiting Issues](#rate-limiting-issues)
6. [TLS Certificate Expiry](#tls-certificate-expiry)
7. [Gateway Won't Start](#gateway-wont-start)
8. [Model Loading Failure](#model-loading-failure)
9. **Memory Pressure](#memory-pressure)
10. [Disk Space Critical](#disk-space-critical)

---

## Upstream Service Unavailable

**Symptom:** Gateway returns 502/503 errors, `/readyz` returns 503

### Diagnosis

```bash
# Check gateway health
curl http://localhost:8081/healthz
curl http://localhost:8081/readyz

# Check detailed status
curl http://localhost:8081/readyz/detailed | jq

# Check gateway logs
docker compose logs gateway | tail -100

# Check vLLM logs
docker compose logs vllm | tail -100

# Check if vLLM container is running
docker compose ps vllm

# Test vLLM directly
curl http://vllm:8999/health
```

### Resolution

**If vLLM is not running:**

```bash
# Restart vLLM
docker compose restart vllm

# Wait for model to load
make wait

# Verify
make health
```

**If vLLM is OOM:**

```bash
# Check GPU memory
nvidia-smi

# Reduce model size or GPU_MEMORY_UTILIZATION
vim .env
# GPU_MEMORY_UTILIZATION=0.85

# Restart
make restart
```

**If network issue:**

```bash
# Check Docker network
docker network inspect llm-assistant-net

# Restart network
docker compose down
docker network rm llm-assistant-net
docker compose up -d
```

---

## Circuit Breaker Open

**Symptom:** Gateway returns 503 with "circuit breaker open" message, even though upstream is healthy

### Diagnosis

```bash
# Check circuit breaker status
curl http://localhost:8081/admin/circuit-breakers | jq

# Check which upstream is affected
curl http://localhost:8081/readyz/detailed | jq '.circuit_breakers'

# Check gateway logs for failures
docker compose logs gateway | grep -i "circuit\|failure" | tail -50
```

### Resolution

**If upstream is now healthy:**

```bash
# Reset circuit breaker for specific upstream
curl -X POST "http://localhost:8081/admin/circuit-breaker/reset?base_url=http://vllm:8999/v1"

# Or reset all
curl -X POST http://localhost:8081/admin/circuit-breaker/reset

# Verify
curl http://localhost:8081/readyz/detailed | jq
```

**If upstream still unhealthy:**

1. Follow "Upstream Service Unavailable" runbook
2. Wait for upstream to recover
3. Circuit will auto-transition to half-open after 30s
4. Monitor for successful test request

**Prevention:**

- Investigate root cause of upstream failures
- Consider adjusting circuit breaker parameters if too sensitive
- Monitor upstream health metrics

---

## High Error Rate

**Symptom:** Error rate > 5%, multiple 4xx/5xx responses

### Diagnosis

```bash
# Check error metrics
curl http://localhost:8081/metrics | jq '.summary'

# Check error breakdown
curl http://localhost:8081/metrics | jq '.summary.error_counts'

# Check recent logs
docker compose logs gateway | grep -E "ERROR|WARN" | tail -100

# Check status code distribution
curl http://localhost:8081/metrics | jq '.summary.status_code_counts'
```

### Resolution

**4xx errors (client errors):**

- 401: Check API keys (`make vscode-config`)
- 404: Verify model name matches configuration
- 429: Check rate limits, see "Rate Limiting Issues"
- 400: Check request payload format

**5xx errors (server errors):**

- 502: Upstream unavailable, see "Upstream Service Unavailable"
- 503: Service overloaded or circuit breaker open
- 504: Upstream timeout, increase `REQUEST_TIMEOUT_SECONDS`

**Immediate actions:**

```bash
# Check current load
curl http://localhost:8081/metrics | jq '.request_stats.active_requests'

# If overloaded, enable rate limiting
# Edit .env and set rate limits
# Restart gateway
docker compose restart gateway
```

---

## Slow Response Times

**Symptom:** p95 latency > 10s, TTFT > 5s

### Diagnosis

```bash
# Check latency metrics
curl http://localhost:8081/metrics | jq '.request_stats'

# Key metrics:
# - p95_response_time_ms
# - p95_ttft_ms (time to first token)
# - p95_upstream_latency_ms

# Check vLLM metrics
curl http://localhost:8081/metrics/upstream

# Look for:
# - kv_cache_usage (should be < 0.9)
# - num_requests_running
# - num_requests_waiting
```

### Resolution

**High TTFT (time to first token):**

```bash
# Context window too large - reduce MAX_MODEL_LEN
vim .env
# MAX_MODEL_LEN=16384

# Enable prefix caching
# VLLM_EXTRA_ARGS=--enable-prefix-caching

make restart
```

**High response time:**

```bash
# Too many concurrent requests - reduce MAX_NUM_SEQS
vim .env
# VLLM_MAX_NUM_SEQS=128

# GPU utilization too high - reduce
# GPU_MEMORY_UTILIZATION=0.85

make restart
```

**KV cache full:**

```bash
# Reduce model concurrency
# MAX_MODEL_LEN=8192

# Or use smaller model
# MODEL_ID=Qwen/Qwen2.5-Coder-7B

make restart
```

---

## Rate Limiting Issues

**Symptom:** 429 Too Many Requests, legitimate users blocked

### Diagnosis

```bash
# Check rate limit hits
curl http://localhost:8081/metrics | jq '.summary.rate_limit_hits'

# Check rate limiter status
curl http://localhost:8081/readyz/detailed | jq '.rate_limiter'

# Check configuration
docker compose exec gateway env | grep RATE_LIMIT
```

### Resolution

**Redis connection lost:**

```bash
# Check Redis
docker compose ps redis

# Restart Redis
docker compose restart redis

# Verify connection
curl http://localhost:8081/readyz/detailed | jq '.rate_limiter'
```

**Limits too aggressive:**

```bash
# Adjust rate limits in .env
# RATE_LIMIT_RULES="/v1/chat/completions:200:60,/v1/autocomplete:500:60"
# RATE_LIMIT_MAX_REQUESTS=200
# RATE_LIMIT_WINDOW_SECONDS=60

docker compose restart gateway
```

**Single user consuming all quota:**

```bash
# Implement per-user rate limits
# Add API key-based rate limiting
# See docs/OPERATIONS.md for configuration
```

---

## TLS Certificate Expiry

**Symptom:** HTTPS connections fail, browser shows certificate warning

### Diagnosis

```bash
# Check certificate expiry
openssl s_client -connect localhost:443 -servername localhost 2>/dev/null | openssl x509 -noout -dates

# Check days until expiry
openssl s_client -connect localhost:443 -servername localhost 2>/dev/null | \
  openssl x509 -noout -enddate | \
  awk -F= '{print $2}'
```

### Resolution

**Let's Encrypt certificate:**

```bash
# Manual renewal
sudo certbot renew

# Verify auto-renewal cron
crontab -l | grep certbot

# Restart nginx
docker compose --profile nginx restart nginx
```

**Self-signed certificate:**

```bash
# Generate new certificate
./scripts/setup-tls.sh development

# Restart nginx
docker compose --profile nginx restart nginx
```

**Custom certificate:**

```bash
# Replace certificate files
cp new-cert.pem deploy/nginx-tls/ssl/cert.pem
cp new-key.pem deploy/nginx-tls/ssl/key.pem

# Restart nginx
docker compose --profile nginx restart nginx
```

**Prevention:**

- Set up monitoring for certificate expiry (alert at 30 days)
- Use Let's Encrypt with auto-renewal
- Document renewal process

---

## Gateway Won't Start

**Symptom:** Gateway container exits immediately or won't start

### Diagnosis

```bash
# Check container status
docker compose ps gateway

# Check logs
docker compose logs gateway

# Check configuration
docker compose config

# Test configuration
docker compose run --rm gateway python -c "from llm_assistant_api.main import create_app; create_app()"
```

### Resolution

**Configuration error:**

```bash
# Validate .env
cat .env | grep -E "^[A-Z_]+="

# Check for duplicate keys
cat .env | sort | uniq -d

# Fix and restart
vim .env
docker compose up -d gateway
```

**Port conflict:**

```bash
# Check what's using the port
sudo lsof -i :8081

# Change port in .env
# API_PORT=8082

docker compose up -d
```

**Dependency missing:**

```bash
# Rebuild gateway
make build

# Or pull latest
docker compose pull gateway

docker compose up -d gateway
```

---

## Model Loading Failure

**Symptom:** vLLM exits during startup, model download fails

### Diagnosis

```bash
# Check vLLM logs
docker compose logs vllm | tail -200

# Check disk space
docker system df
docker run --rm -v llm-assistant-model-cache:/models alpine df -h /models

# Check Hugging Face token
docker compose exec vllm env | grep HF_TOKEN
```

### Resolution

**Disk full:**

```bash
# Clean old models
docker run --rm -v llm-assistant-model-cache:/models alpine \
  rm -rf /models/hub/models--org--old-model

# Clean Docker
docker system prune -af

# Restart
make restart
```

**Authentication error:**

```bash
# Update HF token
vim .env
# HF_TOKEN=your_new_token

# Pull model again
make pull-model
```

**Model file corrupted:**

```bash
# Remove corrupted model
docker run --rm -v llm-assistant-model-cache:/models alpine \
  rm -rf /models/hub/models--org--model-name

# Re-download
make pull-model
```

---

## Memory Pressure

**Symptom:** OOM kills, gateway or vLLM restarts unexpectedly

### Diagnosis

```bash
# Check memory usage
docker stats --no-stream

# Check system memory
free -h

# Check OOM logs
dmesg | grep -i "killed process"

# Check gateway memory
docker compose exec gateway cat /sys/fs/cgroup/memory/memory.usage_in_bytes
```

### Resolution

**Gateway OOM:**

```bash
# Increase memory limit in docker-compose.yml
# deploy:
#   resources:
#     limits:
#       memory: 2G

# Or reduce gateway workers
# WEB_CONCURRENCY=2

docker compose up -d gateway
```

**vLLM OOM:**

```bash
# Reduce GPU memory utilization
vim .env
# GPU_MEMORY_UTILIZATION=0.80

# Reduce model size
# MAX_MODEL_LEN=8192

make restart
```

**System memory pressure:**

```bash
# Stop unnecessary services
docker compose stop [service-name]

# Add swap (temporary)
sudo fallocate -l 4G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
```

---

## Disk Space Critical

**Symptom:** Disk usage > 90%, writes failing

### Diagnosis

```bash
# Check disk usage
df -h

# Check Docker disk usage
docker system df

# Check model cache size
docker run --rm -v llm-assistant-model-cache:/models alpine \
  du -sh /models/hub/*

# Check logs size
docker compose exec gateway du -sh /var/log/*
```

### Resolution

**Model cache cleanup:**

```bash
# List models by size
docker run --rm -v llm-assistant-model-cache:/models alpine \
  du -sh /models/hub/* | sort -hr

# Remove unused models
docker run --rm -v llm-assistant-model-cache:/models alpine \
  rm -rf /models/hub/models--org--unwanted-model

# Prune Docker
docker system prune -af --volumes
```

**Log rotation:**

```bash
# Configure log rotation in docker-compose.yml
# logging:
#   driver: "json-file"
#   options:
#     max-size: "10m"
#     max-file: "3"

docker compose up -d
```

**Temporary cleanup:**

```bash
# Remove old containers
docker container prune -f

# Remove unused images
docker image prune -af

# Remove build cache
docker builder prune -af
```

---

## Monitoring and Alerting

### Key Metrics to Monitor

| Metric | Warning | Critical |
|--------|---------|----------|
| Error Rate | > 5% | > 10% |
| p95 Latency | > 5s | > 10s |
| p95 TTFT | > 3s | > 5s |
| Active Requests | > 50 | > 100 |
| Circuit Breaker State | half-open | open |
| Disk Usage | > 80% | > 90% |
| Memory Usage | > 80% | > 90% |
| Certificate Expiry | < 30 days | < 7 days |

### Alerting Commands

```bash
# Check error rate
curl -s http://localhost:8081/metrics | jq '.summary.error_rate'

# Check circuit breakers
curl -s http://localhost:8081/admin/circuit-breakers | jq 'to_entries[] | select(.value.state != "closed")'

# Check disk
df -h /var/lib/docker | awk 'NR==2 {print $5}'
```

### Health Check Script

```bash
#!/bin/bash
# save as scripts/health-check.sh

set -e

GATEWAY_URL="${GATEWAY_URL:-http://localhost:8081}"

echo "Checking gateway health..."

# Liveness
if ! curl -s "$GATEWAY_URL/healthz" | grep -q "ok"; then
    echo "FAIL: Gateway not alive"
    exit 1
fi

# Readiness
STATUS=$(curl -s "$GATEWAY_URL/readyz/detailed" | jq -r '.status')
if [[ "$STATUS" != "ready" ]]; then
    echo "WARN: Gateway not ready (status: $STATUS)"
fi

# Circuit breakers
OPEN_CB=$(curl -s "$GATEWAY_URL/admin/circuit-breakers" | jq '[to_entries[] | select(.value.state == "open")] | length')
if [[ "$OPEN_CB" -gt 0 ]]; then
    echo "WARN: $OPEN_CB circuit breaker(s) open"
fi

# Error rate
ERROR_RATE=$(curl -s "$GATEWAY_URL/metrics" | jq '.summary.error_rate')
if (( $(echo "$ERROR_RATE > 0.05" | bc -l) )); then
    echo "WARN: High error rate: $ERROR_RATE"
fi

echo "OK: All checks passed"
exit 0
```

---

## Escalation Path

1. **L1 (On-call):** Follow runbooks, restart services if needed
2. **L2 (Platform):** Investigate recurring issues, adjust configuration
3. **L3 (Engineering):** Code bugs, architecture issues, capacity planning

### Contact Information

- **On-call:** [TODO: Add on-call rotation]
- **Platform Team:** [TODO: Add Slack channel]
- **Engineering:** [TODO: Add escalation process]

### Post-Incident

After resolving any P1/P2 incident:

1. Document timeline in incident report
2. Identify root cause
3. Create action items to prevent recurrence
4. Update runbooks if needed
5. Schedule post-mortem meeting
