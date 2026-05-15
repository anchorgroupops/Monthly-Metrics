"""Tests for the FUB daily metrics calculator."""
from __future__ import annotations

import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

from src.fub_daily_metrics import (
    TARGETS,
    calc_agent_metrics,
    calc_response_time,
    calc_team_averages,
    is_zillow_lead,
    save_daily_snapshot,
)


# ── Fixtures ─────────────────────────────────────────────────────


def _make_lead(
    source="Zillow Preferred",
    source_id=14,
    contacted=1,
    stage_id=29,
    created_offset_hours=24,
    first_contact_offset_hours=23.5,
    calls_out=2,
    texts_sent=3,
    emails_sent=1,
):
    """Create a mock FUB lead dict."""
    now = datetime.now(timezone.utc)
    created = now - timedelta(hours=created_offset_hours)
    first_contact = now - timedelta(hours=first_contact_offset_hours)

    return {
        "id": 1000,
        "source": source,
        "sourceId": source_id,
        "contacted": contacted,
        "stageId": stage_id,
        "created": created.isoformat(),
        "lastOutgoingCall": first_contact.isoformat() if calls_out > 0 else None,
        "lastSentText": first_contact.isoformat() if texts_sent > 0 else None,
        "lastSentEmail": first_contact.isoformat() if emails_sent > 0 else None,
        "firstCall": 120 if calls_out > 0 else 0,
        "callsOutgoing": calls_out,
        "callsIncoming": 1,
        "textsSent": texts_sent,
        "textsReceived": 2,
        "emailsSent": emails_sent,
        "emailsReceived": 0,
    }


# ── is_zillow_lead ───────────────────────────────────────────────


def test_zillow_lead_by_source_id():
    assert is_zillow_lead({"sourceId": 14, "source": "something"})


def test_zillow_lead_by_source_name():
    assert is_zillow_lead({"sourceId": 99, "source": "Zillow Preferred"})


def test_zillow_lead_by_source_name_flex():
    assert is_zillow_lead({"sourceId": 99, "source": "Zillow Flex"})


def test_non_zillow_lead():
    assert not is_zillow_lead({"sourceId": 22, "source": "Agent-generated"})


def test_none_source():
    assert not is_zillow_lead({"sourceId": None, "source": None})


# ── calc_response_time ───────────────────────────────────────────


def test_response_time_from_call():
    lead = _make_lead(first_contact_offset_hours=23.5)
    rt = calc_response_time(lead)
    assert rt is not None
    assert 1700 < rt < 1900  # ~30 min = 1800s


def test_response_time_no_contact():
    lead = _make_lead(calls_out=0, texts_sent=0, emails_sent=0)
    lead["lastOutgoingCall"] = None
    lead["lastSentText"] = None
    lead["lastSentEmail"] = None
    assert calc_response_time(lead) is None


def test_response_time_no_created():
    lead = _make_lead()
    lead["created"] = None
    assert calc_response_time(lead) is None


# ── calc_agent_metrics ───────────────────────────────────────────


def test_empty_leads():
    m = calc_agent_metrics([])
    assert m["total_zillow_leads"] == 0
    assert m["response_time_avg"] is None
    assert m["contact_rate"] is None


def test_no_zillow_leads():
    leads = [_make_lead(source="Agent-generated", source_id=22)]
    m = calc_agent_metrics(leads)
    assert m["total_zillow_leads"] == 0
    assert m["total_all_leads"] == 1


def test_single_zillow_lead():
    leads = [_make_lead()]
    m = calc_agent_metrics(leads)
    assert m["total_zillow_leads"] == 1
    assert m["contact_rate"] == 1.0
    assert m["calls_outgoing"] == 2
    assert m["texts_sent"] == 3
    assert m["appointment_rate"] == 1.0  # stageId=29


def test_mixed_leads():
    leads = [
        _make_lead(contacted=1, stage_id=30),
        _make_lead(contacted=0, stage_id=26),
        _make_lead(source="Agent-generated", source_id=22),
    ]
    m = calc_agent_metrics(leads)
    assert m["total_zillow_leads"] == 2
    assert m["total_all_leads"] == 3
    assert m["contact_rate"] == 0.5
    assert m["appointment_rate"] == 0.5  # 1 out of 2 Zillow leads


def test_appointment_rate_below_threshold():
    leads = [_make_lead(stage_id=27)]  # "Attempted contact" — not an appointment
    m = calc_agent_metrics(leads)
    assert m["appointment_rate"] == 0.0


def test_lead_acceptance_rate():
    leads = [
        _make_lead(stage_id=28),  # "Spoke with customer" — accepted
        _make_lead(stage_id=26),  # "New" — not accepted
    ]
    m = calc_agent_metrics(leads)
    assert m["lead_acceptance_rate"] == 0.5


# ── calc_team_averages ───────────────────────────────────────────


def test_team_averages_empty():
    team = calc_team_averages([])
    assert team["total_zillow_leads"] == 0


def test_team_averages_two_agents():
    results = [
        {
            "agent_id": 1,
            "agent_name": "Alice",
            "metrics": {
                "total_zillow_leads": 10,
                "total_all_leads": 15,
                "response_time_avg": 200,
                "contact_rate": 0.8,
                "calls_outgoing": 20,
                "calls_per_lead": 2.0,
                "texts_sent": 30,
                "texts_per_lead": 3.0,
                "emails_sent": 5,
                "appointment_rate": 0.3,
                "lead_acceptance_rate": 0.9,
            },
        },
        {
            "agent_id": 2,
            "agent_name": "Bob",
            "metrics": {
                "total_zillow_leads": 5,
                "total_all_leads": 8,
                "response_time_avg": 400,
                "contact_rate": 0.6,
                "calls_outgoing": 10,
                "calls_per_lead": 2.0,
                "texts_sent": 15,
                "texts_per_lead": 3.0,
                "emails_sent": 3,
                "appointment_rate": 0.2,
                "lead_acceptance_rate": 0.8,
            },
        },
    ]
    team = calc_team_averages(results)
    assert team["total_zillow_leads"] == 15
    assert team["response_time_avg"] == 300.0
    assert team["contact_rate"] == 0.7
    assert team["appointment_rate"] == 0.25


# ── save_daily_snapshot ──────────────────────────────────────────


def test_save_snapshot():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    results = [
        {
            "agent_id": 1,
            "agent_name": "Test Agent",
            "metrics": {
                "total_zillow_leads": 5,
                "total_all_leads": 10,
                "response_time_avg": 180.5,
                "contact_rate": 0.8,
                "calls_outgoing": 15,
                "calls_per_lead": 3.0,
                "texts_sent": 20,
                "texts_per_lead": 4.0,
                "emails_sent": 5,
                "appointment_rate": 0.4,
                "lead_acceptance_rate": 0.9,
            },
        }
    ]

    save_daily_snapshot(results, db_path=db_path)

    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT * FROM daily_snapshots").fetchall()
    conn.close()

    assert len(rows) == 1
    assert rows[0][3] == "Test Agent"  # agent_name
    assert rows[0][4] == 5  # total_zillow_leads


def test_save_snapshot_upsert():
    """Saving twice on the same day should update, not duplicate."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    results = [
        {
            "agent_id": 1,
            "agent_name": "Agent A",
            "metrics": {
                "total_zillow_leads": 3,
                "total_all_leads": 5,
                "response_time_avg": 100,
                "contact_rate": 0.5,
                "calls_outgoing": 6,
                "calls_per_lead": 2.0,
                "texts_sent": 9,
                "texts_per_lead": 3.0,
                "emails_sent": 2,
                "appointment_rate": 0.1,
                "lead_acceptance_rate": 0.7,
            },
        }
    ]

    save_daily_snapshot(results, db_path=db_path)
    save_daily_snapshot(results, db_path=db_path)

    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM daily_snapshots").fetchone()[0]
    conn.close()

    assert count == 1  # Upsert, not duplicate


# ── TARGETS sanity ───────────────────────────────────────────────


def test_targets_exist():
    assert "response_time_sec" in TARGETS
    assert "contact_rate" in TARGETS
    assert "appointment_rate" in TARGETS
    assert TARGETS["response_time_sec"] == 300
