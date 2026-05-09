"""
HTTP /health endpoint dla zewnetrznego monitoringu (UptimeRobot, Coolify health checks).

Long-polling bot nie ma natywnego HTTP server - dorzucamy aiohttp na osobny port (default 8080).

Endpoints:
  GET /health         - zwraca status JSON (ok/degraded/down)
  GET /metrics        - prometheus-style text metrics (opcjonalne, future)

Bezpieczenstwo:
  - Endpoint nie wymaga auth (bo to dla zewnetrznego monitora)
  - NIE zwraca tokenow / sekretow
  - Zwraca tylko ok/down + breaker states + uptime
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from aiohttp import web

from . import breaker
from .config import settings

log = logging.getLogger(__name__)

START_TIME = time.time()


async def health_handler(request: web.Request) -> web.Response:
    """Returns 200 if all OK, 503 if degraded."""
    breakers_status = breaker.all_stats()

    # Determine overall status
    open_circuits = [
        name for name, stats in breakers_status.items() if stats["state"] == "open"
    ]
    degraded = bool(open_circuits)

    payload: dict[str, Any] = {
        "status": "degraded" if degraded else "ok",
        "uptime_seconds": int(time.time() - START_TIME),
        "version": "1.0.0",
        "whitelist_size": len(settings.admin_user_ids),
        "circuit_breakers": breakers_status,
    }
    if degraded:
        payload["open_circuits"] = open_circuits

    status_code = 503 if degraded else 200
    return web.json_response(payload, status=status_code)


async def metrics_handler(request: web.Request) -> web.Response:
    """Prometheus-style metrics (basic)."""
    breakers_status = breaker.all_stats()
    lines = [
        "# HELP ulos_bot_uptime_seconds Bot uptime",
        "# TYPE ulos_bot_uptime_seconds counter",
        f"ulos_bot_uptime_seconds {int(time.time() - START_TIME)}",
        "",
        "# HELP ulos_bot_circuit_state Circuit breaker state (0=closed, 1=half_open, 2=open)",
        "# TYPE ulos_bot_circuit_state gauge",
    ]
    state_map = {"closed": 0, "half_open": 1, "open": 2}
    for name, stats in breakers_status.items():
        state_val = state_map.get(stats["state"], 0)
        lines.append(f'ulos_bot_circuit_state{{breaker="{name}"}} {state_val}')
    return web.Response(text="\n".join(lines) + "\n", content_type="text/plain")


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/health", health_handler)
    app.router.add_get("/metrics", metrics_handler)
    return app


async def start_health_server(port: int = 8080) -> web.AppRunner:
    """Start health endpoint as background task. Call from bot startup."""
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="127.0.0.1", port=port)
    await site.start()
    log.info("Health endpoint listening on http://127.0.0.1:%d/health", port)
    return runner
