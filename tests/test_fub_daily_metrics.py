"""Tests for src/fub_daily_metrics.py — daily activity metric calculations."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
import responses

from config import settings
from src import fub_daily_metrics as fdm

_FIXED_TODAY = date(2026, 5, 15)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _person(
    *,
    person_id: int = 1,
    source: str = "Zillow Preferred",
    source_id: int | None = 14,
    created: str = "2026-05-10T14:00:00Z",
    contacted: int = 1,
    first_call: str | None = "2026-05-10T14:02:00Z",
    last_sent_text: str | None = None,
    last_sent_email: str | None = None,
    calls_outgoing: int = 0,
    texts_sent: int = 0,
    emails_sent: int = 0,
    calls_duration: int = 0,
    stage_id: int | None = 28,
) -> dict:
    """Build a synthetic FUB person record with realistic field names."""
    p: dict = {
        "id": person_id,
        "source": source,
        "created": created,
        "contacted": contacted,
        "firstCall": first_call,
        "lastSentText": last_sent_text,
        "lastSentEmail": last_sent_email,
        "callsOutgoing": calls_outgoing,
        "textsSent": texts_sent,
        "emailsSent": emails_sent,
        "callsDuration": calls_duration,
        "stageId": stage_id,
    }
    if source_id is not None:
        p["sourceId"] = source_id
    return p


# ── month_start ──────────────────────────────────────────────────────────────


class TestMonthStart:
    def test_returns_first_of_current_month(self):
        assert fdm.month_start(date(2026, 5, 15)) == "2026-05-01"

    def test_already_first(self):
        assert fdm.month_start(date(2026, 1, 1)) == "2026-01-01"


# ── _parse_ts ────────────────────────────────────────────────────────────────


class TestParseTs:
    def test_iso_with_z(self):
        ts = fdm._parse_ts("2026-05-10T14:00:00Z")
        assert ts == datetime(2026, 5, 10, 14, 0, 0, tzinfo=UTC)

    def test_iso_with_offset(self):
        ts = fdm._parse_ts("2026-05-10T14:00:00+00:00")
        assert ts == datetime(2026, 5, 10, 14, 0, 0, tzinfo=UTC)

    def test_none(self):
        assert fdm._parse_ts(None) is None

    def test_zero_int(self):
        assert fdm._parse_ts(0) is None

    def test_empty_string(self):
        assert fdm._parse_ts("") is None
        assert fdm._parse_ts("   ") is None

    def test_garbage_string(self):
        assert fdm._parse_ts("not a date") is None

    def test_epoch_seconds(self):
        ts = fdm._parse_ts(1747920000)
        assert ts is not None
        assert ts.tzinfo is UTC


# ── is_zillow_preferred ──────────────────────────────────────────────────────


class TestZillowFilter:
    def test_matches_by_source_id(self):
        assert fdm.is_zillow_preferred({"sourceId": 14, "source": "anything"})

    def test_matches_by_source_name_case_insensitive(self):
        assert fdm.is_zillow_preferred({"source": "Zillow Preferred"})
        assert fdm.is_zillow_preferred({"source": "zillow preferred"})

    def test_matches_zillow_flex(self):
        assert fdm.is_zillow_preferred({"source": "Zillow Flex"})

    def test_rejects_other_sources(self):
        assert not fdm.is_zillow_preferred({"source": "Realtor.com"})
        assert not fdm.is_zillow_preferred({"sourceId": 7, "source": "Web Form"})

    def test_rejects_empty(self):
        assert not fdm.is_zillow_preferred({})


# ── _response_time_seconds ───────────────────────────────────────────────────


class TestResponseTime:
    def test_uses_first_call_when_only_call_present(self):
        p = _person(
            created="2026-05-10T14:00:00Z",
            first_call="2026-05-10T14:05:00Z",
            last_sent_text=None,
            last_sent_email=None,
        )
        assert fdm._response_time_seconds(p) == 300.0

    def test_picks_earliest_of_three_signals(self):
        p = _person(
            created="2026-05-10T14:00:00Z",
            first_call="2026-05-10T14:10:00Z",
            last_sent_text="2026-05-10T14:02:00Z",  # earliest
            last_sent_email="2026-05-10T14:05:00Z",
        )
        assert fdm._response_time_seconds(p) == 120.0

    def test_returns_none_when_no_contact(self):
        p = _person(first_call=None, last_sent_text=None, last_sent_email=None)
        assert fdm._response_time_seconds(p) is None

    def test_returns_none_when_created_missing(self):
        p = _person(created=None, first_call="2026-05-10T14:05:00Z")
        assert fdm._response_time_seconds(p) is None

    def test_ignores_signals_before_created(self):
        """Stale firstCall from before this lead was even assigned should be skipped."""
        p = _person(
            created="2026-05-10T14:00:00Z",
            first_call="2026-05-09T10:00:00Z",  # before created
            last_sent_text="2026-05-10T14:03:00Z",
            last_sent_email=None,
        )
        assert fdm._response_time_seconds(p) == 180.0

    def test_negative_clamped_to_zero(self):
        """Even if everything is stale, we never return a negative number."""
        p = _person(
            created="2026-05-10T14:00:00Z",
            first_call="2026-05-09T10:00:00Z",
            last_sent_text=None,
            last_sent_email=None,
        )
        assert fdm._response_time_seconds(p) is None


# ── calculate_agent_metrics ──────────────────────────────────────────────────


class TestCalculateAgentMetrics:
    def test_empty_list_returns_zero_metrics(self):
        m = fdm.calculate_agent_metrics([])
        assert m["total_zillow_leads"] == 0
        assert m["activity_points"] == 0
        assert m["response_time_seconds"] is None
        assert m["contact_rate"] is None
        assert m["pickup_rate"] is None
        # Counts are 0, not None
        assert m["call_volume"] == 0
        assert m["texts_sent"] == 0
        assert m["emails_sent"] == 0

    def test_contact_rate(self):
        people = [
            _person(person_id=1, contacted=1),
            _person(person_id=2, contacted=1),
            _person(person_id=3, contacted=0, first_call=None),
            _person(person_id=4, contacted=0, first_call=None),
        ]
        m = fdm.calculate_agent_metrics(people)
        assert m["contact_rate"] == 0.5

    def test_appointment_rate_uses_stage_29_or_30(self):
        people = [
            _person(person_id=1, stage_id=29),
            _person(person_id=2, stage_id=30),
            _person(person_id=3, stage_id=28),
            _person(person_id=4, stage_id=26),
        ]
        m = fdm.calculate_agent_metrics(people)
        assert m["appointment_rate"] == 0.5
        assert m["appointments_set"] == 2

    def test_lead_acceptance_anything_past_new(self):
        people = [
            _person(person_id=1, stage_id=26),  # New, not accepted
            _person(person_id=2, stage_id=27),  # accepted
            _person(person_id=3, stage_id=28),  # accepted
            _person(person_id=4, stage_id=29),  # accepted
        ]
        m = fdm.calculate_agent_metrics(people)
        assert m["lead_acceptance_rate"] == 0.75

    def test_pickup_rate_only_counts_called_leads(self):
        """Leads with no first_call are excluded from the pickup denominator."""
        people = [
            _person(
                person_id=1, first_call="2026-05-10T14:01:00Z", calls_duration=180
            ),  # picked up
            _person(person_id=2, first_call="2026-05-10T14:01:00Z", calls_duration=5),  # voicemail
            _person(person_id=3, first_call=None, calls_duration=0),  # never called → excluded
        ]
        m = fdm.calculate_agent_metrics(people)
        # 1 of 2 attempts picked up
        assert m["pickup_rate"] == 0.5

    def test_pickup_rate_none_when_no_calls_attempted(self):
        people = [_person(person_id=1, first_call=None, calls_duration=0)]
        m = fdm.calculate_agent_metrics(people)
        assert m["pickup_rate"] is None

    def test_activity_points_weighted_correctly(self):
        people = [
            _person(
                person_id=1,
                stage_id=29,  # +500 (appt set)
                calls_duration=200,  # +100 (conversation 2+ min)
                calls_outgoing=3,  # +30
                texts_sent=10,  # +20
                emails_sent=5,  # +5
                first_call="2026-05-10T14:01:00Z",
            )
        ]
        m = fdm.calculate_agent_metrics(people)
        assert m["activity_points"] == 500 + 100 + 30 + 20 + 5
        assert m["appointments_set"] == 1
        assert m["conversations_2min"] == 1

    def test_response_time_is_average_of_responded_leads(self):
        people = [
            _person(
                person_id=1,
                created="2026-05-10T14:00:00Z",
                first_call="2026-05-10T14:01:00Z",  # 60s
            ),
            _person(
                person_id=2,
                created="2026-05-10T14:00:00Z",
                first_call="2026-05-10T14:05:00Z",  # 300s
            ),
            _person(person_id=3, first_call=None),  # excluded
        ]
        m = fdm.calculate_agent_metrics(people)
        assert m["response_time_seconds"] == 180.0

    def test_new_leads_not_acted_on(self):
        people = [
            _person(person_id=1, stage_id=26, contacted=0, first_call=None),  # stale
            _person(person_id=2, stage_id=26, contacted=1, first_call="2026-05-10T14:01:00Z"),
            _person(person_id=3, stage_id=28, contacted=1),
        ]
        m = fdm.calculate_agent_metrics(people)
        assert m["new_leads_not_acted_on"] == 1

    def test_handles_missing_fields_gracefully(self):
        """A sparse FUB record (missing optional fields) shouldn't blow up."""
        people = [{"id": 1, "source": "Zillow Preferred", "created": "2026-05-10T14:00:00Z"}]
        m = fdm.calculate_agent_metrics(people)
        assert m["total_zillow_leads"] == 1
        assert m["call_volume"] == 0


# ── HTTP layer (responses-mocked) ─────────────────────────────────────────────


@pytest.fixture
def fub_api_key(monkeypatch):
    monkeypatch.setattr(settings, "FUB_API_KEY", "test-key")
    monkeypatch.setattr(fdm, "FUB_API_KEY", "test-key")


class TestFetchPeopleForAgent:
    @responses.activate
    def test_single_page(self, fub_api_key):
        responses.add(
            responses.GET,
            "https://api.followupboss.com/v1/people",
            json={
                "_metadata": {"total": 2, "limit": 100, "offset": 0},
                "people": [_person(person_id=1), _person(person_id=2)],
            },
            status=200,
        )
        people = fdm.fetch_people_for_agent("42", "2026-05-01")
        assert len(people) == 2

    @responses.activate
    def test_pagination_offset(self, fub_api_key):
        # Two pages of 100, then a short page of 10.
        page1 = {
            "_metadata": {"total": 210, "limit": 100, "offset": 0},
            "people": [_person(person_id=i) for i in range(100)],
        }
        page2 = {
            "_metadata": {"total": 210, "limit": 100, "offset": 100},
            "people": [_person(person_id=i) for i in range(100, 200)],
        }
        page3 = {
            "_metadata": {"total": 210, "limit": 100, "offset": 200},
            "people": [_person(person_id=i) for i in range(200, 210)],
        }
        responses.add(
            responses.GET,
            "https://api.followupboss.com/v1/people",
            json=page1,
            status=200,
        )
        responses.add(
            responses.GET,
            "https://api.followupboss.com/v1/people",
            json=page2,
            status=200,
        )
        responses.add(
            responses.GET,
            "https://api.followupboss.com/v1/people",
            json=page3,
            status=200,
        )

        people = fdm.fetch_people_for_agent("42", "2026-05-01")
        assert len(people) == 210

    @responses.activate
    def test_short_page_terminates(self, fub_api_key):
        responses.add(
            responses.GET,
            "https://api.followupboss.com/v1/people",
            json={"_metadata": {"total": 5}, "people": [_person(person_id=i) for i in range(5)]},
            status=200,
        )
        people = fdm.fetch_people_for_agent("42", "2026-05-01")
        assert len(people) == 5

    @responses.activate
    def test_empty_results(self, fub_api_key):
        responses.add(
            responses.GET,
            "https://api.followupboss.com/v1/people",
            json={"_metadata": {"total": 0}, "people": []},
            status=200,
        )
        people = fdm.fetch_people_for_agent("42", "2026-05-01")
        assert people == []

    def test_raises_without_api_key(self, monkeypatch):
        monkeypatch.setattr(fdm, "FUB_API_KEY", "")
        with pytest.raises(OSError, match="FUB_API_KEY"):
            fdm.fetch_people_for_agent("42", "2026-05-01")


# ── pull_daily_metrics + save_results integration ─────────────────────────────


class TestPullDailyMetricsIntegration:
    @responses.activate
    def test_end_to_end_with_mocked_fub(self, fub_api_key, monkeypatch, isolated_db):
        # Roster: one agent (skip auto-discovery by populating AGENTS).
        monkeypatch.setattr(
            settings,
            "AGENTS",
            [{"name": "Alex", "email": "alex@x.com", "fub_agent_id": "42"}],
        )

        responses.add(
            responses.GET,
            "https://api.followupboss.com/v1/people",
            json={
                "_metadata": {"total": 2, "limit": 100, "offset": 0},
                "people": [
                    _person(
                        person_id=1,
                        contacted=1,
                        calls_outgoing=3,
                        texts_sent=4,
                        emails_sent=2,
                        stage_id=29,
                        calls_duration=200,
                    ),
                    _person(
                        person_id=2,
                        contacted=0,
                        source="Realtor.com",
                        source_id=99,
                    ),  # filtered out
                ],
            },
            status=200,
        )

        results = fdm.pull_daily_metrics(today=_FIXED_TODAY)
        assert len(results) == 1
        r = results[0]
        assert r["agent_id"] == "42"
        assert r["snapshot_date"] == "2026-05-15"
        assert r["window_start"] == "2026-05-01"
        assert r["metrics"]["total_zillow_leads"] == 1
        assert r["metrics"]["appointments_set"] == 1

        saved = fdm.save_results(results)
        assert saved == 1

        from src import storage

        snap = storage.latest_daily_snapshot("42")
        assert snap is not None
        assert snap["snapshot_date"] == "2026-05-15"
        assert snap["metrics"]["total_zillow_leads"] == 1.0
        assert snap["metrics"]["appointments_set"] == 1.0

    def test_mock_results_round_trip(self, isolated_db):
        results = fdm.mock_daily_results(today=_FIXED_TODAY)
        fdm.save_results(results)

        from src import storage

        all_snaps = storage.latest_daily_snapshots()
        assert len(all_snaps) == 2
        names = {s["name"] for s in all_snaps}
        assert names == {"Alex Rivera", "Jordan Lee"}
