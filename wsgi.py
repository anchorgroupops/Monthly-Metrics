"""WSGI entrypoint for gunicorn (production)."""

import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s: %(message)s",
)

from src.dashboard import create_app  # noqa: E402

application = create_app()
