# Multi-stage build dla mniejszego image i bezpieczenstwa.
# Stage 1: builder - instaluje deps (z gcc dla niektorych pakietow)
# Stage 2: runtime - tylko Python + dependencies + kod (bez gcc)

FROM python:3.12-slim AS builder

WORKDIR /build

# Build deps (gcc, etc.) - tylko w builder stage
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Python deps - instalacja w wirtualnym dyrektorium
COPY requirements.txt .
RUN pip install --user --no-cache-dir -r requirements.txt

# Stage 2: runtime
FROM python:3.12-slim AS runtime

WORKDIR /app

# Runtime deps (curl dla healthcheck, nic wiecej)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    tini \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -u 1000 botuser

# Skopiuj zainstalowane Python deps z builder stage
COPY --from=builder --chown=botuser:botuser /root/.local /home/botuser/.local
ENV PATH=/home/botuser/.local/bin:$PATH

# Kod aplikacji
COPY --chown=botuser:botuser app/ ./app/

# Healthcheck - HTTP endpoint na 8080 (gdy USE_HEALTH_HTTP=true)
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/health || exit 1

# Non-root user
USER botuser

# tini jako init - prawidlowe handlowanie sygnalow (SIGTERM gracefully)
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "app.main"]

# Expose health endpoint port (informacyjnie - nie publishuje)
EXPOSE 8080
