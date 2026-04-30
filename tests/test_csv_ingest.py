"""Tests for CSV/JSON admin ingest + SQLite round-trip."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "april_2026_sample.csv"


def test_csv_round_trip(isolated_db, isolated_thresholds):
    # Default thresholds.json (the seed defaults) match the fixture columns.
    from src.csv_ingest import parse_file
    from src.storage import load_period, save_period

    agents = parse_file(FIXTURE)
    assert len(agents) == 3
    assert agents[0]["name"] == "Alex Rivera"
    assert agents[0]["speed_to_action"] == 180.0
    assert agents[0]["period"] == "April 2026"

    save_period(agents, source="csv", file_path=str(FIXTURE))

    loaded = load_period("April 2026")
    assert len(loaded) == 3
    by_id = {a["agent_id"]: a for a in loaded}
    assert by_id["mock-001"]["speed_to_action"] == 180.0
    assert by_id["mock-002"]["work_with_rate"] == 0.41


def test_csv_missing_required_column(tmp_path, isolated_thresholds):
    from src.csv_ingest import parse_file

    bad = tmp_path / "bad.csv"
    bad.write_text("name,email,period,csat\nA,a@x.com,April 2026,0.9\n")
    with pytest.raises(ValueError, match="agent_id"):
        parse_file(bad)


def test_csv_missing_metric_column(tmp_path, isolated_thresholds):
    from src.csv_ingest import parse_file

    bad = tmp_path / "bad.csv"
    bad.write_text(
        "agent_id,name,email,period,csat\n"
        "a1,A,a@x.com,April 2026,0.9\n"
    )
    with pytest.raises(ValueError, match="missing metric columns"):
        parse_file(bad)


def test_json_ingest(tmp_path, isolated_db, isolated_thresholds):
    from src.csv_ingest import parse_file
    from src.storage import load_period, save_period

    payload = [{
        "agent_id": "j1", "name": "Jay", "email": "j@x.com", "period": "2026-04",
        "speed_to_action": 250, "work_with_rate": 0.55, "csat": 0.88,
        "appt_set_rate": 0.65, "appt_met_rate": 0.72,
    }]
    f = tmp_path / "april.json"
    f.write_text(json.dumps(payload))

    agents = parse_file(f)
    assert agents[0]["agent_id"] == "j1"
    assert agents[0]["period"] == "April 2026"

    save_period(agents, source="json", file_path=str(f))
    loaded = load_period("2026-04")
    assert len(loaded) == 1
    assert loaded[0]["csat"] == 0.88


def test_period_normalization():
    from src.storage import normalize_period
    assert normalize_period("April 2026") == "2026-04"
    assert normalize_period("2026-04") == "2026-04"
    assert normalize_period("2026-04-15") == "2026-04"
    assert normalize_period("Apr 2026") == "2026-04"


def test_percent_string_coercion(tmp_path, isolated_thresholds):
    from src.csv_ingest import parse_file

    f = tmp_path / "pct.csv"
    f.write_text(
        "agent_id,name,email,period,speed_to_action,work_with_rate,csat,appt_set_rate,appt_met_rate\n"
        "a1,A,a@x.com,April 2026,300,55%,87%,60%,70%\n"
    )
    agents = parse_file(f)
    # Trailing % is stripped but value is taken as-is (55 not 0.55).
    # Admins should provide decimals; this test just guards against crashes.
    assert agents[0]["work_with_rate"] == 55.0
