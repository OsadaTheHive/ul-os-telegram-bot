# Multi-stage build — UL OS Telegram Bot
# Stage 1: builder — instaluje Python deps z gcc
# Stage 2: runtime — Python + deps + Pandoc + ffmpeg + whisper.cpp (Sprint 1.6-1.11)

FROM python:3.12-slim AS builder

WORKDIR /build

# Build deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Python deps
COPY requirements.txt .
RUN pip install --user --no-cache-dir -r requirements.txt

# === Stage 2: runtime ===
FROM python:3.12-slim AS runtime

WORKDIR /app

# Runtime deps:
# - curl: healthcheck
# - tini: graceful PID 1
# - pandoc: Sprint 1.10 (/generate DOCX)
# - ffmpeg: Sprint 1.8 (voice OGG → WAV)
# - libgomp1, libsndfile1: whisper.cpp shared deps
# - ca-certificates: HTTPS API calls
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    tini \
    pandoc \
    ffmpeg \
    libgomp1 \
    libsndfile1 \
    ca-certificates \
    wget \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -u 1000 botuser

# whisper.cpp — Sprint 1.8 voice transcription.
# Build z source (Apache 2.0, ~30MB) i pobranie ggml-base.bin (~140MB, polski OK).
# Jeśli chcesz wyższej jakości: zmień ggml-base na ggml-small (~466MB) lub ggml-medium (~1.4GB).
# Override przez env WHISPER_MODEL_PATH=/models/<inny.bin>
ARG WHISPER_MODEL=base
RUN apt-get update && apt-get install -y --no-install-recommends \
    git build-essential cmake \
    && rm -rf /var/lib/apt/lists/* \
    && git clone --depth 1 https://github.com/ggerganov/whisper.cpp.git /tmp/whisper.cpp \
    && cd /tmp/whisper.cpp \
    && cmake -B build -DCMAKE_BUILD_TYPE=Release \
    && cmake --build build --config Release -j$(nproc) --target whisper-cli \
    && cp build/bin/whisper-cli /usr/local/bin/whisper-cli \
    && mkdir -p /models \
    && wget -q -O /models/ggml-${WHISPER_MODEL}.bin \
       "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-${WHISPER_MODEL}.bin" \
    && chown -R botuser:botuser /models \
    && rm -rf /tmp/whisper.cpp \
    && apt-get purge -y git build-essential cmake \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

ENV WHISPER_MODEL_PATH=/models/ggml-${WHISPER_MODEL}.bin
ENV WHISPER_CLI=/usr/local/bin/whisper-cli

# Python deps z builder stage
COPY --from=builder --chown=botuser:botuser /root/.local /home/botuser/.local
ENV PATH=/home/botuser/.local/bin:$PATH

# Kod aplikacji
COPY --chown=botuser:botuser app/ ./app/

# Logs + tmp directories (persistent volumes w Coolify)
RUN mkdir -p /app/logs /tmp/ulos-worker-staging /tmp/ulos_whisper \
    && chown -R botuser:botuser /app/logs /tmp/ulos-worker-staging /tmp/ulos_whisper

# Healthcheck — HTTP endpoint na 8080
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/health || exit 1

USER botuser

# tini jako init — prawidlowe handlowanie sygnalow (SIGTERM gracefully shutdown bot)
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "app.main"]

EXPOSE 8080
