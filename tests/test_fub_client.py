"""Tests for src/fub_client.py — date math, normalization, and HTTP retries."""

import base64
from datetime import date

import pytest
import requests
import responses

from src import fub_client
from src.fub_client import (
    _auth_header,
    _normalize,
    _null_record,
    _report_period,
    fetch_all_agents,
    fetch_zillow_preferred_report,
    mock_agents,
)


# ── _auth_header ──────────────────────────────────────────────────────────────

class TestAuthHeader:
    def test_basic_auth_with_key_as_username(self, mocker):
        mocker.patch("src.fub_client.FUB_API_KEY", "my-key")
        header = _auth_header()
        # Basic <base64 of "my-key:">
        assert header["Authorization"].startswith("Basic ")
        decoded = base64.b64decode(header["Authorization"][6:]).decode()
        assert decoded == "my-key:"


# ── _report_period ────────────────────────────────────────────────────────────

class TestReportPeriod:
    def test_auto_detects_prior_month_mid_year(self, mocker):
        mocker.patch("src.fub_client.OVERRIDE_REPORT_MONTH", None)

        class FakeDate(date):
            @classmethod
            def today(cls):
                return date(2026, 6, 15)

        mocker.patch("src.fub_client.date", FakeDate)
        start, end = _report_period()
        assert start == "2026-05-01"
        assert end == "2026-05-31"

    def test_january_rolls_back_to_december_prior_year(self, mocker):
        mocker.patch("src.fub_client.OVERRIDE_REPORT_MONTH", None)

        class FakeDate(date):
            @classmethod
            def today(cls):
                return date(2026, 1, 5)

        mocker.patch("src.fub_client.date", FakeDate)
        start, end = _report_period()
        assert start == "2025-12-01"
        assert end == "2025-12-31"

    def test_leap_year_february(self, mocker):
        mocker.patch("src.fub_client.OVERRIDE_REPORT_MONTH", "2024-02")
        start, end = _report_period()
        assert start == "2024-02-01"
        assert end == "2024-02-29"

    def test_non_leap_year_february(self, mocker):
        mocker.patch("src.fub_client.OVERRIDE_REPORT_MONTH", "2025-02")
        start, end = _report_period()
        assert start == "2025-02-01"
        assert end == "2025-02-28"

    def test_december_override_handles_year_rollover(self, mocker):
        mocker.patch("src.fub_client.OVERRIDE_REPORT_MONTH", "2026-12")
        start, end = _report_period()
        assert start == "2026-12-01"
        assert end == "2026-12-31"

    def test_override_takes_precedence_over_today(self, mocker):
        mocker.patch("src.fub_client.OVERRIDE_REPORT_MONTH", "2025-07")
        start, end = _report_period()
        assert start == "2025-07-01"
        assert end == "2025-07-31"


# ── _normalize ────────────────────────────────────────────────────────────────

class TestNormalize:
    @pytest.fixture
    def agent_cfg(self):
        return {
            "fub_agent_id": "abc",
            "name": "Alice",
            "email": "alice@example.com",
        }

    def test_uses_primary_field_names(self, agent_cfg):
        raw = {
            "predictedConversionRate": 0.04,
            "pickupRate": 0.85,
            "csatScore": 4.6,
            "zhlTransfers": 3,
        }
        out = _normalize(raw, agent_cfg, "March 2026", "2026-03-01", "2026-03-31")
        assert out["pCVR"] == 0.04
        assert out["pickup_rate"] == 0.85
        assert out["csat"] == 4.6
        assert out["zhl_transfers"] == 3

    def test_falls_back_to_alternate_field_names(self, agent_cfg):
        # Use only the second-choice field names for each metric.
        raw = {
            "pCVR": 0.04,
            "callPickupRate": 0.85,
            "csat": 4.6,
            "zillowHomeLoanTransfers": 3,
        }
        out = _normalize(raw, agent_cfg, "March 2026", "2026-03-01", "2026-03-31")
        assert out["pCVR"] == 0.04
        assert out["pickup_rate"] == 0.85
        assert out["csat"] == 4.6
        assert out["zhl_transfers"] == 3

    def test_missing_fields_become_none(self, agent_cfg):
        out = _normalize({}, agent_cfg, "March 2026", "2026-03-01", "2026-03-31")
        assert out["pCVR"] is None
        assert out["pickup_rate"] is None
        assert out["csat"] is None
        assert out["zhl_transfers"] is None

    def test_string_numbers_are_coerced(self, agent_cfg):
        raw = {
            "predictedConversionRate": "0.04",
            "pickupRate": "0.85",
            "csatScore": "4.6",
            "zhlTransfers": "3",
        }
        out = _normalize(raw, agent_cfg, "March 2026", "2026-03-01", "2026-03-31")
        assert out["pCVR"] == 0.04
        assert out["pickup_rate"] == 0.85
        assert out["zhl_transfers"] == 3

    def test_zero_value_is_not_treated_as_missing(self, agent_cfg):
        # Regression: previous `or` chain short-circuited on falsy 0.0 and fell
        # through to the next field, ultimately producing None for a legitimate
        # zero metric. Should now preserve 0.0 / 0 explicitly.
        raw = {
            "predictedConversionRate": 0.0,
            "pickupRate": 0.0,
            "csatScore": 0.0,
            "zhlTransfers": 0,
        }
        out = _normalize(raw, agent_cfg, "March 2026", "2026-03-01", "2026-03-31")
        assert out["pCVR"] == 0.0
        assert out["pickup_rate"] == 0.0
        assert out["csat"] == 0.0
        assert out["zhl_transfers"] == 0

    def test_zhl_transfers_accepts_float_string(self, agent_cfg):
        # `int("3.6")` raises; the coercer should route through float first.
        raw = {"zhlTransfers": "3.6"}
        out = _normalize(raw, agent_cfg, "March 2026", "2026-03-01", "2026-03-31")
        assert out["zhl_transfers"] == 3

    def test_unparseable_value_becomes_none_with_warning(self, agent_cfg, caplog):
        raw = {"predictedConversionRate": "not-a-number", "zhlTransfers": "abc"}
        with caplog.at_level("WARNING"):
            out = _normalize(raw, agent_cfg, "March 2026", "2026-03-01", "2026-03-31")
        assert out["pCVR"] is None
        assert out["zhl_transfers"] is None
        assert "Could not coerce" in caplog.text

    def test_empty_string_is_treated_as_missing(self, agent_cfg):
        raw = {"predictedConversionRate": "", "zhlTransfers": ""}
        out = _normalize(raw, agent_cfg, "March 2026", "2026-03-01", "2026-03-31")
        assert out["pCVR"] is None
        assert out["zhl_transfers"] is None

    def test_identity_and_period_are_passed_through(self, agent_cfg):
        out = _normalize({}, agent_cfg, "March 2026", "2026-03-01", "2026-03-31")
        assert out["agent_id"] == "abc"
        assert out["name"] == "Alice"
        assert out["email"] == "alice@example.com"
        assert out["period"] == "March 2026"
        assert out["start_date"] == "2026-03-01"
        assert out["end_date"] == "2026-03-31"

    def test_raw_response_is_preserved_for_debugging(self, agent_cfg):
        raw = {"predictedConversionRate": 0.04, "extra": "stuff"}
        out = _normalize(raw, agent_cfg, "March 2026", "2026-03-01", "2026-03-31")
        assert out["_raw"] == raw


# ── _null_record ──────────────────────────────────────────────────────────────

class TestNullRecord:
    def test_all_metrics_none_and_error_flag_set(self):
        cfg = {"fub_agent_id": "x", "name": "X", "email": "x@x"}
        out = _null_record(cfg, "March 2026", "2026-03-01", "2026-03-31")
        assert out["pCVR"] is None
        assert out["pickup_rate"] is None
        assert out["csat"] is None
        assert out["zhl_transfers"] is None
        assert out["_error"] is True
        assert out["agent_id"] == "x"


# ── HTTP layer (uses `responses` to stub requests) ────────────────────────────

class TestFetchZillowPreferred:
    BASE = "https://api.followupboss.com/v1"

    @responses.activate
    def test_uses_zillow_preferred_endpoint_first(self, mocker):
        mocker.patch("src.fub_client.FUB_API_KEY", "key")
        responses.add(
            responses.GET,
            f"{self.BASE}/reporting/zillow-preferred",
            json={"predictedConversionRate": 0.04},
            status=200,
        )
        data = fetch_zillow_preferred_report("agent-1", "2026-03-01", "2026-03-31")
        assert data == {"predictedConversionRate": 0.04}

    @responses.activate
    def test_falls_back_to_reporting_agent_on_404(self, mocker):
        mocker.patch("src.fub_client.FUB_API_KEY", "key")
        responses.add(
            responses.GET,
            f"{self.BASE}/reporting/zillow-preferred",
            json={"error": "not found"},
            status=404,
        )
        responses.add(
            responses.GET,
            f"{self.BASE}/reporting/agent",
            json={"pCVR": 0.03},
            status=200,
        )
        data = fetch_zillow_preferred_report("agent-1", "2026-03-01", "2026-03-31")
        assert data == {"pCVR": 0.03}

    @responses.activate
    def test_non_404_http_error_is_reraised(self, mocker):
        mocker.patch("src.fub_client.FUB_API_KEY", "key")
        # 500 should propagate, no fallback attempt.
        responses.add(
            responses.GET,
            f"{self.BASE}/reporting/zillow-preferred",
            json={"error": "server"},
            status=500,
        )
        # Force no retries — otherwise the client would retry the 500.
        mocker.patch("src.fub_client.FUB_MAX_RETRIES", 1)
        with pytest.raises(requests.HTTPError):
            fetch_zillow_preferred_report("agent-1", "2026-03-01", "2026-03-31")


class TestGetRetryBehavior:
    BASE = "https://api.followupboss.com/v1"

    @responses.activate
    def test_429_honors_retry_after_header(self, mocker):
        mocker.patch("src.fub_client.FUB_API_KEY", "key")
        sleep_spy = mocker.patch("src.fub_client.time.sleep")
        responses.add(
            responses.GET, f"{self.BASE}/reporting/zillow-preferred",
            status=429, headers={"Retry-After": "7"},
        )
        responses.add(
            responses.GET, f"{self.BASE}/reporting/zillow-preferred",
            json={"ok": True}, status=200,
        )
        data = fetch_zillow_preferred_report("a", "2026-03-01", "2026-03-31")
        assert data == {"ok": True}
        # Verify we slept the value of Retry-After.
        sleep_spy.assert_any_call(7)

    @responses.activate
    def test_retries_on_connection_error_then_succeeds(self, mocker):
        mocker.patch("src.fub_client.FUB_API_KEY", "key")
        mocker.patch("src.fub_client.time.sleep")  # avoid real backoff
        responses.add(
            responses.GET, f"{self.BASE}/reporting/zillow-preferred",
            body=requests.ConnectionError("boom"),
        )
        responses.add(
            responses.GET, f"{self.BASE}/reporting/zillow-preferred",
            json={"ok": True}, status=200,
        )
        data = fetch_zillow_preferred_report("a", "2026-03-01", "2026-03-31")
        assert data == {"ok": True}

    @responses.activate
    def test_raises_after_max_retries(self, mocker):
        mocker.patch("src.fub_client.FUB_API_KEY", "key")
        mocker.patch("src.fub_client.time.sleep")
        mocker.patch("src.fub_client.FUB_MAX_RETRIES", 2)
        for _ in range(2):
            responses.add(
                responses.GET, f"{self.BASE}/reporting/zillow-preferred",
                body=requests.ConnectionError("boom"),
            )
        with pytest.raises(requests.RequestException):
            fetch_zillow_preferred_report("a", "2026-03-01", "2026-03-31")

    @responses.activate
    def test_exhausting_429s_raises_runtime_error(self, mocker):
        # Repeated 429s should bound at FUB_MAX_RETRIES and raise — not loop
        # forever — even when no Retry-After header is present.
        mocker.patch("src.fub_client.FUB_API_KEY", "key")
        mocker.patch("src.fub_client.time.sleep")
        mocker.patch("src.fub_client.FUB_MAX_RETRIES", 3)
        for _ in range(3):
            responses.add(
                responses.GET, f"{self.BASE}/reporting/zillow-preferred",
                status=429,
            )
        with pytest.raises(RuntimeError, match="unreachable"):
            fetch_zillow_preferred_report("a", "2026-03-01", "2026-03-31")

    @responses.activate
    def test_429_backoff_grows_between_retries(self, mocker):
        # Without a Retry-After header, the delay should double across retries
        # (regression: previously stuck at the initial value).
        mocker.patch("src.fub_client.FUB_API_KEY", "key")
        sleep_spy = mocker.patch("src.fub_client.time.sleep")
        mocker.patch("src.fub_client.FUB_MAX_RETRIES", 3)
        responses.add(responses.GET, f"{self.BASE}/reporting/zillow-preferred", status=429)
        responses.add(responses.GET, f"{self.BASE}/reporting/zillow-preferred", status=429)
        responses.add(
            responses.GET, f"{self.BASE}/reporting/zillow-preferred",
            json={"ok": True}, status=200,
        )
        fetch_zillow_preferred_report("a", "2026-03-01", "2026-03-31")
        sleep_calls = [c.args[0] for c in sleep_spy.call_args_list]
        assert sleep_calls[0] == 2
        assert sleep_calls[1] == 4


# ── fetch_all_agents ──────────────────────────────────────────────────────────

class TestFetchAllAgents:
    def test_raises_when_api_key_missing(self, mocker):
        mocker.patch("src.fub_client.FUB_API_KEY", "")
        mocker.patch("src.fub_client.AGENTS", [{"name": "X", "email": "x@x", "fub_agent_id": "1"}])
        with pytest.raises(EnvironmentError, match="FUB_API_KEY"):
            fetch_all_agents()

    def test_empty_agent_roster_returns_empty_list(self, mocker, caplog):
        mocker.patch("src.fub_client.FUB_API_KEY", "key")
        mocker.patch("src.fub_client.AGENTS", [])
        with caplog.at_level("WARNING"):
            assert fetch_all_agents() == []
        assert "No agents configured" in caplog.text

    def test_per_agent_failure_yields_null_record(self, mocker):
        mocker.patch("src.fub_client.FUB_API_KEY", "key")
        mocker.patch("src.fub_client.AGENTS", [
            {"name": "Alice", "email": "a@a", "fub_agent_id": "1"},
            {"name": "Bob",   "email": "b@b", "fub_agent_id": "2"},
        ])
        mocker.patch("src.fub_client.OVERRIDE_REPORT_MONTH", "2026-03")

        def fake_fetch(agent_id, start, end):
            if agent_id == "1":
                return {"predictedConversionRate": 0.04, "pickupRate": 0.9,
                        "csatScore": 4.6, "zhlTransfers": 3}
            raise RuntimeError("simulated API failure")

        mocker.patch(
            "src.fub_client.fetch_zillow_preferred_report",
            side_effect=fake_fetch,
        )

        out = fetch_all_agents()
        assert len(out) == 2
        assert out[0]["pCVR"] == 0.04
        assert out[0].get("_error") is not True
        # Bob should be a null record, NOT a missing entry — report still runs.
        assert out[1]["name"] == "Bob"
        assert out[1]["pCVR"] is None
        assert out[1]["_error"] is True

    def test_period_label_uses_natural_month_name(self, mocker):
        mocker.patch("src.fub_client.FUB_API_KEY", "key")
        mocker.patch("src.fub_client.AGENTS", [
            {"name": "Alice", "email": "a@a", "fub_agent_id": "1"},
        ])
        mocker.patch("src.fub_client.OVERRIDE_REPORT_MONTH", "2026-03")
        mocker.patch(
            "src.fub_client.fetch_zillow_preferred_report",
            return_value={"predictedConversionRate": 0.04},
        )
        out = fetch_all_agents()
        assert out[0]["period"] == "March 2026"


# ── mock_agents ───────────────────────────────────────────────────────────────

class TestMockAgents:
    def test_returns_three_agents_with_required_keys(self):
        out = mock_agents()
        assert len(out) == 3
        required = {"agent_id", "name", "email", "period", "pCVR",
                    "pickup_rate", "csat", "zhl_transfers"}
        for record in out:
            assert required.issubset(record.keys())

    def test_default_period_label_present(self):
        out = mock_agents()
        assert out[0]["period"] == "March 2026"

    def test_period_can_be_overridden(self):
        out = mock_agents(period="December 2026")
        assert all(r["period"] == "December 2026" for r in out)
