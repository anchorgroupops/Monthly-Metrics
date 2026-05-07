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

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape

from config.settings import BRAND
from src import storage

log = logging.getLogger(__name__)

WEBAPP_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = WEBAPP_DIR / "templates"


def create_app() -> FastAPI:
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

    # Ensure schema exists before the first request lands.
    storage.init_schema()

    from src.webapp.routes import router

    application.include_router(router)
    return application


app = create_app()
