"""End-to-end-ish tests for the FastAPI dashboard routes."""

import pytest


@pytest.fixture
def client(monkeypatch, tmp_db, thresholds_full):
    """TestClient pointed at an isolated DB and patched thresholds."""
    monkeypatch.setattr("src.storage.DATABASE_PATH", tmp_db)
    monkeypatch.setattr("src.metrics.load_thresholds", lambda: thresholds_full)

    # Force the lazily-imported module to re-resolve DATABASE_PATH against tmp.
    from fastapi.testclient import TestClient

    from src.webapp.app import create_app
    app = create_app()
    return TestClient(app, follow_redirects=False)


@pytest.fixture
def alice(tmp_db):
    from src import storage
    storage.upsert_agents(
        [{"name": "Alice", "email": "alice@x", "fub_agent_id": "1"}],
        db_path=tmp_db,
    )
    return storage.get_agent_by_email("alice@x", db_path=tmp_db)


# ── Public surface ───────────────────────────────────────────────────────────

class TestRoot:
    def test_anonymous_redirects_to_login(self, client):
        r = client.get("/")
        assert r.status_code == 302
        assert r.headers["location"] == "/login"


class TestHealth:
    def test_healthz_returns_200(self, client):
        r = client.get("/healthz")
        assert r.status_code == 200
        assert r.text == "ok"


# ── Login flow ───────────────────────────────────────────────────────────────

class TestLoginForm:
    def test_get_renders_form(self, client):
        r = client.get("/login")
        assert r.status_code == 200
        assert "Email me a sign-in link" in r.text

    def test_post_known_email_emails_link(self, client, alice, mocker):
        send = mocker.patch("src.auth.send_html")
        r = client.post("/login", data={"email": "alice@x"})
        assert r.status_code == 200
        assert "Check your inbox" in r.text
        send.assert_called_once()

    def test_post_unknown_email_renders_same_page_no_email(self, client, mocker):
        send = mocker.patch("src.auth.send_html")
        r = client.post("/login", data={"email": "stranger@x"})
        # Same page text — no enumeration leak.
        assert r.status_code == 200
        assert "Check your inbox" in r.text
        send.assert_not_called()


class TestVerify:
    def test_invalid_token_renders_login_with_error(self, client):
        r = client.get("/verify?token=garbage")
        assert r.status_code == 200
        assert "invalid or has expired" in r.text

    def test_valid_token_sets_session_cookie_and_redirects(self, client, alice, mocker):
        # Capture the issued token from the email body.
        captured = {}
        def fake_send(to, subj, html):
            captured["url"] = html.split("/verify?token=")[1].split('"')[0]
        mocker.patch("src.auth.send_html", side_effect=fake_send)
        client.post("/login", data={"email": "alice@x"})

        r = client.get(f"/verify?token={captured['url']}")
        assert r.status_code == 302
        assert r.headers["location"] == "/dashboard"
        assert "anchor_session" in r.cookies


# ── Dashboard ────────────────────────────────────────────────────────────────

class TestDashboardAuth:
    def test_anonymous_redirects_to_login(self, client):
        r = client.get("/dashboard")
        assert r.status_code == 302
        assert r.headers["location"] == "/login"

    def test_logged_in_with_no_data_renders_empty_state(self, client, alice):
        from src import auth
        token = auth.start_session(alice["id"])
        client.cookies.set("anchor_session", token)
        r = client.get("/dashboard")
        assert r.status_code == 200
        assert "No data yet" in r.text

    def test_logged_in_with_snapshot_renders_gauges(
        self, client, alice, tmp_db
    ):
        from src import auth, storage
        storage.write_snapshot(
            {
                "agent_id": "1",
                "name": "Alice",
                "email": "alice@x",
                "period": "March 2026",
                "metrics": {
                    "pCVR":          {"value": 0.040},
                    "pickup_rate":   {"value": 0.90},
                    "csat":          {"value": 4.6},
                    "zhl_transfers": {"value": 4},
                },
                "overall_status": "Preferred",
            },
            db_path=tmp_db,
        )
        token = auth.start_session(alice["id"])
        client.cookies.set("anchor_session", token)
        r = client.get("/dashboard")
        assert r.status_code == 200
        # Inline SVG from gauges module is embedded.
        assert "<svg" in r.text
        # Trend payload is JSON-encoded inside a data island.
        assert 'id="trend-data"' in r.text
        # Overall status pill
        assert "Preferred" in r.text


class TestLogout:
    def test_logout_clears_cookie_and_redirects(self, client, alice):
        from src import auth
        token = auth.start_session(alice["id"])
        client.cookies.set("anchor_session", token)
        r = client.post("/logout")
        assert r.status_code == 302
        assert r.headers["location"] == "/login"
