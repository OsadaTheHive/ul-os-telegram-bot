"""
Observability stack: Sentry + structured JSON logging + OpenTelemetry placeholder.

Sentry init dynamicznie (z env). Jezeli SENTRY_DSN brak -> Sentry ignored,
no overhead. Hubert moze podlaczyc DSN w przyszlosci bez zmiany kodu.

JSON logging: kazdy log jako JSON line - latwy do parsowania w Loki / Splunk.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time

# Sentry import jest opcjonalny - bez DSN po prostu skip
try:
    import sentry_sdk
    from sentry_sdk.integrations.logging import LoggingIntegration

    SENTRY_AVAILABLE = True
except ImportError:
    SENTRY_AVAILABLE = False


class JSONFormatter(logging.Formatter):
    """Formatter ktory emituje kazdy log jako JSON line.

    Pola:
      ts (epoch float), iso, level, logger, message, plus extra fields jezeli sa.
    """

    def format(self, record: logging.LogRecord) -> str:
        log_data: dict = {
            "ts": record.created,
            "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Standard log record fields nie maja byc duplikowane
        skip_fields = {
            "name", "msg", "args", "created", "msecs", "relativeCreated",
            "levelname", "levelno", "pathname", "filename", "module", "exc_info",
            "exc_text", "stack_info", "lineno", "funcName", "process", "processName",
            "thread", "threadName", "getMessage", "message", "asctime",
        }
        # Extra fields (przekazane przez logger.info("msg", extra={...}))
        for key, value in record.__dict__.items():
            if key in skip_fields:
                continue
            try:
                json.dumps(value)  # check if serializable
                log_data[key] = value
            except (TypeError, ValueError):
                log_data[key] = str(value)

        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_data, ensure_ascii=False)


def setup_logging(level: int = logging.INFO, use_json: bool | None = None):
    """Setup root logger.

    Args:
        level: log level
        use_json: True/False/None. Jezeli None - czyta env LOG_FORMAT (json/text, default text dla dev).
    """
    if use_json is None:
        use_json = os.getenv("LOG_FORMAT", "text").lower() == "json"

    handler = logging.StreamHandler(sys.stdout)
    if use_json:
        handler.setFormatter(JSONFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        )

    root = logging.getLogger()
    root.setLevel(level)
    # Wyczysc poprzednie handlery (np. python-telegram-bot dorzuca swoj)
    root.handlers = [handler]


def setup_sentry(dsn: str | None = None, environment: str = "production"):
    """Init Sentry SDK jezeli DSN podany. NoOp jezeli brak.

    Returns: True jezeli Sentry zostal zainicjalizowany, False inaczej.
    """
    if not SENTRY_AVAILABLE:
        logging.getLogger(__name__).info(
            "Sentry SDK not installed - skipping (pip install sentry-sdk to enable)"
        )
        return False

    dsn = dsn or os.getenv("SENTRY_DSN", "").strip()
    if not dsn:
        logging.getLogger(__name__).debug("SENTRY_DSN nieustawiony - Sentry skipped")
        return False

    sentry_sdk.init(
        dsn=dsn,
        environment=environment,
        integrations=[
            LoggingIntegration(
                level=logging.INFO,        # capture INFO and above as breadcrumbs
                event_level=logging.WARNING,  # send WARNING+ as events
            ),
        ],
        traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.1")),
        send_default_pii=False,  # RODO - nie wysylamy domyslnie PII
        before_send=_scrub_sensitive,
    )
    logging.getLogger(__name__).info(
        "Sentry initialized: env=%s, traces_sample_rate=%s",
        environment,
        os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.1"),
    )
    return True


def _scrub_sensitive(event, hint):
    """Strip sensitive fields przed wyslaniem do Sentry."""
    if not isinstance(event, dict):
        return event

    # Strip Authorization headers
    request = event.get("request", {})
    headers = request.get("headers", {})
    for key in list(headers.keys()):
        if key.lower() in ("authorization", "cookie", "x-api-key"):
            headers[key] = "[Filtered]"

    # Strip token-like values w extra/data
    sensitive_keywords = ("token", "secret", "password", "api_key", "bearer")
    extra = event.get("extra", {})
    for key in list(extra.keys()):
        if any(s in key.lower() for s in sensitive_keywords):
            extra[key] = "[Filtered]"

    return event
