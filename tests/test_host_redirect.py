"""Verify anchor.* → metrics.* host redirect behavior."""

from __future__ import annotations

import pytest

from src.dashboard import create_app


@pytest.fixture
def client(isolated_db, isolated_thresholds):
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def test_anchor_host_redirects_to_metrics(client):
    resp = client.get("/", base_url="http://anchor.joelycannoli.com")
    assert resp.status_code == 301
    assert resp.headers["Location"] == "https://metrics.joelycannoli.com/"


def test_anchor_host_preserves_path_and_query(client):
    resp = client.get(
        "/review/2026-04?foo=bar&baz=qux",
        base_url="http://anchor.joelycannoli.com",
    )
    assert resp.status_code == 301
    assert (
        resp.headers["Location"]
        == "https://metrics.joelycannoli.com/review/2026-04?foo=bar&baz=qux"
    )


def test_metrics_host_does_not_redirect(client):
    # Metrics host should hit the real app — root redirects to /login (302),
    # not the canonical-host 301.
    resp = client.get("/", base_url="http://metrics.joelycannoli.com")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_localhost_does_not_redirect(client):
    resp = client.get("/", base_url="http://127.0.0.1:5050")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_redirect_can_be_disabled_via_env(monkeypatch, isolated_db, isolated_thresholds):
    monkeypatch.setenv("REDIRECT_HOST_FROM", "")
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as c:
        resp = c.get("/", base_url="http://anchor.joelycannoli.com")
        assert resp.status_code == 302  # falls through to /login redirect
