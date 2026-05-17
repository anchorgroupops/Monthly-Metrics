"""
Template smoke tests.

Every Jinja template in templates/ must render with realistic context without
raising, and must produce output that doesn't carry unrendered template
syntax through to the browser/email client.

Why this exists separately from the route-level tests in test_dashboard.py:

- ``deck.html.j2`` is reached via the CLI ``--mode review`` path only and was
  previously not exercised in tests at all — a Jinja syntax error there would
  only surface in production on the first of the month.
- ``email.html.j2`` has its privacy contract (``test_privacy``) and round-trip
  builder coverage, but no positive structural assertions — adding a stray
  ``{% if %}`` would silently produce broken HTML.
- ``_pull_status.html`` has three mutually-exclusive states (active, last
  failed, never run) and only the "no run" path was hit by existing tests.
- ``_draft_row.html`` is rendered by approve/reject routes but only with
  ``status in {approved, rejected}``; the pending- and sent-status branches
  had no coverage.

The pattern: drive every template through Flask's ``render_template`` or the
existing builders so we exercise the actual code path that production uses,
then assert (a) it renders, (b) it contains expected anchors, (c) the output
has no stray ``{{`` / ``{%`` and no ``Undefined`` markers that would indicate
a silent missing-variable failure.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# ── Generic output sanity ─────────────────────────────────────────────────────


def _assert_clean_html(html: str) -> None:
    """Catch the most common 'this template silently broke' symptoms."""
    assert html and len(html) > 100, "template produced no/too-little output"
    # Unrendered Jinja delimiters leaking through means a syntax error escaped.
    assert "{{" not in html, "unrendered '{{' in output — template broke mid-render"
    assert "{%" not in html, "unrendered '{%' in output — template broke mid-render"
    # Jinja's default Undefined renders as empty string, but `repr(Undefined)`
    # shows up if any debug filter / |string applied to a missing var.
    assert "Undefined" not in html, "Undefined leaked into rendered output"


# ── Realistic data builders ───────────────────────────────────────────────────


def _scored_agent(
    agent_id: str = "100",
    name: str = "Alice Smith",
    email: str = "alice@example.com",
    overall: str = "Preferred",
):
    """Build a complete scored-agent dict using the real scoring engine."""
    from src.metrics import load_thresholds, score_agent

    raw = {
        "agent_id": agent_id,
        "name": name,
        "email": email,
        "period": "April 2026",
        "speed_to_action": 180.0,
        "work_with_rate": 0.65,
        "csat": 0.92,
        "appt_set_rate": 0.72,
        "appt_met_rate": 0.78,
    }
    if overall == "At Risk":
        raw["speed_to_action"] = 500.0
        raw["work_with_rate"] = 0.42
    elif overall == "Needs Improvement":
        raw["speed_to_action"] = 1200.0
        raw["work_with_rate"] = 0.20
        raw["csat"] = 0.50
    elif overall == "No Data":
        for k in ("speed_to_action", "work_with_rate", "csat", "appt_set_rate", "appt_met_rate"):
            raw[k] = None

    return score_agent(raw, load_thresholds())


@pytest.fixture
def app(isolated_db, isolated_thresholds, monkeypatch):
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


# ─────────────────────────────────────────────────────────────────────────────
# email.html.j2 — render via the production builder
# ─────────────────────────────────────────────────────────────────────────────


class TestEmailTemplate:
    def test_renders_all_overall_statuses(self, isolated_thresholds, isolated_db):
        """Every overall_status branch in the template must render cleanly."""
        from src.email_builder import build_email

        for status in ("Preferred", "At Risk", "Needs Improvement", "No Data"):
            agent = _scored_agent(overall=status)
            html = build_email(agent)
            _assert_clean_html(html)

    def test_contains_brand_chrome(self, isolated_thresholds, isolated_db):
        from src.email_builder import build_email

        html = build_email(_scored_agent())

        # Header, greeting, footer all present and use the agent's first name.
        assert "The Anchor Team" in html
        assert "Hi Alice" in html
        assert "Performance Report" in html
        # Hero gauge SVG embedded (autoescaped HTML in email template would
        # break the SVG render — guard against autoescape regressions).
        assert "<svg" in html
        # Period label rendered in both the header and the footer.
        assert html.count("April 2026") >= 2

    def test_no_data_agent_renders_target_tbd(self, isolated_thresholds, isolated_db):
        """When metrics are all None, the 'Target: TBD' branches should fire."""
        from src.email_builder import build_email

        html = build_email(_scored_agent(overall="No Data"))
        _assert_clean_html(html)
        # The template has explicit 'TBD' fallbacks for missing targets in
        # both hero and secondary gauge sections.
        assert "No Data Available" in html


# ─────────────────────────────────────────────────────────────────────────────
# deck.html.j2 — previously zero test coverage
# ─────────────────────────────────────────────────────────────────────────────


class TestDeckTemplate:
    def test_renders_with_multi_agent_team(self, isolated_thresholds, isolated_db):
        from src.deck_builder import build_deck

        agents = [
            _scored_agent("100", "Alice Smith", "alice@x.com", "Preferred"),
            _scored_agent("200", "Bob Jones", "bob@x.com", "At Risk"),
            _scored_agent("300", "Carol Lee", "carol@x.com", "Needs Improvement"),
        ]
        html = build_deck(agents)
        _assert_clean_html(html)

        # Every agent appears on at least one slide (overview + own slide).
        for a in agents:
            assert a["name"] in html
        # Reveal.js scaffold + status-class chrome rendered.
        assert 'class="reveal"' in html
        assert "status-Preferred" in html
        assert "status-At-Risk" in html
        assert "status-Needs-Improvement" in html

    def test_renders_with_single_agent(self, isolated_thresholds, isolated_db):
        from src.deck_builder import build_deck

        html = build_deck([_scored_agent()])
        _assert_clean_html(html)
        assert "Alice Smith" in html

    def test_empty_input_raises(self, isolated_thresholds, isolated_db):
        from src.deck_builder import build_deck

        with pytest.raises(ValueError, match="no agent data"):
            build_deck([])

    def test_renders_with_nodata_agent(self, isolated_thresholds, isolated_db):
        """Cover the 'No Data' badge branch in both overview and per-agent slides."""
        from src.deck_builder import build_deck

        html = build_deck([_scored_agent(overall="No Data")])
        _assert_clean_html(html)
        assert "status-No-Data" in html


# ─────────────────────────────────────────────────────────────────────────────
# admin/_pull_status.html — three mutually exclusive states
# ─────────────────────────────────────────────────────────────────────────────


class TestPullStatusPartial:
    def test_no_run_recorded(self, auth_client):
        # /pull-status with no runs at all → "No FUB pull recorded yet."
        r = auth_client.get("/pull-status")
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        _assert_clean_html(body)
        assert "No FUB pull recorded yet" in body

    def test_active_run_renders_running_state(self, auth_client, isolated_db):
        from src import storage

        # Start a run but never finish it — get_active_run() should see it.
        run_id = storage.start_run(source="fub")
        try:
            r = auth_client.get("/pull-status")
            assert r.status_code == 200
            body = r.get_data(as_text=True)
            _assert_clean_html(body)
            assert "Pulling from FUB" in body
            assert f"Run #{run_id}" in body
        finally:
            storage.finish_run(run_id, "ok", "cleanup")

    def test_last_run_success(self, auth_client, isolated_db):
        from src import storage

        run_id = storage.start_run(source="fub")
        storage.finish_run(run_id, "ok", "12 agents")

        r = auth_client.get("/pull-status")
        body = r.get_data(as_text=True)
        _assert_clean_html(body)
        assert "Last FUB pull: success" in body
        assert "12 agents" in body

    def test_last_run_failed(self, auth_client, isolated_db):
        from src import storage

        run_id = storage.start_run(source="fub")
        storage.finish_run(run_id, "error", "FUB 500 — retried 3x")

        r = auth_client.get("/pull-status")
        body = r.get_data(as_text=True)
        _assert_clean_html(body)
        assert "Last FUB pull: failed" in body
        assert "FUB 500" in body


# ─────────────────────────────────────────────────────────────────────────────
# admin/_draft_row.html — exercise all four status badges
# ─────────────────────────────────────────────────────────────────────────────


class TestDraftRowAllStatuses:
    def _queue_and_set_status(self, status):
        from src import storage

        storage.save_period(
            [
                {
                    "agent_id": "100",
                    "name": "Alice Smith",
                    "email": "alice@x.com",
                    "period": "2026-04",
                    "csat": 0.92,
                    "_raw": {},
                }
            ],
            source="csv",
        )
        draft_id = storage.queue_draft("100", "April 2026", "<p>hi</p>")
        if status == "approved":
            storage.approve_draft(draft_id)
        elif status == "rejected":
            storage.reject_draft(draft_id)
        elif status == "sent":
            storage.approve_draft(draft_id)
            storage.mark_sent(draft_id)
        return draft_id

    @pytest.mark.parametrize(
        "status,badge_text",
        [
            ("pending", "Pending"),
            ("approved", "Approved"),
            ("rejected", "Rejected"),
            ("sent", "Sent"),
        ],
    )
    def test_status_badge_renders(self, app, isolated_db, status, badge_text):
        """The review page includes _draft_row.html — its four status badges
        all need to render. Previously only approved/rejected were covered
        (via approve/reject route tests), so sent/pending badges could break
        silently."""
        self._queue_and_set_status(status)

        client = app.test_client()
        client.post("/login", data={"password": "testpw"})
        r = client.get("/review/2026-04")
        body = r.get_data(as_text=True)

        _assert_clean_html(body)
        assert badge_text in body
        # And the agent's name made it into the row.
        assert "Alice Smith" in body


# ─────────────────────────────────────────────────────────────────────────────
# admin/_daily_table.html — empty + populated states
# ─────────────────────────────────────────────────────────────────────────────


class TestDailyTablePartial:
    def test_empty_state_renders(self, auth_client):
        r = auth_client.get("/daily/data")
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        _assert_clean_html(body)
        assert "No daily snapshot yet" in body

    def test_populated_renders_leaderboard(self, auth_client, isolated_db):
        from src import storage

        storage.save_daily_snapshot(
            agent_id="100",
            snapshot_date="2026-05-15",
            metrics={
                "response_time_seconds": 240.0,
                "contact_rate": 0.85,
                "pickup_rate": 0.30,
                "appointment_rate": 0.25,
                "call_volume": 45,
                "texts_sent": 90,
                "emails_sent": 30,
                "conversations_2min": 12,
                "appointments_set": 6,
                "new_leads_not_acted_on": 1,
                "total_zillow_leads": 20,
                "activity_points": 7000,
            },
            name="Alice Smith",
            email="alice@x.com",
        )

        r = auth_client.get("/daily/data")
        body = r.get_data(as_text=True)
        _assert_clean_html(body)
        # Leaderboard chrome + the agent + activity points formatted with comma.
        assert "Agent leaderboard" in body
        assert "Alice Smith" in body
        assert "7,000" in body


# ─────────────────────────────────────────────────────────────────────────────
# admin/home.html — covers the No-Data agent branch (separate list + toggle)
# ─────────────────────────────────────────────────────────────────────────────


class TestHomeWithMixedAgents:
    def test_renders_with_mix_of_scored_and_nodata(self, auth_client, isolated_db):
        from src import storage

        agents = [
            {
                "agent_id": "100",
                "name": "Alice Smith",
                "email": "alice@x.com",
                "period": "2026-04",
                "speed_to_action": 180.0,
                "work_with_rate": 0.65,
                "csat": 0.92,
                "appt_set_rate": 0.72,
                "appt_met_rate": 0.78,
                "_raw": {},
            },
            # No-data agent: every metric explicitly None → overall_status ==
            # 'No Data'. (load_period only surfaces agents with at least one
            # agent_periods row, so the keys must be present even when None.)
            {
                "agent_id": "200",
                "name": "Zach Nodata",
                "email": "zach@x.com",
                "period": "2026-04",
                "speed_to_action": None,
                "work_with_rate": None,
                "csat": None,
                "appt_set_rate": None,
                "appt_met_rate": None,
                "_raw": {},
            },
        ]
        storage.save_period(agents, source="csv")

        r = auth_client.get("/home")
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        _assert_clean_html(body)
        # Active agent visible, no-data agent rendered (hidden by default
        # via JS, but the row + toggle button must be in the markup).
        assert "Alice Smith" in body
        assert "Zach Nodata" in body
        assert "Show 1 agent with no data" in body


# ─────────────────────────────────────────────────────────────────────────────
# Static / chrome templates — render via route, assert clean HTML
# ─────────────────────────────────────────────────────────────────────────────


class TestStaticAdminPages:
    """Smoke pass over every admin chrome template that lives behind a GET."""

    @pytest.mark.parametrize(
        "path,must_contain",
        [
            ("/login", "Anchor Metrics Admin"),
            ("/upload", "Upload monthly data"),
            ("/daily", "Daily Activity"),
        ],
    )
    def test_renders_clean(self, auth_client, path, must_contain):
        r = auth_client.get(path)
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        _assert_clean_html(body)
        assert must_contain in body

    def test_empty_state_when_no_periods(self, auth_client):
        # /home with zero ingested periods → admin/empty.html
        r = auth_client.get("/home")
        body = r.get_data(as_text=True)
        _assert_clean_html(body)
        assert "Welcome to Anchor Metrics" in body


# ─────────────────────────────────────────────────────────────────────────────
# Portal templates — login, verify_sent, dashboard (filled + empty)
# ─────────────────────────────────────────────────────────────────────────────


class TestPortalPages:
    def test_login_page_renders(self, client):
        body = client.get("/metrics/login").get_data(as_text=True)
        _assert_clean_html(body)
        assert "Email me a sign-in link" in body

    def test_verify_sent_page_renders(self, client, mocker):
        mocker.patch("src.agent_portal._send_magic_email")
        body = client.post("/metrics/login", data={"email": "unknown@x.com"}).get_data(as_text=True)
        _assert_clean_html(body)
        assert "Check your inbox" in body
        # Email echoed back into the template — confirms the data island reaches it.
        assert "unknown@x.com" in body

    def test_dashboard_renders_with_data(self, client, isolated_db, mocker):
        from src import storage

        storage.save_period(
            [
                {
                    "agent_id": "100",
                    "name": "Alice Smith",
                    "email": "alice@example.com",
                    "period": "2026-04",
                    "speed_to_action": 180.0,
                    "work_with_rate": 0.65,
                    "csat": 0.92,
                    "appt_set_rate": 0.72,
                    "appt_met_rate": 0.78,
                    "_raw": {},
                }
            ],
            source="csv",
        )

        captured: dict = {}

        def fake_send(_to, html):
            captured["url"] = html.split("/metrics/verify?token=")[1].split('"')[0]

        mocker.patch("src.agent_portal._send_magic_email", side_effect=fake_send)
        client.post("/metrics/login", data={"email": "alice@example.com"})
        client.get(f"/metrics/verify?token={captured['url']}")

        body = client.get("/metrics/dashboard").get_data(as_text=True)
        _assert_clean_html(body)
        assert "Alice Smith" in body
        # Chart.js data island present (not empty) and gauge SVGs embedded.
        assert 'id="trend-data"' in body
        assert "<svg" in body


# ─────────────────────────────────────────────────────────────────────────────
# Reachability — fail if a new template file lands without a smoke test path
# ─────────────────────────────────────────────────────────────────────────────


class TestEveryTemplateIsExercised:
    """Walk templates/ and assert each non-partial template is reachable."""

    EXEMPT = {
        # Partials — covered by including-template smoke tests above.
        "admin/_base.html",
        "admin/_daily_table.html",
        "admin/_draft_row.html",
        "admin/_pull_status.html",
        "portal/_base.html",
        # Dead code today (not referenced from any route or template). Listed
        # here so this test fails the day someone introduces a reference and
        # forgets to add a smoke test, rather than letting it ship untested.
        "admin/_metric_bars.html",
    }

    EXPECTED = {
        "admin/daily.html",
        "admin/empty.html",
        "admin/home.html",
        "admin/login.html",
        "admin/review.html",
        "admin/upload.html",
        "portal/dashboard.html",
        "portal/login.html",
        "portal/verify_sent.html",
        "email.html.j2",
        "deck.html.j2",
    }

    def test_every_template_under_templates_dir_is_classified(self):
        """Guardrail: a new template must be added to EXPECTED or EXEMPT
        (and have a smoke test) or this test fails."""
        templates_dir = Path(__file__).resolve().parent.parent / "templates"
        found = set()
        for p in templates_dir.rglob("*.html"):
            found.add(str(p.relative_to(templates_dir)))
        for p in templates_dir.rglob("*.j2"):
            found.add(str(p.relative_to(templates_dir)))

        unclassified = found - self.EXPECTED - self.EXEMPT
        assert not unclassified, (
            f"New template(s) found without a smoke test entry: {sorted(unclassified)}. "
            "Add to EXPECTED (and write a render test) or EXEMPT (with a reason)."
        )
        # And the EXPECTED list mustn't lie — every entry must still exist.
        missing = self.EXPECTED - found
        assert not missing, f"EXPECTED templates that no longer exist: {sorted(missing)}"
