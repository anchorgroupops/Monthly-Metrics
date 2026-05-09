"""
Tests that the dashboard works correctly when mounted under a sub-path
(WEB_BASE_PATH=/metrics), as it will be on anchor.joelycannoli.com/metrics.
"""

import pytest


@pytest.fixture
def client(monkeypatch, tmp_db, thresholds_full):
    monkeypatch.setattr("src.storage.DATABASE_PATH", tmp_db)
    monkeypatch.setattr("src.metrics.load_thresholds", lambda: thresholds_full)
    # Prefix-mount everywhere it's read.
    monkeypatch.setattr("src.webapp.app.WEB_BASE_PATH", "/metrics")
    monkeypatch.setattr("src.webapp.routes.WEB_BASE_PATH", "/metrics")

    from fastapi.testclient import TestClient

    from src.webapp.app import create_app

    return TestClient(create_app(), follow_redirects=False)


@pytest.fixture
def alice(tmp_db):
    from src import storage

    storage.upsert_agents(
        [{"name": "Alice", "email": "alice@x", "fub_agent_id": "1"}],
        db_path=tmp_db,
    )
    return storage.get_agent_by_email("alice@x", db_path=tmp_db)


# ── Routes are reachable at the prefix, not at root ──────────────────────────

class TestRoutesMountedUnderPrefix:
    def test_root_at_prefix_redirects_to_login(self, client):
        r = client.get("/metrics/")
        assert r.status_code == 302
        # Redirect target also includes the prefix.
        assert r.headers["location"] == "/metrics/login"

    def test_login_form_renders_at_prefix(self, client):
        r = client.get("/metrics/login")
        assert r.status_code == 200
        # Form action carries the prefix so POST goes to the right place.
        assert 'action="/metrics/login"' in r.text

    def test_dashboard_anonymous_redirects_to_prefixed_login(self, client):
        r = client.get("/metrics/dashboard")
        assert r.status_code == 302
        assert r.headers["location"] == "/metrics/login"

    def test_root_path_returns_404_when_prefix_set(self, client):
        # Bare /login no longer exists once the router is mounted at /metrics.
        r = client.get("/login")
        assert r.status_code == 404

    def test_healthz_at_prefix(self, client):
        r = client.get("/metrics/healthz")
        assert r.status_code == 200


# ── Magic-link flow round trip ───────────────────────────────────────────────

class TestPrefixedLoginFlow:
    def test_full_flow_sets_cookie_with_prefix_path(self, client, alice, mocker):
        captured = {}

        def fake_send(to, subj, html):
            captured["url"] = html.split("/verify?token=")[1].split('"')[0]

        mocker.patch("src.auth.send_html", side_effect=fake_send)

        # POST to the prefixed URL.
        r = client.post("/metrics/login", data={"email": "alice@x"})
        assert r.status_code == 200

        token = captured["url"]
        # Verify also lives under the prefix.
        r = client.get(f"/metrics/verify?token={token}")
        assert r.status_code == 302
        assert r.headers["location"] == "/metrics/dashboard"

        cookie_header = r.headers.get("set-cookie", "")
        # Cookie scoped to the prefix so it isn't sent to sibling apps.
        assert "Path=/metrics" in cookie_header
        assert "anchor_session=" in cookie_header

    def test_logout_clears_cookie_with_correct_path(self, client, alice):
        from src import auth
        token = auth.start_session(alice["id"])
        client.cookies.set("anchor_session", token, path="/metrics")
        r = client.post("/metrics/logout")
        assert r.status_code == 302
        assert r.headers["location"] == "/metrics/login"
        # Set-Cookie deleting the session must use the same Path or browsers
        # won't treat it as the same cookie.
        cookie_header = r.headers.get("set-cookie", "")
        assert "Path=/metrics" in cookie_header
