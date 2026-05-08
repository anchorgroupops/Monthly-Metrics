"""Tests for src/migrations/ — forward-only schema runner."""

import sqlite3

import pytest


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    from src import storage

    monkeypatch.setattr(storage, "DB_PATH", db)
    return db


class TestMigrationRunner:
    def test_runs_all_pending_on_empty_db(self, fresh_db):
        from src.migrations._runner import apply_pending_migrations

        applied = apply_pending_migrations(fresh_db)
        assert "001_initial.sql" in applied

        conn = sqlite3.connect(fresh_db)
        try:
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        finally:
            conn.close()
        assert "agent_periods" in tables
        assert "drafts" in tables
        assert "schema_migrations" in tables

    def test_idempotent_second_run_applies_nothing(self, fresh_db):
        from src.migrations._runner import apply_pending_migrations

        first = apply_pending_migrations(fresh_db)
        second = apply_pending_migrations(fresh_db)

        assert len(first) >= 1
        assert second == []

    def test_records_applied_filename(self, fresh_db):
        from src.migrations._runner import apply_pending_migrations

        apply_pending_migrations(fresh_db)
        conn = sqlite3.connect(fresh_db)
        try:
            rows = conn.execute(
                "SELECT filename FROM schema_migrations ORDER BY filename"
            ).fetchall()
        finally:
            conn.close()
        assert rows[0][0] == "001_initial.sql"

    def test_sets_wal_mode(self, fresh_db):
        from src.migrations._runner import apply_pending_migrations

        apply_pending_migrations(fresh_db)
        conn = sqlite3.connect(fresh_db)
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            conn.close()
        assert mode.lower() == "wal"
