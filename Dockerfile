FROM python:3.12-slim

WORKDIR /app

# Build dependencies (slim image - musimy mieć gcc dla niektórych pakietów)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Python deps - cachowane przed kopiowaniem kodu
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Kod aplikacji
COPY app/ ./app/

# Healthcheck dla Coolify (long-polling = process running = healthy)
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD pgrep -f "python -m app.main" || exit 1

# Non-root user
RUN useradd -m -u 1000 botuser && chown -R botuser:botuser /app
USER botuser

CMD ["python", "-m", "app.main"]
