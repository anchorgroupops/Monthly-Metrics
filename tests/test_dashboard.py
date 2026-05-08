"""Tests for src/dashboard.py — Flask + HTMX + Tailwind admin UI."""

import io
from datetime import datetime, timedelta

import pytest

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def app(isolated_db, monkeypatch):
    """Fresh Flask app per test — CSRF disabled, admin password set."""
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
    """Client with a logged-in session."""
    client.post("/login", data={"password": "testpw"}, follow_redirects=False)
    return client


@pytest.fixture
def seeded_storage(isolated_db):
    """Insert a small set of agents + drafts so /home and /review render."""
    from src import storage

    agents = [
        {
            "agent_id": "100",
            "name": "Alice",
            "email": "alice@x.com",
            "period": "2026-04",
            "csat": 4.6,
            "operational_readiness": 0.85,
            "_raw": {},
        },
        {
            "agent_id": "200",
            "name": "Bob",
            "email": "bob@x.com",
            "period": "2026-04",
            "csat": 3.9,
            "operational_readiness": 0.55,
            "_raw": {},
        },
    ]
    storage.save_period(agents, source="csv")
    return storage


# ── Healthz ──────────────────────────────────────────────────────────────────


class TestHealthz:
    def test_returns_200(self, client):
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert b"ok" in resp.data


# ── Root redirect ────────────────────────────────────────────────────────────


class TestRoot:
    def test_anonymous_redirects_to_login(self, client):
        resp = client.get("/")
        assert resp.status_code in (301, 302, 303)
        assert "/login" in resp.headers["Location"]

    def test_authed_redirects_to_home(self, auth_client):
        resp = auth_client.get("/")
        assert resp.status_code in (301, 302, 303)
        assert "/home" in resp.headers["Location"]


# ── Login + brute-force ──────────────────────────────────────────────────────


class TestLogin:
    def test_get_renders_form(self, client):
        resp = client.get("/login")
        assert resp.status_code == 200

    def test_correct_password_succeeds(self, client):
        resp = client.post("/login", data={"password": "testpw"}, follow_redirects=False)
        assert resp.status_code in (302, 303)
        assert "/home" in resp.headers["Location"]

    def test_wrong_password_re_renders_login(self, client):
        resp = client.post("/login", data={"password": "wrong"}, follow_redirects=False)
        assert resp.status_code == 200  # login template re-rendered with flash

    def test_brute_force_lockout_after_5_attempts(self, client):
        for _ in range(5):
            client.post("/login", data={"password": "wrong"})
        resp = client.post("/login", data={"password": "wrong"})
        assert resp.status_code == 429

    def test_lockout_returns_login_template(self, client):
        for _ in range(6):
            client.post("/login", data={"password": "wrong"})
        # The 429 handler renders admin/login.html with rate_limited=True
        resp = client.post("/login", data={"password": "wrong"})
        assert resp.status_code == 429


class TestLogout:
    def test_clears_session(self, auth_client):
        resp = auth_client.get("/logout", follow_redirects=False)
        assert resp.status_code in (302, 303)
        # After logout, root redirects back to login
        resp2 = auth_client.get("/")
        assert resp2.status_code in (301, 302, 303)
        assert "/login" in resp2.headers["Location"]


# ── login_required gate ──────────────────────────────────────────────────────


class TestLoginRequired:
    @pytest.mark.parametrize("path", ["/home", "/upload", "/pull-status", "/review/2026-04"])
    def test_get_redirects_to_login_when_anonymous(self, client, path):
        resp = client.get(path, follow_redirects=False)
        assert resp.status_code in (301, 302, 303)
        assert "/login" in resp.headers["Location"]


# ── Home ─────────────────────────────────────────────────────────────────────


class TestHome:
    def test_empty_state_when_no_periods(self, auth_client):
        resp = auth_client.get("/home")
        assert resp.status_code == 200

    def test_renders_with_data(self, app, seeded_storage):
        client = app.test_client()
        client.post("/login", data={"password": "testpw"})
        resp = client.get("/home")

        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "Alice" in body or "alice" in body.lower()


# ── Upload ───────────────────────────────────────────────────────────────────


class TestUpload:
    def test_get_renders_form(self, auth_client):
        resp = auth_client.get("/upload")
        assert resp.status_code == 200

    def test_post_no_file_flashes_error(self, auth_client):
        resp = auth_client.post("/upload", data={}, follow_redirects=False)
        assert resp.status_code in (302, 303)
        assert "/upload" in resp.headers["Location"]

    def test_post_wrong_extension_rejected(self, auth_client):
        resp = auth_client.post(
            "/upload",
            data={"file": (io.BytesIO(b"not csv"), "wrong.txt")},
            content_type="multipart/form-data",
            follow_redirects=False,
        )
        assert resp.status_code in (302, 303)
        assert "/upload" in resp.headers["Location"]

    def test_post_valid_csv_persists(self, auth_client, isolated_db):
        from src import storage

        # Column keys must match config/thresholds.json metrics
        csv_bytes = (
            b"agent_id,name,email,period,speed_to_action,work_with_rate,csat,appt_set_rate,appt_met_rate\n"
            b"100,Alice,alice@x.com,April 2026,120,0.62,0.91,0.71,0.78\n"
        )
        resp = auth_client.post(
            "/upload",
            data={"file": (io.BytesIO(csv_bytes), "april.csv")},
            content_type="multipart/form-data",
            follow_redirects=False,
        )
        assert resp.status_code in (302, 303)
        assert "/home" in resp.headers["Location"]

        loaded = storage.load_period("2026-04")
        assert len(loaded) == 1

    def test_post_malformed_csv_flashes_error(self, auth_client):
        bad_csv = b"this,is\nnot,a,valid,csv\n"  # missing required columns
        resp = auth_client.post(
            "/upload",
            data={"file": (io.BytesIO(bad_csv), "bad.csv")},
            content_type="multipart/form-data",
            follow_redirects=False,
        )
        # csv_ingest raises ValueError → flash + redirect to /upload
        assert resp.status_code in (302, 303)


# ── Pull (manual FUB pull background job) ────────────────────────────────────


class TestPull:
    def test_pull_now_no_agents_flashes_error(self, auth_client, monkeypatch):

        # Module-level imports inside the route — patch settings module
        from config import settings

        monkeypatch.setattr(settings, "AGENTS", [])
        resp = auth_client.post("/pull-now", follow_redirects=False)
        assert resp.status_code in (302, 303)

    def test_pull_now_no_api_key_flashes_error(self, auth_client, monkeypatch):
        from config import settings

        monkeypatch.setattr(
            settings, "AGENTS", [{"name": "x", "email": "x@x", "fub_agent_id": "1"}]
        )
        monkeypatch.setattr(settings, "FUB_API_KEY", "")
        resp = auth_client.post("/pull-now", follow_redirects=False)
        assert resp.status_code in (302, 303)

    def test_pull_now_already_running_blocks_second(self, auth_client, monkeypatch):
        from config import settings
        from src import storage

        monkeypatch.setattr(
            settings, "AGENTS", [{"name": "x", "email": "x@x", "fub_agent_id": "1"}]
        )
        monkeypatch.setattr(settings, "FUB_API_KEY", "test-key")

        # Manually create an in-progress run
        storage.start_run(source="fub")

        resp = auth_client.post("/pull-now", follow_redirects=False)
        assert resp.status_code in (302, 303)

    def test_pull_status_renders(self, auth_client):
        resp = auth_client.get("/pull-status")
        assert resp.status_code == 200

    def test_pull_now_starts_thread(self, auth_client, monkeypatch, mocker):
        from config import settings

        monkeypatch.setattr(
            settings, "AGENTS", [{"name": "x", "email": "x@x", "fub_agent_id": "1"}]
        )
        monkeypatch.setattr(settings, "FUB_API_KEY", "test-key")

        # Mock Thread to avoid actually running the worker
        thread_mock = mocker.patch("src.dashboard.threading.Thread")

        resp = auth_client.post("/pull-now", follow_redirects=False)
        assert resp.status_code in (302, 303)
        thread_mock.assert_called_once()
        thread_mock.return_value.start.assert_called_once()


# ── Review ───────────────────────────────────────────────────────────────────


class TestReview:
    def test_empty_period_renders(self, auth_client):
        resp = auth_client.get("/review/2026-04")
        assert resp.status_code == 200

    def test_with_drafts(self, app, seeded_storage):
        from src import storage

        storage.queue_draft("100", "2026-04", "<html>Hi Alice</html>")

        client = app.test_client()
        client.post("/login", data={"password": "testpw"})
        resp = client.get("/review/2026-04")
        assert resp.status_code == 200


class TestDraftPreview:
    def test_returns_html(self, auth_client, isolated_db):
        from src import storage

        storage.save_period(
            [
                {
                    "agent_id": "100",
                    "name": "A",
                    "email": "a@x",
                    "period": "2026-04",
                    "csat": 0.85,
                    "_raw": {},
                }
            ],
            source="csv",
        )
        draft_id = storage.queue_draft("100", "2026-04", "<html><body>Preview body</body></html>")

        resp = auth_client.get(f"/draft/{draft_id}")
        assert resp.status_code == 200
        assert b"Preview body" in resp.data

    def test_404_on_missing(self, auth_client):
        resp = auth_client.get("/draft/99999")
        assert resp.status_code == 404


class TestDraftApprove:
    def test_approve_marks_draft(self, auth_client, isolated_db):
        from src import storage

        storage.save_period(
            [
                {
                    "agent_id": "100",
                    "name": "A",
                    "email": "a@x",
                    "period": "2026-04",
                    "csat": 0.85,
                    "_raw": {},
                }
            ],
            source="csv",
        )
        draft_id = storage.queue_draft("100", "2026-04", "<html/>")

        resp = auth_client.post(f"/draft/{draft_id}/approve")
        assert resp.status_code == 200

        approved = storage.list_drafts(status="approved")
        assert len(approved) == 1

    def test_404_on_missing(self, auth_client):
        resp = auth_client.post("/draft/99999/approve")
        assert resp.status_code == 404


class TestDraftReject:
    def test_reject_marks_draft(self, auth_client, isolated_db):
        from src import storage

        storage.save_period(
            [
                {
                    "agent_id": "100",
                    "name": "A",
                    "email": "a@x",
                    "period": "2026-04",
                    "csat": 0.85,
                    "_raw": {},
                }
            ],
            source="csv",
        )
        draft_id = storage.queue_draft("100", "2026-04", "<html/>")

        resp = auth_client.post(f"/draft/{draft_id}/reject")
        assert resp.status_code == 200

        rejected = storage.list_drafts(status="rejected")
        assert len(rejected) == 1

    def test_404_on_missing(self, auth_client):
        resp = auth_client.post("/draft/99999/reject")
        assert resp.status_code == 404


class TestApproveAll:
    def test_approves_all_pending_in_period(self, auth_client, isolated_db):
        from src import storage

        storage.save_period(
            [
                {
                    "agent_id": "100",
                    "name": "A",
                    "email": "a@x",
                    "period": "2026-04",
                    "csat": 0.85,
                    "_raw": {},
                },
                {
                    "agent_id": "200",
                    "name": "B",
                    "email": "b@x",
                    "period": "2026-04",
                    "csat": 0.75,
                    "_raw": {},
                },
            ],
            source="csv",
        )
        storage.queue_draft("100", "2026-04", "<html/>")
        storage.queue_draft("200", "2026-04", "<html/>")

        resp = auth_client.post("/review/2026-04/approve_all", follow_redirects=False)
        assert resp.status_code in (302, 303)

        approved = storage.list_drafts(status="approved")
        assert len(approved) == 2


# ── Send ─────────────────────────────────────────────────────────────────────


class TestSend:
    def test_no_approved_flashes_error(self, auth_client):
        resp = auth_client.post("/send", follow_redirects=False)
        assert resp.status_code in (302, 303)
        assert "/home" in resp.headers["Location"]

    def test_missing_smtp_creds_flashes_error(self, auth_client, isolated_db, monkeypatch):
        from src import dashboard, storage

        monkeypatch.setattr(dashboard, "SMTP_USER", "")
        monkeypatch.setattr(dashboard, "SMTP_PASSWORD", "")

        storage.save_period(
            [
                {
                    "agent_id": "100",
                    "name": "A",
                    "email": "a@x",
                    "period": "2026-04",
                    "csat": 0.85,
                    "_raw": {},
                }
            ],
            source="csv",
        )
        draft_id = storage.queue_draft("100", "2026-04", "<html/>")
        storage.approve_draft(draft_id)

        resp = auth_client.post("/send", follow_redirects=False)
        assert resp.status_code in (302, 303)

    def test_sends_via_smtp_on_happy_path(self, auth_client, isolated_db, monkeypatch, mocker):
        from src import dashboard, storage

        monkeypatch.setattr(dashboard, "SMTP_USER", "user@x.com")
        monkeypatch.setattr(dashboard, "SMTP_PASSWORD", "pw")
        monkeypatch.setattr(dashboard, "SMTP_HOST", "smtp.example.com")
        monkeypatch.setattr(dashboard, "SMTP_PORT", 587)

        smtp_class = mocker.patch("src.dashboard.smtplib.SMTP")
        server = smtp_class.return_value.__enter__.return_value

        storage.save_period(
            [
                {
                    "agent_id": "100",
                    "name": "A",
                    "email": "alice@example.com",
                    "period": "2026-04",
                    "csat": 0.85,
                    "_raw": {},
                }
            ],
            source="csv",
        )
        draft_id = storage.queue_draft("100", "2026-04", "<html>email body</html>")
        storage.approve_draft(draft_id)

        resp = auth_client.post("/send", follow_redirects=False)
        assert resp.status_code in (302, 303)

        server.starttls.assert_called_once()
        server.login.assert_called_once_with("user@x.com", "pw")
        assert server.sendmail.call_count == 1

        sent_drafts = storage.list_drafts(status="sent")
        assert len(sent_drafts) == 1

    def test_smtp_error_flashes_error(self, auth_client, isolated_db, monkeypatch, mocker):
        import smtplib

        from src import dashboard, storage

        monkeypatch.setattr(dashboard, "SMTP_USER", "user@x.com")
        monkeypatch.setattr(dashboard, "SMTP_PASSWORD", "pw")

        smtp_class = mocker.patch("src.dashboard.smtplib.SMTP")
        smtp_class.return_value.__enter__.side_effect = smtplib.SMTPException("boom")

        storage.save_period(
            [
                {
                    "agent_id": "100",
                    "name": "A",
                    "email": "a@x",
                    "period": "2026-04",
                    "csat": 0.85,
                    "_raw": {},
                }
            ],
            source="csv",
        )
        draft_id = storage.queue_draft("100", "2026-04", "<html/>")
        storage.approve_draft(draft_id)

        resp = auth_client.post("/send", follow_redirects=False)
        assert resp.status_code in (302, 303)


# ── _check_password ──────────────────────────────────────────────────────────


class TestCheckPassword:
    def test_correct_password(self, monkeypatch):
        from src.dashboard import _check_password

        monkeypatch.setenv("ADMIN_PASSWORD", "secret123")
        assert _check_password("secret123") is True

    def test_wrong_password(self, monkeypatch):
        from src.dashboard import _check_password

        monkeypatch.setenv("ADMIN_PASSWORD", "secret123")
        assert _check_password("wrong") is False

    def test_empty_password_does_not_match_default(self, monkeypatch):
        from src.dashboard import _check_password

        monkeypatch.setenv("ADMIN_PASSWORD", "anchor")
        # An empty password should not match — even though both inputs are
        # short, compare_digest will reject ""
        assert _check_password("") is False


# ── _reap_stale_runs ─────────────────────────────────────────────────────────


class TestReapStaleRuns:
    def test_no_active_run_is_noop(self, isolated_db):
        from src.dashboard import _reap_stale_runs

        _reap_stale_runs()  # should not raise

    def test_recent_run_is_left_alone(self, isolated_db):
        from src import storage
        from src.dashboard import _reap_stale_runs

        run_id = storage.start_run(source="fub")
        _reap_stale_runs()

        active = storage.get_active_run()
        assert active is not None
        assert active["id"] == run_id

    def test_stale_run_is_marked_error(self, isolated_db, monkeypatch):
        from src import storage
        from src.dashboard import _reap_stale_runs

        run_id = storage.start_run(source="fub")
        # Backdate the row's created_at to 1 hour ago
        with storage.connect() as conn:
            old = (datetime.utcnow() - timedelta(hours=1)).isoformat()
            conn.execute("UPDATE runs SET created_at = ? WHERE id = ?", (old, run_id))

        _reap_stale_runs()

        with storage.connect() as conn:
            row = conn.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()
        assert row["status"] == "error"


# ── _pull_pipeline_worker ────────────────────────────────────────────────────


class TestPullPipelineWorker:
    def test_happy_path(self, isolated_db, monkeypatch, mocker):
        from src import storage
        from src.dashboard import _pull_pipeline_worker

        run_id = storage.start_run(source="fub")

        # Mock the FUB fetch to return one agent
        mocker.patch(
            "src.fub_client.fetch_all_agents",
            return_value=[
                {
                    "agent_id": "100",
                    "name": "Alice",
                    "email": "a@x",
                    "period": "April 2026",
                    "csat": 0.85,
                    "_raw": {},
                },
            ],
        )
        # Mock research to skip the network call
        mocker.patch("src.threshold_researcher.run_research")
        # Mock email build to keep test fast
        mocker.patch("src.email_builder.build_email", return_value="<html/>")

        _pull_pipeline_worker(run_id)

        with storage.connect() as conn:
            row = conn.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()
        assert row["status"] == "ok"

    def test_zero_agents_marks_ok_with_note(self, isolated_db, mocker):
        from src import storage
        from src.dashboard import _pull_pipeline_worker

        run_id = storage.start_run(source="fub")
        mocker.patch("src.fub_client.fetch_all_agents", return_value=[])

        _pull_pipeline_worker(run_id)

        with storage.connect() as conn:
            row = conn.execute("SELECT status, notes FROM runs WHERE id = ?", (run_id,)).fetchone()
        assert row["status"] == "ok"
        assert "0 agents" in row["notes"]

    def test_fetch_failure_marks_error_and_alerts(self, isolated_db, mocker):
        from src import storage
        from src.dashboard import _pull_pipeline_worker

        run_id = storage.start_run(source="fub")
        mocker.patch("src.fub_client.fetch_all_agents", side_effect=RuntimeError("boom"))
        notify_mock = mocker.patch("src.notifier.notify_admin_failure", return_value=True)

        _pull_pipeline_worker(run_id)

        with storage.connect() as conn:
            row = conn.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()
        assert row["status"] == "error"
        notify_mock.assert_called_once()

    def test_research_failure_does_not_abort(self, isolated_db, mocker):
        """Step 2 (research) is non-fatal — if it fails, drafts still queue."""
        from src import storage
        from src.dashboard import _pull_pipeline_worker

        run_id = storage.start_run(source="fub")
        mocker.patch(
            "src.fub_client.fetch_all_agents",
            return_value=[
                {
                    "agent_id": "100",
                    "name": "Alice",
                    "email": "a@x",
                    "period": "April 2026",
                    "csat": 0.85,
                    "_raw": {},
                },
            ],
        )
        mocker.patch(
            "src.threshold_researcher.run_research",
            side_effect=RuntimeError("research api down"),
        )
        mocker.patch("src.email_builder.build_email", return_value="<html/>")

        _pull_pipeline_worker(run_id)

        with storage.connect() as conn:
            row = conn.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()
        assert row["status"] == "ok"


# ── Template filters ─────────────────────────────────────────────────────────


class TestTemplateFilters:
    def test_metric_value_filter(self, app):
        with app.app_context():
            f = app.jinja_env.filters["metric_value"]
            assert f({"value": None}) == "—"
            assert f({"value": 0.85, "unit": "percent"}) == "85.0%"
            assert f({"value": 30, "unit": "seconds"}) == "30s"
            assert f({"value": 90, "unit": "seconds"}) == "1m 30s"
            assert f({"value": 120, "unit": "seconds"}) == "2m"
            assert f({"value": 4.5, "unit": "score"}) == "4.5"
            assert f({"value": 7, "unit": "count"}) == "7"
            assert f({"value": 1.234, "unit": ""}) == "1.23"

    def test_status_color_filter(self, app):
        with app.app_context():
            f = app.jinja_env.filters["status_color"]
            assert "emerald" in f("Preferred")
            assert "amber" in f("At Risk")
            assert "rose" in f("Needs Improvement")
            assert "slate" in f("No Data")
            assert "slate" in f("Unknown")


# ── CSRF (separate fixture with CSRF re-enabled) ──────────────────────────────


@pytest.fixture
def csrf_app(isolated_db, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "testpw")
    monkeypatch.setenv("FLASK_SECRET_KEY", "test-secret")

    from src.dashboard import create_app

    app = create_app()
    app.config["TESTING"] = True
    return app  # CSRF default-enabled


class TestCSRFEnforcement:
    def test_post_without_csrf_token_rejected(self, csrf_app):
        client = csrf_app.test_client()
        resp = client.post("/login", data={"password": "testpw"})
        # Flask-WTF returns 400 for missing CSRF
        assert resp.status_code in (400, 403)

    def test_healthz_csrf_exempt(self, csrf_app):
        """healthz must be CSRF-exempt so probes can hit it without a token."""
        client = csrf_app.test_client()
        resp = client.get("/healthz")
        assert resp.status_code == 200


# ── Production-mode toggles ──────────────────────────────────────────────────


class TestProductionMode:
    def test_secure_cookies_when_deployment_mode_production(self, isolated_db, monkeypatch):
        monkeypatch.setenv("ADMIN_PASSWORD", "testpw")
        monkeypatch.setenv("FLASK_SECRET_KEY", "test-secret")
        monkeypatch.setenv("DEPLOYMENT_MODE", "production")

        from src.dashboard import create_app

        app = create_app()
        assert app.config.get("SESSION_COOKIE_SECURE") is True
        assert app.config.get("SESSION_COOKIE_HTTPONLY") is True
        assert app.config.get("SESSION_COOKIE_SAMESITE") == "Lax"
        assert app.config.get("PREFERRED_URL_SCHEME") == "https"
