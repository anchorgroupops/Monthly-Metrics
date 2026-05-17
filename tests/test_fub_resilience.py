"""
Resilience tests for the FUB clients.

Covers two narrow but high-impact behaviors that production depends on but
were previously untested:

1. **Soft-fail on /v1/calls and /v1/appointments**. When FUB returns 404 or
   403 (endpoint disabled / not authorized for the tenant), the daily pull
   must keep going and fall back to person-level aggregates. If we ever
   regress to raising here, every daily snapshot would error out across the
   whole roster, silently, until someone notices the /daily view is stale.

2. **Pagination caps**. Each fetcher has a hard ``if offset >= N: break``
   guard so a buggy ``_metadata.total`` from FUB can't pin a worker in an
   infinite-loop / quota-burn. These are pure defensive code and previously
   had zero coverage — exactly the kind of branch that gets accidentally
   deleted in a refactor.
"""

from __future__ import annotations

import json

import pytest
import requests
import responses

from config import settings
from src import fub_client
from src import fub_daily_metrics as fdm

# ── Helpers ───────────────────────────────────────────────────────────────────


def _person(
    *,
    person_id: int = 1,
    source: str = "Zillow Preferred",
    source_id: int | None = 14,
    created: str = "2026-05-10T14:00:00Z",
    contacted: int = 1,
    first_call: str | None = "2026-05-10T14:02:00Z",
    calls_outgoing: int = 0,
    texts_sent: int = 0,
    emails_sent: int = 0,
    calls_duration: int = 0,
    stage_id: int | None = 28,
) -> dict:
    return {
        "id": person_id,
        "source": source,
        "sourceId": source_id,
        "created": created,
        "contacted": contacted,
        "firstCall": first_call,
        "callsOutgoing": calls_outgoing,
        "textsSent": texts_sent,
        "emailsSent": emails_sent,
        "callsDuration": calls_duration,
        "stageId": stage_id,
    }


@pytest.fixture
def fub_api_key(monkeypatch):
    monkeypatch.setattr(settings, "FUB_API_KEY", "test-key")
    monkeypatch.setattr(fdm, "FUB_API_KEY", "test-key")
    monkeypatch.setattr(fub_client, "FUB_API_KEY", "test-key")


@pytest.fixture
def fast_retries(monkeypatch, mocker):
    """Trim retry budget to 1 and stub sleep so retry-path tests don't crawl."""
    monkeypatch.setattr(fdm, "FUB_MAX_RETRIES", 1)
    monkeypatch.setattr(fub_client, "FUB_MAX_RETRIES", 1)
    mocker.patch.object(fdm.time, "sleep")
    mocker.patch.object(fub_client.time, "sleep")


# ─────────────────────────────────────────────────────────────────────────────
# 1. Soft-fail on /v1/calls
# ─────────────────────────────────────────────────────────────────────────────


class TestDailyCallsFallback:
    @responses.activate
    def test_404_returns_empty_list(self, fub_api_key, fast_retries):
        responses.add(
            responses.GET,
            "https://api.followupboss.com/v1/calls",
            status=404,
            json={"error": "Not Found"},
        )
        assert fdm.fetch_calls_for_agent("42", "2026-05-01") == []

    @responses.activate
    def test_403_returns_empty_list(self, fub_api_key, fast_retries):
        responses.add(
            responses.GET,
            "https://api.followupboss.com/v1/calls",
            status=403,
            json={"error": "Forbidden"},
        )
        assert fdm.fetch_calls_for_agent("42", "2026-05-01") == []

    @responses.activate
    def test_other_4xx_reraises(self, fub_api_key, fast_retries):
        # 401 is auth, not "endpoint disabled" — must propagate so the operator
        # learns the key is wrong instead of getting empty pulls forever.
        responses.add(
            responses.GET,
            "https://api.followupboss.com/v1/calls",
            status=401,
            json={"error": "Unauthorized"},
        )
        with pytest.raises(requests.HTTPError):
            fdm.fetch_calls_for_agent("42", "2026-05-01")

    @responses.activate
    def test_network_error_swallowed_returns_empty(self, fub_api_key, fast_retries):
        # Generic Exception path (the broad `except Exception` after HTTPError):
        # transient connect failure on /calls should fall back, not abort the
        # whole agent's daily snapshot.
        responses.add(
            responses.GET,
            "https://api.followupboss.com/v1/calls",
            body=requests.ConnectionError("transient"),
        )
        assert fdm.fetch_calls_for_agent("42", "2026-05-01") == []

    def test_raises_without_api_key(self, monkeypatch):
        monkeypatch.setattr(fdm, "FUB_API_KEY", "")
        with pytest.raises(OSError, match="FUB_API_KEY"):
            fdm.fetch_calls_for_agent("42", "2026-05-01")


# ─────────────────────────────────────────────────────────────────────────────
# 2. Soft-fail on /v1/appointments (daily)
# ─────────────────────────────────────────────────────────────────────────────


class TestDailyAppointmentsFallback:
    @responses.activate
    def test_404_returns_empty_list(self, fub_api_key, fast_retries):
        responses.add(
            responses.GET,
            "https://api.followupboss.com/v1/appointments",
            status=404,
        )
        assert fdm.fetch_appointments_for_agent("42", "2026-05-01") == []

    @responses.activate
    def test_403_returns_empty_list(self, fub_api_key, fast_retries):
        responses.add(
            responses.GET,
            "https://api.followupboss.com/v1/appointments",
            status=403,
        )
        assert fdm.fetch_appointments_for_agent("42", "2026-05-01") == []

    @responses.activate
    def test_other_4xx_reraises(self, fub_api_key, fast_retries):
        responses.add(
            responses.GET,
            "https://api.followupboss.com/v1/appointments",
            status=401,
        )
        with pytest.raises(requests.HTTPError):
            fdm.fetch_appointments_for_agent("42", "2026-05-01")

    @responses.activate
    def test_network_error_swallowed_returns_empty(self, fub_api_key, fast_retries):
        responses.add(
            responses.GET,
            "https://api.followupboss.com/v1/appointments",
            body=requests.ConnectionError("transient"),
        )
        assert fdm.fetch_appointments_for_agent("42", "2026-05-01") == []

    def test_raises_without_api_key(self, monkeypatch):
        monkeypatch.setattr(fdm, "FUB_API_KEY", "")
        with pytest.raises(OSError, match="FUB_API_KEY"):
            fdm.fetch_appointments_for_agent("42", "2026-05-01")


# ─────────────────────────────────────────────────────────────────────────────
# 3. End-to-end: daily pull degrades gracefully when /calls + /appts are off
# ─────────────────────────────────────────────────────────────────────────────


class TestDailyPullDegradesGracefully:
    @responses.activate
    def test_metrics_still_computed_from_person_aggregates(
        self, fub_api_key, fast_retries, monkeypatch, isolated_db
    ):
        """
        Tenant where /v1/calls and /v1/appointments both 404. The daily
        snapshot must still land — using person-level callsOutgoing /
        callsDuration and stage-id inference for the appointment count.
        """
        from datetime import date

        monkeypatch.setattr(
            settings,
            "AGENTS",
            [{"name": "Alex", "email": "alex@x.com", "fub_agent_id": "42"}],
        )

        responses.add(
            responses.GET,
            "https://api.followupboss.com/v1/people",
            json={
                "_metadata": {"total": 2},
                "people": [
                    _person(
                        person_id=1,
                        contacted=1,
                        calls_outgoing=4,
                        calls_duration=200,  # ≥120s → counts as 2-min conversation
                        texts_sent=3,
                        emails_sent=1,
                        stage_id=29,  # ∈ APPT_STAGE_IDS → contributes to appt count
                    ),
                    _person(
                        person_id=2,
                        contacted=1,
                        calls_outgoing=2,
                        stage_id=28,
                    ),
                ],
            },
            status=200,
        )
        responses.add(responses.GET, "https://api.followupboss.com/v1/calls", status=404)
        responses.add(responses.GET, "https://api.followupboss.com/v1/appointments", status=404)

        results = fdm.pull_daily_metrics(today=date(2026, 5, 15))
        assert len(results) == 1
        r = results[0]
        assert "_error" not in r, f"unexpected error: {r.get('_error')}"

        m = r["metrics"]
        # Fallback path: call_volume = sum(person.callsOutgoing) = 4 + 2
        assert m["call_volume"] == 6
        # Fallback path: conversations_2min counts person with callsDuration ≥120
        assert m["conversations_2min"] == 1
        # Fallback path: appointment count = leads at stage 29 or 30
        assert m["appointments_set"] == 1
        # And aggregated text/email totals still flow through
        assert m["texts_sent"] == 3
        assert m["emails_sent"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# 4. Pagination caps — daily fetchers
# ─────────────────────────────────────────────────────────────────────────────


def _full_page_callback(record_key: str, page_size: int = 100):
    """
    Build a responses-callback that returns a 'full' page every time, with
    a `_metadata.total` larger than any cap. The fetchers should still stop
    at their hard cap, not loop forever.
    """

    def callback(_request):
        body = json.dumps(
            {
                "_metadata": {"total": 999_999, "limit": page_size, "offset": 0},
                record_key: [{"id": i} for i in range(page_size)],
            }
        )
        return (200, {}, body)

    return callback


class TestPaginationCaps:
    @responses.activate
    def test_daily_fetch_people_caps_at_5000(self, fub_api_key):
        responses.add_callback(
            responses.GET,
            "https://api.followupboss.com/v1/people",
            callback=_full_page_callback("people"),
        )
        people = fdm.fetch_people_for_agent("42", "2026-05-01")
        # Cap is `if offset >= 5000: break` after offset += 100 each iteration:
        # 50 page reads × 100 records, then loop breaks.
        assert len(people) == 5000
        assert len(responses.calls) == 50

    @responses.activate
    def test_daily_fetch_calls_caps_at_10000(self, fub_api_key):
        responses.add_callback(
            responses.GET,
            "https://api.followupboss.com/v1/calls",
            callback=_full_page_callback("calls"),
        )
        calls = fdm.fetch_calls_for_agent("42", "2026-05-01")
        # 100 page reads × 100 records before the offset >= 10000 guard fires.
        assert len(calls) == 10_000
        assert len(responses.calls) == 100

    @responses.activate
    def test_daily_fetch_appointments_caps_at_5000(self, fub_api_key):
        responses.add_callback(
            responses.GET,
            "https://api.followupboss.com/v1/appointments",
            callback=_full_page_callback("appointments"),
        )
        appts = fdm.fetch_appointments_for_agent("42", "2026-05-01")
        assert len(appts) == 5000
        assert len(responses.calls) == 50


# ─────────────────────────────────────────────────────────────────────────────
# 5. Pagination caps — monthly client (fub_client.py)
# ─────────────────────────────────────────────────────────────────────────────


def _zillow_full_page_callback(record_key: str, page_size: int = 100):
    """Same as _full_page_callback but each person record is Zillow Preferred,
    since fub_client._fetch_people_for_agent filters on is_zillow_preferred."""

    def callback(_request):
        body = json.dumps(
            {
                "_metadata": {"total": 999_999, "limit": page_size, "offset": 0},
                record_key: [
                    {"id": i, "sourceId": 14, "source": "Zillow Preferred"}
                    for i in range(page_size)
                ],
            }
        )
        return (200, {}, body)

    return callback


class TestMonthlyPaginationCaps:
    @responses.activate
    def test_monthly_fetch_people_caps_at_5000(self, fub_api_key):
        responses.add_callback(
            responses.GET,
            "https://api.followupboss.com/v1/people",
            callback=_zillow_full_page_callback("people"),
        )
        people = fub_client._fetch_people_for_agent("42", "2026-05-01", "2026-05-31")
        assert len(people) == 5000
        assert len(responses.calls) == 50

    @responses.activate
    def test_monthly_fetch_appointments_caps_at_5000(self, fub_api_key):
        responses.add_callback(
            responses.GET,
            "https://api.followupboss.com/v1/appointments",
            callback=_full_page_callback("appointments"),
        )
        appts = fub_client._fetch_appointments_for_agent("42", "2026-05-01", "2026-05-31")
        assert len(appts) == 5000
        assert len(responses.calls) == 50


# ─────────────────────────────────────────────────────────────────────────────
# 6. Monthly /v1/appointments soft-fail (same fallback story as daily)
# ─────────────────────────────────────────────────────────────────────────────


class TestMonthlyAppointmentsFallback:
    @responses.activate
    def test_404_returns_empty_list(self, fub_api_key, fast_retries):
        responses.add(
            responses.GET,
            "https://api.followupboss.com/v1/appointments",
            status=404,
        )
        assert fub_client._fetch_appointments_for_agent("42", "2026-05-01", "2026-05-31") == []

    @responses.activate
    def test_403_returns_empty_list(self, fub_api_key, fast_retries):
        responses.add(
            responses.GET,
            "https://api.followupboss.com/v1/appointments",
            status=403,
        )
        assert fub_client._fetch_appointments_for_agent("42", "2026-05-01", "2026-05-31") == []

    @responses.activate
    def test_other_4xx_reraises(self, fub_api_key, fast_retries):
        responses.add(
            responses.GET,
            "https://api.followupboss.com/v1/appointments",
            status=401,
        )
        with pytest.raises(requests.HTTPError):
            fub_client._fetch_appointments_for_agent("42", "2026-05-01", "2026-05-31")

    @responses.activate
    def test_network_error_swallowed_returns_empty(self, fub_api_key, fast_retries):
        responses.add(
            responses.GET,
            "https://api.followupboss.com/v1/appointments",
            body=requests.ConnectionError("transient"),
        )
        assert fub_client._fetch_appointments_for_agent("42", "2026-05-01", "2026-05-31") == []
