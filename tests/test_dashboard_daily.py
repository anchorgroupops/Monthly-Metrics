"""Tests for the /daily dashboard route family."""

from __future__ import annotations

import pytest

# ── Fixtures (mirror the existing test_dashboard ones) ────────────────────────


@pytest.fixture
def app(isolated_db, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "testpw")
    monkeypatch.setenv("FLASK_SECRET_KEY", "test-secret-not-for-prod")
    monkeypatch.delenv("DEPLOYMENT_MODE", raising=False)

    from src.dashboard import create_app

    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    return app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def auth_client(client):
    client.post("/login", data={"password": "testpw"}, follow_redirects=False)
    return client


@pytest.fixture
def seeded_daily(isolated_db):
    """Two agents with snapshots so /daily/data has rows to render."""
    from src import storage

    storage.save_daily_snapshot(
        agent_id="100",
        snapshot_date="2026-05-15",
        name="Alice",
        email="alice@x.com",
        metrics={
            "response_time_seconds": 180.0,
            "contact_rate": 0.85,
            "pickup_rate": 0.30,
            "appointment_rate": 0.25,
            "lead_acceptance_rate": 0.80,
            "call_volume": 40,
            "texts_sent": 80,
            "emails_sent": 25,
            "conversations_2min": 12,
            "appointments_set": 5,
            "new_leads_not_acted_on": 1,
            "total_zillow_leads": 20,
            "activity_points": 5 * 500 + 12 * 100 + 40 * 10 + 80 * 2 + 25,
        },
    )
    storage.save_daily_snapshot(
        agent_id="200",
        snapshot_date="2026-05-15",
        name="Bob",
        email="bob@x.com",
        metrics={
            "response_time_seconds": 700.0,  # red
            "contact_rate": 0.40,  # red
            "pickup_rate": 0.10,  # red
            "appointment_rate": 0.05,  # red
            "lead_acceptance_rate": 0.50,
            "call_volume": 5,
            "texts_sent": 10,
            "emails_sent": 3,
            "conversations_2min": 1,
            "appointments_set": 0,
            "new_leads_not_acted_on": 6,
            "total_zillow_leads": 10,
            "activity_points": 1 * 100 + 5 * 10 + 10 * 2 + 3,
        },
    )


# ── /daily landing page ──────────────────────────────────────────────────────


class TestDailyLanding:
    def test_unauthenticated_redirects_to_login(self, client):
        resp = client.get("/daily", follow_redirects=False)
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_authenticated_renders(self, auth_client):
        resp = auth_client.get("/daily")
        assert resp.status_code == 200
        body = resp.data.decode()
        assert "Daily Activity" in body
        # The page contains the HTMX self-loader pointing at /daily/data.
        assert "/daily/data" in body
        # Refresh-now button is present.
        assert "/daily/refresh" in body


# ── /daily/data (HTMX-swapped partial) ───────────────────────────────────────


class TestDailyData:
    def test_empty_state_when_no_snapshots(self, auth_client):
        resp = auth_client.get("/daily/data")
        assert resp.status_code == 200
        body = resp.data.decode()
        assert "No daily snapshots" in body or "No FUB pull recorded" in body

    def test_renders_agents_when_snapshots_exist(self, auth_client, seeded_daily):
        resp = auth_client.get("/daily/data")
        assert resp.status_code == 200
        body = resp.data.decode()
        assert "Alice" in body
        assert "Bob" in body
        # Activity points are formatted with a thousands separator.
        # Alice: 5*500 + 12*100 + 40*10 + 80*2 + 25 = 2500+1200+400+160+25 = 4285
        assert "4,285" in body
        # Team total appointments_set = 5 + 0 = 5
        assert "Team total" in body

    def test_color_coding_classes_present(self, auth_client, seeded_daily):
        resp = auth_client.get("/daily/data")
        body = resp.data.decode()
        # Bob's metrics are all red — should hit the rose class.
        assert "bg-rose-500" in body
        # Alice's contact_rate (85%) is green — emerald class should appear.
        assert "bg-emerald-500" in body

    def test_sorted_by_activity_points_descending(self, auth_client, seeded_daily):
        resp = auth_client.get("/daily/data")
        body = resp.data.decode()
        # Alice (4285 pts) appears before Bob (123 pts) in HTML order.
        assert body.index("Alice") < body.index("Bob")


# ── /daily/refresh ──────────────────────────────────────────────────────────


class TestDailyRefresh:
    def test_unauthenticated_blocked(self, client):
        resp = client.post("/daily/refresh", follow_redirects=False)
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_without_api_key_flashes_error(self, auth_client, monkeypatch):
        from config import settings

        monkeypatch.setattr(settings, "FUB_API_KEY", "")
        resp = auth_client.post("/daily/refresh", follow_redirects=True)
        assert resp.status_code == 200
        body = resp.data.decode()
        assert "FUB_API_KEY" in body

    def test_blocks_when_pull_already_in_progress(self, auth_client, monkeypatch):
        from config import settings
        from src import storage

        monkeypatch.setattr(settings, "FUB_API_KEY", "test-key")
        # Manually mark a run in-progress.
        storage.start_run(source="fub-daily")

        resp = auth_client.post("/daily/refresh", follow_redirects=True)
        body = resp.data.decode()
        assert "already in progress" in body


# ── _classify / _progress_pct (pure helpers) ─────────────────────────────────


class TestClassify:
    def test_lower_is_better_green_when_at_or_below_target(self):
        from src.dashboard import _classify

        cfg = {"target": 300, "yellow_floor": 600, "direction": "lower"}
        assert _classify(150, cfg) == "green"
        assert _classify(300, cfg) == "green"
        assert _classify(500, cfg) == "yellow"
        assert _classify(601, cfg) == "red"

    def test_higher_is_better(self):
        from src.dashboard import _classify

        cfg = {"target": 0.80, "yellow_floor": 0.60, "direction": "higher"}
        assert _classify(0.95, cfg) == "green"
        assert _classify(0.80, cfg) == "green"
        assert _classify(0.65, cfg) == "yellow"
        assert _classify(0.30, cfg) == "red"

    def test_none_returns_no_data(self):
        from src.dashboard import _classify

        cfg = {"target": 1, "yellow_floor": 0, "direction": "higher"}
        assert _classify(None, cfg) == "no_data"


class TestProgressPct:
    def test_higher_is_better_clamped_to_100(self):
        from src.dashboard import _progress_pct

        cfg = {"target": 0.80, "yellow_floor": 0.60, "direction": "higher"}
        assert _progress_pct(0.40, cfg) == 50
        assert _progress_pct(0.80, cfg) == 100
        assert _progress_pct(1.20, cfg) == 100  # over-target capped

    def test_lower_is_better_full_when_at_target(self):
        from src.dashboard import _progress_pct

        cfg = {"target": 300, "yellow_floor": 600, "direction": "lower"}
        assert _progress_pct(150, cfg) == 100
        assert _progress_pct(300, cfg) == 100
        # 600 → 100 * 300 / 600 = 50
        assert _progress_pct(600, cfg) == 50

    def test_none_returns_zero(self):
        from src.dashboard import _progress_pct

        cfg = {"target": 0.80, "yellow_floor": 0.60, "direction": "higher"}
        assert _progress_pct(None, cfg) == 0
