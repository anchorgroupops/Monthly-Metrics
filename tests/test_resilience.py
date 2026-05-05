"""Defensive-boot + error-page tests so the dashboard can't go silently dark."""

from __future__ import annotations

import importlib

import pytest

from src.dashboard import create_app


@pytest.fixture
def client(isolated_db, isolated_thresholds):
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def test_app_boots_when_reaper_raises(monkeypatch, isolated_db, isolated_thresholds):
    """A SQLite hiccup at startup must not prevent the app from booting."""
    from src import dashboard

    def boom():
        raise RuntimeError("simulated SQLite failure")

    monkeypatch.setattr(dashboard, "_reap_stale_runs", boom)
    # Should not raise — boot is wrapped in try/except.
    app = create_app()
    assert app is not None


def test_404_renders_friendly_page(client):
    resp = client.get("/this-route-does-not-exist",
                      base_url="http://metrics.joelycannoli.com")
    assert resp.status_code == 404
    body = resp.get_data(as_text=True)
    assert "404" in body
    assert "Not found" in body
    # No raw Werkzeug traceback leaked.
    assert "Traceback" not in body


def test_smtp_port_invalid_falls_back_to_587(monkeypatch):
    monkeypatch.setenv("SMTP_PORT", "not-a-number")
    from config import settings
    importlib.reload(settings)
    assert settings.SMTP_PORT == 587


def test_smtp_port_valid_is_used(monkeypatch):
    monkeypatch.setenv("SMTP_PORT", "2525")
    from config import settings
    importlib.reload(settings)
    assert settings.SMTP_PORT == 2525
