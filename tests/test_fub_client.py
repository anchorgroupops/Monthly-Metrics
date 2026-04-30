"""
Tests for src/fub_client.py.

Focus areas:
- _report_period: date arithmetic including year-boundary rollover and override
- _normalize: field name fallback chains for different FUB API response shapes
- _null_record: error placeholder structure
- mock_agents: data shape and custom period
- _auth_header: Basic auth token construction
"""

import base64
import datetime as dt
from unittest.mock import patch

import pytest

from src.fub_client import (
    _auth_header,
    _normalize,
    _null_record,
    _report_period,
    mock_agents,
)


# ── _report_period ────────────────────────────────────────────────────────────

class TestReportPeriod:
    """
    _report_period() returns (start_date, end_date) for the prior calendar month.
    Mocking is necessary because the real date.today() would make the tests
    non-deterministic and break on the 1st of each month.
    """

    def _patch_today(self, fake_today: dt.date):
        """Context manager that replaces date.today() inside fub_client."""
        real_date = dt.date

        class _FakeDate(dt.date):
            @classmethod
            def today(cls):
                return fake_today

        return patch("src.fub_client.date", _FakeDate)

    def test_mid_year_gives_previous_month(self):
        with self._patch_today(dt.date(2026, 4, 15)):
            start, end = _report_period()
        assert start == "2026-03-01"
        assert end == "2026-03-31"

    def test_january_gives_december_of_prior_year(self):
        with self._patch_today(dt.date(2026, 1, 15)):
            start, end = _report_period()
        assert start == "2025-12-01"
        assert end == "2025-12-31"

    def test_march_gives_february(self):
        # February end date — should be 28 in non-leap year, 29 in leap year
        with self._patch_today(dt.date(2026, 3, 1)):
            start, end = _report_period()
        assert start == "2026-02-01"
        assert end == "2026-02-28"   # 2026 is not a leap year

    def test_march_gives_february_29_in_leap_year(self):
        with self._patch_today(dt.date(2025, 3, 1)):
            start, end = _report_period()
        # 2025 is not a leap year either, but 2024 was
        # Going back from 2025-03 → 2025-02-28
        assert start == "2025-02-01"
        assert end == "2025-02-28"

    def test_override_month_is_respected(self):
        with patch("src.fub_client.OVERRIDE_REPORT_MONTH", "2025-06"):
            start, end = _report_period()
        assert start == "2025-06-01"
        assert end == "2025-06-30"

    def test_override_december(self):
        with patch("src.fub_client.OVERRIDE_REPORT_MONTH", "2024-12"):
            start, end = _report_period()
        assert start == "2024-12-01"
        assert end == "2024-12-31"

    def test_returns_iso_format_strings(self):
        with patch("src.fub_client.OVERRIDE_REPORT_MONTH", "2026-03"):
            start, end = _report_period()
        # Validate ISO format: YYYY-MM-DD
        dt.date.fromisoformat(start)
        dt.date.fromisoformat(end)

    def test_start_is_always_first_of_month(self):
        with patch("src.fub_client.OVERRIDE_REPORT_MONTH", "2026-03"):
            start, _ = _report_period()
        assert start.endswith("-01")


# ── _normalize ────────────────────────────────────────────────────────────────

class TestNormalize:
    """
    The FUB API may return metrics under different field names depending on the
    account integration. _normalize() must handle all documented fallbacks.
    """

    @pytest.fixture
    def agent_cfg(self):
        return {"fub_agent_id": "123", "name": "Jane Smith", "email": "jane@test.com"}

    def _call(self, raw, agent_cfg):
        return _normalize(raw, agent_cfg, "March 2026", "2026-03-01", "2026-03-31")

    # Primary field names
    def test_primary_field_names(self, agent_cfg):
        raw = {
            "predictedConversionRate": 0.038,
            "pickupRate": 0.91,
            "csatScore": 4.7,
            "zhlTransfers": 5,
        }
        result = self._call(raw, agent_cfg)
        assert result["pCVR"] == pytest.approx(0.038)
        assert result["pickup_rate"] == pytest.approx(0.91)
        assert result["csat"] == pytest.approx(4.7)
        assert result["zhl_transfers"] == 5

    # Secondary fallback field names
    def test_secondary_field_names(self, agent_cfg):
        raw = {
            "pCVR": 0.025,
            "callPickupRate": 0.80,
            "csat": 4.2,
            "zillowHomeLoanTransfers": 3,
        }
        result = self._call(raw, agent_cfg)
        assert result["pCVR"] == pytest.approx(0.025)
        assert result["pickup_rate"] == pytest.approx(0.80)
        assert result["csat"] == pytest.approx(4.2)
        assert result["zhl_transfers"] == 3

    # Tertiary fallback field names
    def test_tertiary_field_names(self, agent_cfg):
        raw = {
            "conversionRatePredicted": 0.031,
            "answerRate": 0.77,
            "satisfactionScore": 4.3,
            "transferCount": 2,
        }
        result = self._call(raw, agent_cfg)
        assert result["pCVR"] == pytest.approx(0.031)
        assert result["pickup_rate"] == pytest.approx(0.77)
        assert result["csat"] == pytest.approx(4.3)
        assert result["zhl_transfers"] == 2

    def test_missing_fields_return_none(self, agent_cfg):
        result = self._call({}, agent_cfg)
        assert result["pCVR"] is None
        assert result["pickup_rate"] is None
        assert result["csat"] is None
        assert result["zhl_transfers"] is None

    def test_identity_fields_from_agent_cfg(self, agent_cfg):
        result = self._call({}, agent_cfg)
        assert result["agent_id"] == "123"
        assert result["name"] == "Jane Smith"
        assert result["email"] == "jane@test.com"

    def test_period_and_dates_set_correctly(self, agent_cfg):
        result = self._call({}, agent_cfg)
        assert result["period"] == "March 2026"
        assert result["start_date"] == "2026-03-01"
        assert result["end_date"] == "2026-03-31"

    def test_raw_response_preserved(self, agent_cfg):
        raw = {"predictedConversionRate": 0.038}
        result = self._call(raw, agent_cfg)
        assert result["_raw"] is raw

    def test_zhl_transfers_cast_to_int(self, agent_cfg):
        raw = {"zhlTransfers": 3.0}
        result = self._call(raw, agent_cfg)
        assert result["zhl_transfers"] == 3
        assert isinstance(result["zhl_transfers"], int)

    def test_float_fields_cast_to_float(self, agent_cfg):
        raw = {"predictedConversionRate": "0.038"}  # string from API
        result = self._call(raw, agent_cfg)
        assert result["pCVR"] == pytest.approx(0.038)
        assert isinstance(result["pCVR"], float)


# ── _null_record ──────────────────────────────────────────────────────────────

class TestNullRecord:

    @pytest.fixture
    def agent_cfg(self):
        return {"fub_agent_id": "123", "name": "Jane Smith", "email": "jane@test.com"}

    def test_all_metrics_are_none(self, agent_cfg):
        result = _null_record(agent_cfg, "March 2026", "2026-03-01", "2026-03-31")
        assert result["pCVR"] is None
        assert result["pickup_rate"] is None
        assert result["csat"] is None
        assert result["zhl_transfers"] is None

    def test_error_flag_is_true(self, agent_cfg):
        result = _null_record(agent_cfg, "March 2026", "2026-03-01", "2026-03-31")
        assert result["_error"] is True

    def test_identity_fields_preserved(self, agent_cfg):
        result = _null_record(agent_cfg, "March 2026", "2026-03-01", "2026-03-31")
        assert result["agent_id"] == "123"
        assert result["name"] == "Jane Smith"
        assert result["email"] == "jane@test.com"

    def test_raw_is_empty_dict(self, agent_cfg):
        result = _null_record(agent_cfg, "March 2026", "2026-03-01", "2026-03-31")
        assert result["_raw"] == {}


# ── mock_agents ───────────────────────────────────────────────────────────────

class TestMockAgents:

    def test_returns_three_agents_by_default(self):
        assert len(mock_agents()) == 3

    def test_all_required_keys_present(self):
        required = {"agent_id", "name", "email", "period",
                    "start_date", "end_date", "pCVR", "pickup_rate",
                    "csat", "zhl_transfers", "_raw"}
        for agent in mock_agents():
            assert required.issubset(agent.keys())

    def test_default_period_label(self):
        agents = mock_agents()
        assert all(a["period"] == "March 2026" for a in agents)

    def test_custom_period_applied_to_all(self):
        agents = mock_agents(period="April 2026")
        assert all(a["period"] == "April 2026" for a in agents)

    def test_metric_values_are_numeric(self):
        for agent in mock_agents():
            assert isinstance(agent["pCVR"], float)
            assert isinstance(agent["pickup_rate"], float)
            assert isinstance(agent["csat"], float)
            assert isinstance(agent["zhl_transfers"], int)

    def test_metric_values_in_realistic_ranges(self):
        for agent in mock_agents():
            assert 0 < agent["pCVR"] < 1
            assert 0 < agent["pickup_rate"] <= 1
            assert 1 <= agent["csat"] <= 5
            assert agent["zhl_transfers"] >= 0

    def test_agents_have_unique_ids(self):
        ids = [a["agent_id"] for a in mock_agents()]
        assert len(ids) == len(set(ids))


# ── _auth_header ──────────────────────────────────────────────────────────────

class TestAuthHeader:

    def test_produces_basic_auth_header(self):
        with patch("src.fub_client.FUB_API_KEY", "test-key-123"):
            header = _auth_header()
        expected_token = base64.b64encode(b"test-key-123:").decode()
        assert header["Authorization"] == f"Basic {expected_token}"

    def test_empty_api_key_still_encodes(self):
        with patch("src.fub_client.FUB_API_KEY", ""):
            header = _auth_header()
        expected_token = base64.b64encode(b":").decode()
        assert header["Authorization"] == f"Basic {expected_token}"

    def test_colon_appended_as_per_fub_spec(self):
        with patch("src.fub_client.FUB_API_KEY", "mykey"):
            header = _auth_header()
        # Decoded token must be "mykey:" (username only, no password)
        raw = base64.b64decode(header["Authorization"].split(" ")[1])
        assert raw == b"mykey:"
