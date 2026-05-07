"""Tests for src/daily_sync.py."""

import json

import pytest


@pytest.fixture
def isolated(monkeypatch, tmp_db, thresholds_full):
    """Point storage and thresholds at test fixtures, populate roster."""
    monkeypatch.setattr("src.storage.DATABASE_PATH", tmp_db)
    monkeypatch.setattr("src.metrics.load_thresholds", lambda: thresholds_full)

    def fake_load_agents():
        return [
            {"name": "Alex Rivera", "email": "alex@example.com", "fub_agent_id": "mock-001"},
            {"name": "Jordan Lee",  "email": "jordan@example.com", "fub_agent_id": "mock-002"},
            {"name": "Morgan Chen", "email": "morgan@example.com", "fub_agent_id": "mock-003"},
        ]

    monkeypatch.setattr("src.daily_sync.load_agents", fake_load_agents)
    return tmp_db


class TestRun:
    def test_writes_one_snapshot_per_mock_agent(self, isolated):
        from src import daily_sync, storage
        summary = daily_sync.run(mock=True)
        assert summary == {"agents": 3, "snapshots": 3}

        with storage.connect(isolated) as conn:
            count = conn.execute("SELECT COUNT(*) FROM metric_snapshots").fetchone()[0]
        assert count == 3

    def test_running_twice_same_day_does_not_duplicate(self, isolated):
        from src import daily_sync, storage
        daily_sync.run(mock=True)
        daily_sync.run(mock=True)

        with storage.connect(isolated) as conn:
            count = conn.execute("SELECT COUNT(*) FROM metric_snapshots").fetchone()[0]
        assert count == 3

    def test_empty_roster_short_circuits(self, monkeypatch, tmp_db):
        from src import daily_sync, storage
        monkeypatch.setattr("src.storage.DATABASE_PATH", tmp_db)
        monkeypatch.setattr("src.daily_sync.load_agents", lambda: [])
        out = daily_sync.run(mock=True)
        assert out == {"agents": 0, "snapshots": 0}

    def test_roster_changes_propagate_to_agents_table(self, isolated):
        from src import daily_sync, storage
        daily_sync.run(mock=True)
        with storage.connect(isolated) as conn:
            rows = conn.execute(
                "SELECT email FROM agents WHERE active=1"
            ).fetchall()
        emails = {r["email"] for r in rows}
        assert emails == {"alex@example.com", "jordan@example.com", "morgan@example.com"}

    def test_snapshot_persists_overall_status_and_metrics(self, isolated):
        from src import daily_sync, storage
        daily_sync.run(mock=True)
        with storage.connect(isolated) as conn:
            row = conn.execute(
                """
                SELECT s.* FROM metric_snapshots s
                JOIN agents a ON a.id = s.agent_id
                WHERE a.email = ?
                """,
                ("alex@example.com",),
            ).fetchone()
        assert row["pcvr"] == 0.038
        assert row["pickup_rate"] == 0.91
        assert row["overall_status"] in ("Preferred", "At Risk", "Needs Improvement")
        # raw_json is a complete scored_agent payload.
        raw = json.loads(row["raw_json"])
        assert raw["name"] == "Alex Rivera"
        assert "metrics" in raw
