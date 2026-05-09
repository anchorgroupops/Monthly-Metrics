"""Tests for src/agent_portal.py — per-agent self-service portal at /metrics."""

from __future__ import annotations

import json

import pytest


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def portal_app(isolated_db, isolated_thresholds, monkeypatch):
    """Flask app with the portal blueprint registered, isolated DB + thresholds."""
    monkeypatch.setenv("ADMIN_PASSWORD", "testpw")
    monkeypatch.setenv("FLASK_SECRET_KEY", "test-secret-not-for-prod")
    monkeypatch.delenv("DEPLOYMENT_MODE", raising=False)
    monkeypatch.delenv("PORTAL_BASE_URL", raising=False)
    monkeypatch.delenv("DEV_LOG_MAGIC_LINK", raising=False)

    # Make sure thresholds.json has at least one numeric metric so score_agent
    # has something to render on /metrics/dashboard.
    from tests.conftest import write_thresholds

    write_thresholds(
        isolated_thresholds,
        {
            "csat": {
                "label": "CSAT",
                "unit": "score",
                "target": 4.5,
                "yellow_floor": 4.0,
                "weight": 1.0,
                "gauge_size": "hero",
            },
        },
    )

    from src.dashboard import create_app

    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    return app


@pytest.fixture
def client(portal_app):
    return portal_app.test_client()


@pytest.fixture
def alice(isolated_db):
    """Seed an Alice agent with one period of data."""
    from src import storage

    storage.save_period(
        [
            {
                "agent_id": "100",
                "name": "Alice Smith",
                "email": "alice@example.com",
                "period": "2026-04",
                "csat": 4.6,
                "_raw": {},
            }
        ],
        source="csv",
    )
    return {"agent_id": "100", "email": "alice@example.com"}


# ── Public surface ───────────────────────────────────────────────────────────


class TestPublicRoutes:
    def test_root_anonymous_redirects_to_login(self, client):
        r = client.get("/metrics/")
        assert r.status_code == 302
        assert r.headers["Location"].endswith("/metrics/login")

    def test_login_form_renders(self, client):
        r = client.get("/metrics/login")
        assert r.status_code == 200
        assert b"Email me a sign-in link" in r.data
        assert b'action="/metrics/login"' in r.data

    def test_dashboard_anonymous_redirects_to_login(self, client):
        r = client.get("/metrics/dashboard")
        assert r.status_code == 302
        assert r.headers["Location"].endswith("/metrics/login")

    def test_healthz(self, client):
        r = client.get("/metrics/healthz")
        assert r.status_code == 200
        assert r.data == b"ok"

    def test_admin_login_still_at_root(self, client):
        # Adding the portal blueprint must not move admin off /login.
        r = client.get("/login")
        assert r.status_code == 200
        assert b"Anchor Metrics Admin" in r.data


# ── Magic-link issuance ──────────────────────────────────────────────────────


class TestLoginIssue:
    def test_unknown_email_does_not_send(self, client, mocker):
        sender = mocker.patch("src.agent_portal._send_magic_email")
        r = client.post("/metrics/login", data={"email": "stranger@example.com"})
        assert r.status_code == 200
        # Same "check inbox" page shown either way — no enumeration leak.
        assert b"Check your inbox" in r.data
        sender.assert_not_called()

    def test_known_email_sends_link(self, client, alice, mocker):
        sender = mocker.patch("src.agent_portal._send_magic_email")
        r = client.post("/metrics/login", data={"email": "alice@example.com"})
        assert r.status_code == 200
        assert b"Check your inbox" in r.data
        sender.assert_called_once()

        to_addr, html = sender.call_args.args
        assert to_addr == "alice@example.com"
        assert "/metrics/verify?token=" in html

    def test_email_match_is_case_insensitive(self, client, alice, mocker):
        sender = mocker.patch("src.agent_portal._send_magic_email")
        client.post("/metrics/login", data={"email": "ALICE@EXAMPLE.COM"})
        sender.assert_called_once()

    def test_smtp_failure_doesnt_leak_to_user(self, client, alice, mocker):
        # Operator log gets the trace; user still sees the same UI.
        mocker.patch(
            "src.agent_portal._send_magic_email",
            side_effect=RuntimeError("smtp boom"),
        )
        r = client.post("/metrics/login", data={"email": "alice@example.com"})
        assert r.status_code == 200
        assert b"Check your inbox" in r.data

    def test_dev_log_mode_skips_smtp(self, client, alice, mocker, monkeypatch, caplog):
        # With DEV_LOG_MAGIC_LINK + no SMTP creds, the URL is logged.
        monkeypatch.setattr("src.agent_portal.DEV_LOG_MAGIC_LINK", True)
        monkeypatch.setattr("src.agent_portal.SMTP_USER", "")
        monkeypatch.setattr("src.agent_portal.SMTP_PASSWORD", "")
        sender = mocker.patch("src.agent_portal._send_magic_email")
        with caplog.at_level("WARNING", logger="src.agent_portal"):
            client.post("/metrics/login", data={"email": "alice@example.com"})
        sender.assert_not_called()
        assert any("/metrics/verify?token=" in r.message for r in caplog.records)


# ── Verify + session cookie ──────────────────────────────────────────────────


def _issue_token_for(client, mocker, email: str) -> str:
    """POST /metrics/login, capture the magic-link token from the email body."""
    captured = {}

    def fake_send(_to, html):
        captured["url"] = html.split("/metrics/verify?token=")[1].split('"')[0]

    mocker.patch("src.agent_portal._send_magic_email", side_effect=fake_send)
    client.post("/metrics/login", data={"email": email})
    return captured["url"]


class TestVerify:
    def test_invalid_token_renders_login_with_error(self, client):
        r = client.get("/metrics/verify?token=nope")
        assert r.status_code == 200
        assert b"invalid or has expired" in r.data

    def test_valid_token_sets_session_cookie_and_redirects(
        self, client, alice, mocker
    ):
        token = _issue_token_for(client, mocker, "alice@example.com")
        r = client.get(f"/metrics/verify?token={token}")
        assert r.status_code == 302
        assert r.headers["Location"].endswith("/metrics/dashboard")
        # Session cookie scoped to /metrics so it isn't sent to /admin etc.
        cookies = r.headers.getlist("Set-Cookie")
        portal = next(c for c in cookies if c.startswith("anchor_portal="))
        assert "Path=/metrics" in portal
        assert "HttpOnly" in portal

    def test_token_is_single_use(self, client, alice, mocker):
        token = _issue_token_for(client, mocker, "alice@example.com")
        first = client.get(f"/metrics/verify?token={token}")
        second = client.get(f"/metrics/verify?token={token}")
        assert first.status_code == 302
        # Second use renders the login page with the error.
        assert second.status_code == 200
        assert b"invalid or has expired" in second.data


# ── Dashboard render ─────────────────────────────────────────────────────────


@pytest.fixture
def signed_in(client, alice, mocker):
    """Client with a portal session cookie set."""
    token = _issue_token_for(client, mocker, "alice@example.com")
    r = client.get(f"/metrics/verify?token={token}")
    # TestClient persists cookies automatically for follow-up requests.
    assert r.status_code == 302
    return client


class TestDashboard:
    def test_renders_gauge_for_known_agent(self, signed_in):
        r = signed_in.get("/metrics/dashboard")
        assert r.status_code == 200
        assert b"Alice Smith" in r.data
        # Inline SVG from gauges module is embedded.
        assert b"<svg" in r.data
        # Trend payload is JSON-encoded inside a data island.
        assert b'id="trend-data"' in r.data

    def test_empty_state_when_agent_has_no_periods(
        self, portal_app, isolated_db, mocker
    ):
        # Insert agent_meta directly, no agent_periods rows.
        from src import storage

        with storage.connect() as conn:
            conn.execute(
                "INSERT INTO agent_meta (agent_id, name, email) VALUES (?, ?, ?)",
                ("404", "Empty Eve", "eve@example.com"),
            )

        client = portal_app.test_client()
        token = _issue_token_for(client, mocker, "eve@example.com")
        client.get(f"/metrics/verify?token={token}")
        r = client.get("/metrics/dashboard")
        assert r.status_code == 200
        assert b"No data yet" in r.data

    def test_logout_clears_cookie_and_redirects(self, signed_in):
        r = signed_in.post("/metrics/logout")
        assert r.status_code == 302
        assert r.headers["Location"].endswith("/metrics/login")
        # Set-Cookie deleting the session must use the same Path.
        cookies = r.headers.getlist("Set-Cookie")
        cleared = next(c for c in cookies if c.startswith("anchor_portal="))
        assert "Path=/metrics" in cleared


# ── Trend payload XSS guard ──────────────────────────────────────────────────


class TestSafeScriptJson:
    def test_escapes_script_close_tag(self):
        from src.agent_portal import _safe_script_json

        out = _safe_script_json({"x": "</script>"})
        assert "</script>" not in out
        assert json.loads(out) == {"x": "</script>"}

    def test_escapes_html_comment_tokens(self):
        from src.agent_portal import _safe_script_json

        out = _safe_script_json({"x": "<!-- y -->"})
        assert "<!--" not in out and "-->" not in out
        assert json.loads(out) == {"x": "<!-- y -->"}

    def test_round_trip_for_normal_payload(self):
        from src.agent_portal import _safe_script_json

        payload = {
            "labels": ["2026-01", "2026-02"],
            "metrics": {"csat": {"target": 4.5, "values": [4.2, 4.4]}},
        }
        assert json.loads(_safe_script_json(payload)) == payload


# ── Storage helpers (smoke) ──────────────────────────────────────────────────


class TestPortalStorage:
    def test_magic_link_round_trip(self, isolated_db, alice):
        from src import portal_storage

        token = portal_storage.create_magic_link("alice@example.com", 15)
        assert isinstance(token, str) and len(token) > 20

        # First consume returns the email; second returns None.
        assert portal_storage.consume_magic_link(token) == "alice@example.com"
        assert portal_storage.consume_magic_link(token) is None

    def test_session_round_trip(self, isolated_db, alice):
        from src import portal_storage

        token = portal_storage.create_session("100", 30)
        agent = portal_storage.lookup_session(token)
        assert agent is not None and agent["email"] == "alice@example.com"

        portal_storage.delete_session(token)
        assert portal_storage.lookup_session(token) is None

    def test_find_agent_case_insensitive(self, isolated_db, alice):
        from src import portal_storage

        a = portal_storage.find_agent_by_email("ALICE@EXAMPLE.COM")
        assert a is not None and a["agent_id"] == "100"

    def test_find_agent_unknown_returns_none(self, isolated_db):
        from src import portal_storage

        assert portal_storage.find_agent_by_email("ghost@example.com") is None
