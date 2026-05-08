"""
Tests for the FUB pull pipeline:
- run-lifecycle helpers in src.storage (start_run / finish_run / get_active_run)
- save_period() with a pre-allocated run_id (dashboard manual-pull path)
- cmd_pull() error path (FUB error → run marked 'error', exit 1)
- cmd_pull() happy path (idempotent: same data twice → one set of rows)
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

# ── storage: run lifecycle ────────────────────────────────────────────────────


def test_start_run_creates_running_row(isolated_db):
    from src import storage

    run_id = storage.start_run(source="fub")
    active = storage.get_active_run()
    assert active is not None
    assert active["id"] == run_id
    assert active["status"] == "running"
    assert active["source"] == "fub"


def test_finish_run_clears_active(isolated_db):
    from src import storage

    run_id = storage.start_run(source="fub")
    storage.finish_run(run_id, "ok", "all good")
    assert storage.get_active_run() is None
    latest = storage.latest_run(source="fub")
    assert latest["status"] == "ok"
    assert latest["notes"] == "all good"


def test_finish_run_rejects_bad_status(isolated_db):
    from src import storage

    run_id = storage.start_run(source="fub")
    with pytest.raises(ValueError):
        storage.finish_run(run_id, "queued")


def test_save_period_with_run_id_updates_existing(isolated_db):
    """save_period(run_id=...) must NOT create a second runs row."""
    from src import storage

    run_id = storage.start_run(source="fub")
    agents = [
        {
            "agent_id": "a1",
            "name": "A",
            "email": "a@x.com",
            "period": "2026-04",
            "pCVR": 0.05,
        }
    ]
    storage.save_period(agents, source="fub", run_id=run_id)

    # Exactly one row for this run, in 'ok' state.
    assert storage.get_active_run() is None
    with storage.connect() as conn:
        rows = conn.execute("SELECT id, status, row_count FROM runs").fetchall()
    assert len(rows) == 1
    assert rows[0]["id"] == run_id
    assert rows[0]["status"] == "ok"
    assert rows[0]["row_count"] == 1


def test_save_period_without_run_id_inserts_new(isolated_db):
    """Backwards-compat: save_period without run_id still inserts a new row."""
    from src import storage

    agents = [
        {
            "agent_id": "a1",
            "name": "A",
            "email": "a@x.com",
            "period": "2026-04",
            "pCVR": 0.05,
        }
    ]
    storage.save_period(agents, source="csv")
    with storage.connect() as conn:
        rows = conn.execute("SELECT status FROM runs").fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == "ok"


# ── cmd_pull ──────────────────────────────────────────────────────────────────


@pytest.fixture
def fake_fub_agents():
    """Two-agent payload that matches what fetch_all_agents() would return."""
    return [
        {
            "agent_id": "100",
            "name": "Alex Rivera",
            "email": "alex@x.com",
            "period": "April 2026",
            "start_date": "2026-04-01",
            "end_date": "2026-04-30",
            "pCVR": 0.038,
            "pickup_rate": 0.91,
            "csat": 4.7,
            "zhl_transfers": 5,
            "_raw": {},
        },
        {
            "agent_id": "200",
            "name": "Jordan Lee",
            "email": "jordan@x.com",
            "period": "April 2026",
            "start_date": "2026-04-01",
            "end_date": "2026-04-30",
            "pCVR": 0.021,
            "pickup_rate": 0.74,
            "csat": 4.1,
            "zhl_transfers": 2,
            "_raw": {},
        },
    ]


def _patched_settings(monkeypatch):
    """cmd_pull guards on AGENTS + FUB_API_KEY — tests bypass those gates."""
    from config import settings

    monkeypatch.setattr(settings, "AGENTS", [{"name": "x", "email": "x@x", "fub_agent_id": "100"}])
    monkeypatch.setattr(settings, "FUB_API_KEY", "test-key")


def test_cmd_pull_happy_path(isolated_db, monkeypatch, fake_fub_agents):
    from main import cmd_pull
    from src import storage

    _patched_settings(monkeypatch)
    with patch("src.fub_client.fetch_all_agents", return_value=fake_fub_agents):
        rc = cmd_pull(args=type("A", (), {})())

    assert rc == 0
    # Exactly one runs row, status ok, both agents persisted.
    with storage.connect() as conn:
        runs = conn.execute("SELECT status, row_count FROM runs").fetchall()
    assert len(runs) == 1
    assert runs[0]["status"] == "ok"
    assert runs[0]["row_count"] == 2

    loaded = storage.load_period("2026-04")
    assert len(loaded) == 2
    assert {a["agent_id"] for a in loaded} == {"100", "200"}


def test_cmd_pull_idempotent(isolated_db, monkeypatch, fake_fub_agents):
    """Two pulls of the same data → upserted, no duplicates."""
    from main import cmd_pull
    from src import storage

    _patched_settings(monkeypatch)
    with patch("src.fub_client.fetch_all_agents", return_value=fake_fub_agents):
        cmd_pull(args=type("A", (), {})())
        cmd_pull(args=type("A", (), {})())

    loaded = storage.load_period("2026-04")
    # Still exactly two agents — upsert on (agent_id, period, metric_key).
    assert len(loaded) == 2
    # Two run rows (one per pull) is fine — that's audit history, not data.
    with storage.connect() as conn:
        runs = conn.execute("SELECT id FROM runs").fetchall()
    assert len(runs) == 2


def test_cmd_pull_marks_error_on_fub_failure(isolated_db, monkeypatch):
    from main import cmd_pull
    from src import storage

    _patched_settings(monkeypatch)
    with patch("src.fub_client.fetch_all_agents", side_effect=RuntimeError("FUB 502")):
        rc = cmd_pull(args=type("A", (), {})())

    assert rc == 1
    # No active run after failure — should be terminal 'error', not stuck.
    assert storage.get_active_run() is None
    latest = storage.latest_run(source="fub")
    assert latest["status"] == "error"
    assert "FUB 502" in (latest["notes"] or "")


def test_cmd_pull_discovery_returns_no_agents(isolated_db, monkeypatch, mocker):
    """
    AGENTS empty + auto-discovery returns [] → graceful exit 0, run row marked
    'ok' with the no-agents note. (fetch_all_agents is mocked so the test never
    touches the live FUB API.)
    """
    from config import settings
    from main import cmd_pull
    from src import storage

    monkeypatch.setattr(settings, "FUB_API_KEY", "test-key")
    mocker.patch("src.fub_client.fetch_all_agents", return_value=[])

    rc = cmd_pull(args=type("A", (), {})())
    assert rc == 0
    with storage.connect() as conn:
        runs = conn.execute("SELECT id, status, notes FROM runs").fetchall()
    assert len(runs) == 1
    assert runs[0][1] == "ok"
    assert "no agents" in (runs[0][2] or "").lower()


def test_cmd_pull_missing_api_key(isolated_db, monkeypatch):
    from config import settings
    from main import cmd_pull
    from src import storage

    monkeypatch.setattr(settings, "FUB_API_KEY", "")

    rc = cmd_pull(args=type("A", (), {})())
    assert rc == 1
    with storage.connect() as conn:
        runs = conn.execute("SELECT id FROM runs").fetchall()
    assert len(runs) == 0
