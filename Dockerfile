# syntax=docker/dockerfile:1.7
#
# Gateway image only. The model is NEVER baked in -- vLLM pulls it from
# Hugging Face at runtime into a named volume, so this image stays ~150 MB and
# the same tag can front any model.

FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy metadata first so dependency resolution is cached independently of src.
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install .


FROM python:3.12-slim AS runtime

ARG APP_VERSION=0.0.0
ARG GIT_SHA=unknown
ARG BUILD_DATE=unknown

LABEL org.opencontainers.image.title="llm-assistant-api" \
      org.opencontainers.image.description="OpenAI-compatible gateway for a self-hosted coding assistant" \
      org.opencontainers.image.source="https://github.com/Ryan121/llm-assistant-api" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.created="${BUILD_DATE}"

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    APP_VERSION="${APP_VERSION}" \
    GIT_SHA="${GIT_SHA}"

RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --home /app --shell /usr/sbin/nologin app

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
USER 10001:10001

EXPOSE 8081

# Liveness only -- readiness (is the model loaded?) is /readyz, which the
# deployment scripts poll separately because a cold model pull takes minutes.
HEALTHCHECK --interval=15s --timeout=5s --start-period=15s --retries=5 \
    CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8081/healthz', timeout=3)"]

# Worker count comes from WEB_CONCURRENCY, which uvicorn reads itself.
# --no-access-log because the app's own middleware already logs method, path,
# status, duration and request id -- uvicorn's line is strictly less useful.
ENTRYPOINT ["uvicorn", "llm_assistant_api.main:app"]
CMD ["--host", "0.0.0.0", "--port", "8081", "--proxy-headers", "--forwarded-allow-ips", "*", "--no-access-log"]
