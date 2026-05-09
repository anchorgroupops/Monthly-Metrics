"""
FastAPI application factory.

Run locally:
    uvicorn src.webapp.app:app --port 8081 --reload

In production (systemd unit):
    uvicorn src.webapp.app:app --host 127.0.0.1 --port 8081

The Pi binds 127.0.0.1 only — public access is provided by Cloudflare Tunnel
which proxies the public hostname to localhost.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from config.settings import BRAND, SECRET_KEY, WEB_BASE_PATH
from src import storage

log = logging.getLogger(__name__)

WEBAPP_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = WEBAPP_DIR / "templates"

DEFAULT_SECRET_KEY = "dev-only-not-for-prod-change-me"


def create_app() -> FastAPI:
    if SECRET_KEY == DEFAULT_SECRET_KEY:
        log.warning(
            "SECRET_KEY is the development default. Set a strong value "
            "(e.g. `openssl rand -hex 32`) in /etc/monthly-metrics.env "
            "before exposing the dashboard publicly."
        )

    application = FastAPI(
        title="Anchor Group Monthly Metrics",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    application.state.jinja = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "j2"]),
    )
    application.state.jinja.globals["brand"] = BRAND
    # Templates use this so form actions and links include the deployment
    # prefix (e.g. "/metrics") when the app is mounted under a sub-path.
    application.state.jinja.globals["base_path"] = WEB_BASE_PATH

    # Ensure schema exists before the first request lands.
    storage.init_schema()

    @application.exception_handler(Exception)
    async def _server_error(request: Request, exc: Exception) -> HTMLResponse:
        # Log full traceback for the operator; show a clean, non-leaky page.
        log.exception("Unhandled error on %s %s", request.method, request.url.path)
        body = (
            "<!DOCTYPE html><html><body style=\"font-family:sans-serif;"
            "background:#F5EDE0;padding:48px;text-align:center;\">"
            "<h1 style=\"color:#167272;\">Something went wrong</h1>"
            "<p>Please try again in a moment. If this keeps happening, "
            "let the team know.</p></body></html>"
        )
        return HTMLResponse(body, status_code=500)

    from src.webapp.routes import router

    # Mount under WEB_BASE_PATH so a reverse proxy can forward unmodified URLs
    # (e.g. /metrics/login → /metrics/login on the upstream). This works the
    # same for Cloudflare Tunnel `path:` ingress, nginx `proxy_pass` without a
    # trailing slash, or Caddy `reverse_proxy` without `handle_path`.
    application.include_router(router, prefix=WEB_BASE_PATH)
    return application


app = create_app()
