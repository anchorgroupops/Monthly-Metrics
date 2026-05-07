"""Tests for src/storage.py — SQLite schema + CRUD."""

from datetime import datetime, timedelta, timezone

import pytest

from src import storage


# ── Schema / agents ───────────────────────────────────────────────────────────

class TestSchema:
    def test_init_creates_all_tables(self, tmp_db):
        with storage.connect(tmp_db) as conn:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
        assert {"agents", "metric_snapshots", "magic_links", "sessions"} <= tables

    def test_init_is_idempotent(self, tmp_db):
        # Calling twice should not raise.
        storage.init_schema(db_path=tmp_db)
        storage.init_schema(db_path=tmp_db)


class TestUpsertAgents:
    def test_inserts_new_rows_and_assigns_slug(self, tmp_db):
        roster = [
            {"name": "Alice Smith", "email": "alice@x", "fub_agent_id": "1"},
            {"name": "Bob Jones",   "email": "bob@x",   "fub_agent_id": "2"},
        ]
        storage.upsert_agents(roster, db_path=tmp_db)
        a = storage.get_agent_by_email("alice@x", db_path=tmp_db)
        assert a is not None
        assert a["slug"] == "alice-smith"
        assert a["active"] == 1

    def test_updates_existing_row_in_place(self, tmp_db):
        roster = [{"name": "Alice", "email": "a@x", "fub_agent_id": "1"}]
        storage.upsert_agents(roster, db_path=tmp_db)
        # Now rename
        roster = [{"name": "Alice Smith", "email": "a@x", "fub_agent_id": "9"}]
        storage.upsert_agents(roster, db_path=tmp_db)
        a = storage.get_agent_by_email("a@x", db_path=tmp_db)
        assert a["name"] == "Alice Smith"
        assert a["fub_agent_id"] == "9"

    def test_rows_missing_from_roster_get_deactivated(self, tmp_db):
        roster1 = [
            {"name": "Alice", "email": "a@x", "fub_agent_id": "1"},
            {"name": "Bob",   "email": "b@x", "fub_agent_id": "2"},
        ]
        storage.upsert_agents(roster1, db_path=tmp_db)
        # Bob disappears
        storage.upsert_agents(
            [{"name": "Alice", "email": "a@x", "fub_agent_id": "1"}],
            db_path=tmp_db,
        )
        # get_agent_by_email filters to active=1
        assert storage.get_agent_by_email("b@x", db_path=tmp_db) is None
        # But the row still exists with active=0
        with storage.connect(tmp_db) as conn:
            row = conn.execute(
                "SELECT active FROM agents WHERE email = ?", ("b@x",)
            ).fetchone()
        assert row["active"] == 0


# ── Snapshots ────────────────────────────────────────────────────────────────

@pytest.fixture
def alice(tmp_db):
    storage.upsert_agents(
        [{"name": "Alice", "email": "alice@x", "fub_agent_id": "1"}],
        db_path=tmp_db,
    )
    return storage.get_agent_by_email("alice@x", db_path=tmp_db)


def _scored(period="March 2026", as_of=None, **values):
    metrics = {k: {"value": v} for k, v in values.items()}
    out = {
        "agent_id": "1",
        "name": "Alice",
        "email": "alice@x",
        "period": period,
        "metrics": metrics,
        "overall_status": "Preferred",
    }
    if as_of:
        out["as_of_date"] = as_of
    return out


class TestSnapshots:
    def test_writes_and_reads_latest(self, tmp_db, alice):
        storage.write_snapshot(
            _scored(as_of="2026-04-15",
                    pCVR=0.04, pickup_rate=0.9, csat=4.7, zhl_transfers=3),
            db_path=tmp_db,
        )
        latest = storage.latest_snapshot(alice["id"], db_path=tmp_db)
        assert latest["pcvr"] == 0.04
        assert latest["pickup_rate"] == 0.9
        assert latest["overall_status"] == "Preferred"
        assert latest["period"] == "2026-03"
        assert latest["raw"]["name"] == "Alice"

    def test_idempotent_on_same_day(self, tmp_db, alice):
        # Two calls on same date = one row, with the second value winning.
        storage.write_snapshot(
            _scored(as_of="2026-04-15", pCVR=0.03,
                    pickup_rate=0.8, csat=4.0, zhl_transfers=2),
            db_path=tmp_db,
        )
        storage.write_snapshot(
            _scored(as_of="2026-04-15", pCVR=0.05,
                    pickup_rate=0.95, csat=4.8, zhl_transfers=4),
            db_path=tmp_db,
        )
        with storage.connect(tmp_db) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM metric_snapshots WHERE agent_id = ?",
                (alice["id"],),
            ).fetchone()[0]
        assert count == 1
        latest = storage.latest_snapshot(alice["id"], db_path=tmp_db)
        assert latest["pcvr"] == 0.05

    def test_trend_returns_one_row_per_period(self, tmp_db, alice):
        # Three months with multiple as-of-dates each — trend returns the latest.
        # March (2 days), April (1 day), May (2 days).
        for period, day, pcvr in [
            ("March 2026", "2026-03-15", 0.020),
            ("March 2026", "2026-03-31", 0.025),
            ("April 2026", "2026-04-30", 0.030),
            ("May 2026",   "2026-05-15", 0.032),
            ("May 2026",   "2026-05-30", 0.034),
        ]:
            storage.write_snapshot(
                _scored(period=period, as_of=day, pCVR=pcvr,
                        pickup_rate=0.85, csat=4.4, zhl_transfers=3),
                db_path=tmp_db,
            )
        trend = storage.trend_snapshots(alice["id"], months=6, db_path=tmp_db)
        assert [r["period"] for r in trend] == ["2026-03", "2026-04", "2026-05"]
        # Each period returns the LAST as-of-date for that month.
        assert trend[0]["pcvr"] == 0.025
        assert trend[2]["pcvr"] == 0.034

    def test_trend_respects_months_limit(self, tmp_db, alice):
        for month in range(1, 13):
            storage.write_snapshot(
                _scored(
                    period=f"2026-{month:02d}",
                    as_of=f"2026-{month:02d}-28",
                    pCVR=0.03, pickup_rate=0.8, csat=4.0, zhl_transfers=2,
                ),
                db_path=tmp_db,
            )
        trend = storage.trend_snapshots(alice["id"], months=4, db_path=tmp_db)
        assert len(trend) == 4
        # Most recent four months, ascending.
        assert [r["period"] for r in trend] == [
            "2026-09", "2026-10", "2026-11", "2026-12"
        ]


# ── Magic links ──────────────────────────────────────────────────────────────

class TestMagicLinks:
    def test_create_then_consume(self, tmp_db):
        token = storage.create_magic_link("a@x", ttl_minutes=15, db_path=tmp_db)
        assert isinstance(token, str) and len(token) > 20
        email = storage.consume_magic_link(token, db_path=tmp_db)
        assert email == "a@x"

    def test_consume_twice_returns_none(self, tmp_db):
        token = storage.create_magic_link("a@x", 15, db_path=tmp_db)
        assert storage.consume_magic_link(token, db_path=tmp_db) == "a@x"
        assert storage.consume_magic_link(token, db_path=tmp_db) is None

    def test_unknown_token_returns_none(self, tmp_db):
        assert storage.consume_magic_link("not-a-real-token", db_path=tmp_db) is None

    def test_expired_token_returns_none(self, tmp_db):
        # Manually insert an already-expired row.
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        with storage.connect(tmp_db) as conn:
            conn.execute(
                "INSERT INTO magic_links (token, email, expires_at) VALUES (?, ?, ?)",
                ("expired", "a@x", past),
            )
            conn.commit()
        assert storage.consume_magic_link("expired", db_path=tmp_db) is None


# ── Sessions ─────────────────────────────────────────────────────────────────

class TestSessions:
    def test_create_then_lookup(self, tmp_db, alice):
        token = storage.create_session(alice["id"], ttl_days=30, db_path=tmp_db)
        agent = storage.lookup_session(token, db_path=tmp_db)
        assert agent is not None
        assert agent["email"] == "alice@x"

    def test_delete_session_invalidates_lookup(self, tmp_db, alice):
        token = storage.create_session(alice["id"], ttl_days=30, db_path=tmp_db)
        storage.delete_session(token, db_path=tmp_db)
        assert storage.lookup_session(token, db_path=tmp_db) is None

    def test_inactive_agent_sessions_dont_resolve(self, tmp_db, alice):
        token = storage.create_session(alice["id"], ttl_days=30, db_path=tmp_db)
        with storage.connect(tmp_db) as conn:
            conn.execute("UPDATE agents SET active = 0 WHERE id = ?", (alice["id"],))
            conn.commit()
        assert storage.lookup_session(token, db_path=tmp_db) is None

    def test_blank_token_returns_none(self, tmp_db):
        assert storage.lookup_session("", db_path=tmp_db) is None
        assert storage.lookup_session(None, db_path=tmp_db) is None  # type: ignore[arg-type]


# ── Seed history (admin helper) ──────────────────────────────────────────────

class TestSeedHistory:
    def test_writes_six_months(self, tmp_db, alice):
        n = storage.seed_history("alice@x", db_path=tmp_db)
        assert n == 6
        trend = storage.trend_snapshots(alice["id"], months=6, db_path=tmp_db)
        assert len(trend) == 6

    def test_unknown_email_raises(self, tmp_db):
        with pytest.raises(ValueError):
            storage.seed_history("nope@x", db_path=tmp_db)
