"""Tests for src/fub_client.py — Follow Up Boss API integration."""

import base64
from datetime import date

import pytest
import requests
import responses

# ── _auth_header ──────────────────────────────────────────────────────────────


class TestAuthHeader:
    def test_basic_auth_with_key_as_username(self, monkeypatch):
        from src import fub_client

        monkeypatch.setattr(fub_client, "FUB_API_KEY", "test-api-key-123")

        header = fub_client._auth_header()

        assert "Authorization" in header
        scheme, token = header["Authorization"].split(" ", 1)
        assert scheme == "Basic"

        decoded = base64.b64decode(token).decode()
        assert decoded == "test-api-key-123:"

    def test_handles_special_characters_in_key(self, monkeypatch):
        from src import fub_client

        monkeypatch.setattr(fub_client, "FUB_API_KEY", "key!@#$%^&*()")

        header = fub_client._auth_header()
        decoded = base64.b64decode(header["Authorization"].split()[1]).decode()
        assert decoded == "key!@#$%^&*():"


# ── _report_period ────────────────────────────────────────────────────────────


class TestReportPeriod:
    def test_uses_override_month_when_set(self, monkeypatch):
        from src import fub_client

        monkeypatch.setattr(fub_client, "OVERRIDE_REPORT_MONTH", "2026-04")

        start, end = fub_client._report_period()

        assert start == "2026-04-01"
        assert end == "2026-04-30"

    def test_returns_prior_month_when_no_override(self, monkeypatch):
        """Without override, returns first..last day of the prior calendar month."""
        from src import fub_client

        monkeypatch.setattr(fub_client, "OVERRIDE_REPORT_MONTH", None)

        # Pin "today" by mocking date.today() in the module
        class _FakeDate(date):
            @classmethod
            def today(cls):
                return date(2026, 5, 15)

        monkeypatch.setattr(fub_client, "date", _FakeDate)

        start, end = fub_client._report_period()

        assert start == "2026-04-01"
        assert end == "2026-04-30"

    def test_december_boundary(self, monkeypatch):
        """Override month = December produces correct year-end range."""
        from src import fub_client

        monkeypatch.setattr(fub_client, "OVERRIDE_REPORT_MONTH", "2025-12")

        start, end = fub_client._report_period()

        assert start == "2025-12-01"
        assert end == "2025-12-31"

    def test_january_returns_prior_december(self, monkeypatch):
        """When today is January, prior month is December of the previous year."""
        from src import fub_client

        monkeypatch.setattr(fub_client, "OVERRIDE_REPORT_MONTH", None)

        class _FakeDate(date):
            @classmethod
            def today(cls):
                return date(2026, 1, 5)

        monkeypatch.setattr(fub_client, "date", _FakeDate)

        start, end = fub_client._report_period()

        assert start == "2025-12-01"
        assert end == "2025-12-31"


# ── _get (HTTP retry logic) ───────────────────────────────────────────────────


class TestGetWithRetry:
    @responses.activate
    def test_happy_path_returns_json(self, monkeypatch):
        from src import fub_client

        monkeypatch.setattr(fub_client, "FUB_API_KEY", "test-key")
        monkeypatch.setattr(fub_client, "FUB_BASE_URL", "https://api.example.com")

        responses.get(
            "https://api.example.com/people",
            json={"items": [{"id": 1}]},
            status=200,
        )

        result = fub_client._get("/people")
        assert result == {"items": [{"id": 1}]}

    @responses.activate
    def test_includes_auth_header(self, monkeypatch):
        from src import fub_client

        monkeypatch.setattr(fub_client, "FUB_API_KEY", "secret-key")
        monkeypatch.setattr(fub_client, "FUB_BASE_URL", "https://api.example.com")

        responses.get(
            "https://api.example.com/x",
            json={},
            status=200,
        )

        fub_client._get("/x")

        sent_auth = responses.calls[0].request.headers["Authorization"]
        decoded = base64.b64decode(sent_auth.split()[1]).decode()
        assert decoded == "secret-key:"

    @responses.activate
    def test_retries_on_429(self, monkeypatch, mocker):
        from src import fub_client

        monkeypatch.setattr(fub_client, "FUB_API_KEY", "test-key")
        monkeypatch.setattr(fub_client, "FUB_BASE_URL", "https://api.example.com")
        monkeypatch.setattr(fub_client, "FUB_MAX_RETRIES", 3)

        # Stub time.sleep to keep test fast
        mocker.patch.object(fub_client.time, "sleep")

        responses.get(
            "https://api.example.com/x",
            status=429,
            headers={"Retry-After": "1"},
        )
        responses.get(
            "https://api.example.com/x",
            json={"ok": True},
            status=200,
        )

        result = fub_client._get("/x")
        assert result == {"ok": True}
        assert len(responses.calls) == 2

    @responses.activate
    def test_retries_on_transient_failure(self, monkeypatch, mocker):
        from src import fub_client

        monkeypatch.setattr(fub_client, "FUB_API_KEY", "test-key")
        monkeypatch.setattr(fub_client, "FUB_BASE_URL", "https://api.example.com")
        monkeypatch.setattr(fub_client, "FUB_MAX_RETRIES", 3)
        mocker.patch.object(fub_client.time, "sleep")

        responses.get(
            "https://api.example.com/x",
            body=requests.ConnectionError("transient"),
        )
        responses.get(
            "https://api.example.com/x",
            json={"ok": True},
            status=200,
        )

        result = fub_client._get("/x")
        assert result == {"ok": True}

    @responses.activate
    def test_raises_after_max_retries(self, monkeypatch, mocker):
        from src import fub_client

        monkeypatch.setattr(fub_client, "FUB_API_KEY", "test-key")
        monkeypatch.setattr(fub_client, "FUB_BASE_URL", "https://api.example.com")
        monkeypatch.setattr(fub_client, "FUB_MAX_RETRIES", 2)
        mocker.patch.object(fub_client.time, "sleep")

        responses.get(
            "https://api.example.com/x",
            body=requests.ConnectionError("permanent"),
        )
        responses.get(
            "https://api.example.com/x",
            body=requests.ConnectionError("permanent"),
        )

        with pytest.raises(requests.RequestException):
            fub_client._get("/x")

    @responses.activate
    def test_5xx_raises_via_raise_for_status(self, monkeypatch, mocker):
        from src import fub_client

        monkeypatch.setattr(fub_client, "FUB_API_KEY", "test-key")
        monkeypatch.setattr(fub_client, "FUB_BASE_URL", "https://api.example.com")
        monkeypatch.setattr(fub_client, "FUB_MAX_RETRIES", 1)
        mocker.patch.object(fub_client.time, "sleep")

        responses.get("https://api.example.com/x", status=500)

        with pytest.raises(requests.HTTPError):
            fub_client._get("/x")


# ── fetch_zillow_preferred_report ─────────────────────────────────────────────


class TestFetchZillowPreferredReport:
    @responses.activate
    def test_uses_zillow_preferred_endpoint_when_available(self, monkeypatch):
        from src import fub_client

        monkeypatch.setattr(fub_client, "FUB_API_KEY", "test-key")
        monkeypatch.setattr(fub_client, "FUB_BASE_URL", "https://api.example.com")

        responses.get(
            "https://api.example.com/reporting/zillow-preferred",
            json={"pCVR": 0.05},
            status=200,
        )

        result = fub_client.fetch_zillow_preferred_report("100", "2026-04-01", "2026-04-30")
        assert result == {"pCVR": 0.05}

    @responses.activate
    def test_falls_back_to_agent_endpoint_on_404(self, monkeypatch):
        from src import fub_client

        monkeypatch.setattr(fub_client, "FUB_API_KEY", "test-key")
        monkeypatch.setattr(fub_client, "FUB_BASE_URL", "https://api.example.com")

        responses.get(
            "https://api.example.com/reporting/zillow-preferred",
            json={"error": "not found"},
            status=404,
        )
        responses.get(
            "https://api.example.com/reporting/agent",
            json={"pickupRate": 0.8},
            status=200,
        )

        result = fub_client.fetch_zillow_preferred_report("100", "2026-04-01", "2026-04-30")
        assert result == {"pickupRate": 0.8}


# ── fetch_users (auto-discovery) ──────────────────────────────────────────────


class TestFetchUsers:
    def test_raises_when_api_key_missing(self, monkeypatch):
        from src import fub_client

        monkeypatch.setattr(fub_client, "FUB_API_KEY", "")

        with pytest.raises(OSError, match="FUB_API_KEY"):
            fub_client.fetch_users()

    @responses.activate
    def test_keeps_only_agents_and_brokers(self, monkeypatch):
        from src import fub_client

        monkeypatch.setattr(fub_client, "FUB_API_KEY", "test-key")
        monkeypatch.setattr(fub_client, "FUB_BASE_URL", "https://api.example.com")

        responses.get(
            "https://api.example.com/users",
            json={
                "_metadata": {"total": 4, "next": None},
                "users": [
                    {"id": 1, "name": "Alice", "email": "a@x.com", "role": "Agent"},
                    {"id": 2, "name": "Bob", "email": "b@x.com", "role": "Broker"},
                    {"id": 3, "name": "Carol", "email": "c@x.com", "role": "Lender"},
                    {"id": 4, "name": "Dan", "email": "d@x.com", "role": "Admin"},
                ],
            },
            status=200,
        )

        roster = fub_client.fetch_users()

        assert len(roster) == 2
        assert {r["name"] for r in roster} == {"Alice", "Bob"}
        assert all("fub_agent_id" in r for r in roster)

    @responses.activate
    def test_skips_inactive_and_missing_fields(self, monkeypatch):
        from src import fub_client

        monkeypatch.setattr(fub_client, "FUB_API_KEY", "test-key")
        monkeypatch.setattr(fub_client, "FUB_BASE_URL", "https://api.example.com")

        responses.get(
            "https://api.example.com/users",
            json={
                "_metadata": {"total": 4, "next": None},
                "users": [
                    {"id": 1, "name": "Alice", "email": "a@x.com", "role": "Agent"},
                    {"id": 2, "name": "Bob", "email": "b@x.com", "role": "Agent",
                     "deleted": True},
                    {"id": 3, "name": "Carol", "email": "c@x.com", "role": "Agent",
                     "status": "inactive"},
                    {"id": 4, "name": "", "email": "d@x.com", "role": "Agent"},
                ],
            },
            status=200,
        )

        roster = fub_client.fetch_users()

        assert len(roster) == 1
        assert roster[0]["name"] == "Alice"

    @responses.activate
    def test_paginates_via_next_token(self, monkeypatch):
        from src import fub_client

        monkeypatch.setattr(fub_client, "FUB_API_KEY", "test-key")
        monkeypatch.setattr(fub_client, "FUB_BASE_URL", "https://api.example.com")

        responses.get(
            "https://api.example.com/users",
            json={
                "_metadata": {"total": 2, "next": "page2token"},
                "users": [
                    {"id": 1, "name": "Alice", "email": "a@x.com", "role": "Agent"},
                ],
            },
            status=200,
        )
        responses.get(
            "https://api.example.com/users",
            json={
                "_metadata": {"total": 2, "next": None},
                "users": [
                    {"id": 2, "name": "Bob", "email": "b@x.com", "role": "Agent"},
                ],
            },
            status=200,
        )

        roster = fub_client.fetch_users()

        assert len(roster) == 2
        assert {r["fub_agent_id"] for r in roster} == {"1", "2"}


# ── fetch_all_agents ──────────────────────────────────────────────────────────


class TestFetchAllAgents:
    def test_raises_oserror_when_api_key_missing(self, monkeypatch):
        from src import fub_client

        monkeypatch.setattr(fub_client, "FUB_API_KEY", "")

        with pytest.raises(OSError, match="FUB_API_KEY"):
            fub_client.fetch_all_agents()

    def test_falls_back_to_user_discovery_when_no_agents(self, monkeypatch, mocker):
        from src import fub_client

        monkeypatch.setattr(fub_client, "FUB_API_KEY", "test-key")
        monkeypatch.setattr(fub_client, "AGENTS", [])

        mock_fetch_users = mocker.patch.object(
            fub_client, "fetch_users", return_value=[]
        )

        result = fub_client.fetch_all_agents()
        mock_fetch_users.assert_called_once()
        assert result == []

    @responses.activate
    def test_normalizes_each_agent(self, monkeypatch):
        from src import fub_client

        monkeypatch.setattr(fub_client, "FUB_API_KEY", "test-key")
        monkeypatch.setattr(fub_client, "FUB_BASE_URL", "https://api.example.com")
        monkeypatch.setattr(fub_client, "OVERRIDE_REPORT_MONTH", "2026-04")
        monkeypatch.setattr(
            fub_client,
            "AGENTS",
            [
                {"name": "Alice", "email": "alice@x.com", "fub_agent_id": "100"},
            ],
        )

        responses.get(
            "https://api.example.com/reporting/zillow-preferred",
            json={
                "predictedConversionRate": 0.045,
                "pickupRate": 0.88,
                "csatScore": 4.6,
                "zhlTransfers": 3,
            },
            status=200,
        )

        result = fub_client.fetch_all_agents()

        assert len(result) == 1
        agent = result[0]
        assert agent["agent_id"] == "100"
        assert agent["name"] == "Alice"
        assert agent["pCVR"] == 0.045
        assert agent["pickup_rate"] == 0.88
        assert agent["csat"] == 4.6
        assert agent["zhl_transfers"] == 3
        assert agent["period"] == "April 2026"

    @responses.activate
    def test_returns_null_record_on_agent_failure(self, monkeypatch, mocker):
        """If one agent's fetch raises, the loop continues with a null record."""
        from src import fub_client

        monkeypatch.setattr(fub_client, "FUB_API_KEY", "test-key")
        monkeypatch.setattr(fub_client, "FUB_BASE_URL", "https://api.example.com")
        monkeypatch.setattr(fub_client, "OVERRIDE_REPORT_MONTH", "2026-04")
        monkeypatch.setattr(fub_client, "FUB_MAX_RETRIES", 1)
        monkeypatch.setattr(
            fub_client,
            "AGENTS",
            [
                {"name": "Bob", "email": "bob@x.com", "fub_agent_id": "200"},
            ],
        )
        mocker.patch.object(fub_client.time, "sleep")

        # Both endpoints 500 — should raise after retries → caught → null record
        responses.get(
            "https://api.example.com/reporting/zillow-preferred",
            status=500,
        )

        result = fub_client.fetch_all_agents()

        assert len(result) == 1
        assert result[0]["agent_id"] == "200"
        assert result[0]["pCVR"] is None
        assert result[0]["_error"] is True


# ── _normalize ────────────────────────────────────────────────────────────────


class TestNormalize:
    def test_maps_canonical_field_names(self):
        from src.fub_client import _normalize

        raw = {
            "predictedConversionRate": 0.05,
            "pickupRate": 0.9,
            "csatScore": 4.5,
            "zhlTransfers": 7,
        }
        cfg = {"fub_agent_id": "100", "name": "Alice", "email": "alice@x.com"}

        out = _normalize(raw, cfg, "April 2026", "2026-04-01", "2026-04-30")

        assert out["pCVR"] == 0.05
        assert out["pickup_rate"] == 0.9
        assert out["csat"] == 4.5
        assert out["zhl_transfers"] == 7
        assert out["_raw"] == raw

    def test_falls_through_alternate_names(self):
        from src.fub_client import _normalize

        raw = {
            "pCVR": 0.04,
            "callPickupRate": 0.85,
            "csat": 4.3,
            "zillowHomeLoanTransfers": 2,
        }
        cfg = {"fub_agent_id": "100", "name": "Alice", "email": "alice@x.com"}

        out = _normalize(raw, cfg, "April 2026", "2026-04-01", "2026-04-30")

        assert out["pCVR"] == 0.04
        assert out["pickup_rate"] == 0.85
        assert out["csat"] == 4.3
        assert out["zhl_transfers"] == 2

    def test_preserves_none_for_missing_metrics(self):
        from src.fub_client import _normalize

        raw = {}
        cfg = {"fub_agent_id": "100", "name": "Alice", "email": "alice@x.com"}

        out = _normalize(raw, cfg, "April 2026", "2026-04-01", "2026-04-30")

        assert out["pCVR"] is None
        assert out["pickup_rate"] is None
        assert out["csat"] is None
        assert out["zhl_transfers"] is None


# ── _null_record ──────────────────────────────────────────────────────────────


class TestNullRecord:
    def test_returns_placeholder_with_error_flag(self):
        from src.fub_client import _null_record

        cfg = {"fub_agent_id": "100", "name": "Alice", "email": "alice@x.com"}
        out = _null_record(cfg, "April 2026", "2026-04-01", "2026-04-30")

        assert out["agent_id"] == "100"
        assert out["pCVR"] is None
        assert out["_error"] is True
        assert out["_raw"] == {}


# ── mock_agents ───────────────────────────────────────────────────────────────


class TestMockAgents:
    def test_returns_three_agents_with_default_period(self):
        from src.fub_client import mock_agents

        agents = mock_agents()

        assert len(agents) == 3
        assert all(a["period"] == "March 2026" for a in agents)
        assert all("agent_id" in a and a["agent_id"].startswith("mock-") for a in agents)
        assert all("email" in a and "@" in a["email"] for a in agents)

    def test_period_override(self):
        from src.fub_client import mock_agents

        agents = mock_agents(period="January 2027")

        assert all(a["period"] == "January 2027" for a in agents)
