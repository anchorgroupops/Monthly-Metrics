"""Tests for src/storage.py — SQLite persistence + draft approval queue."""

import pytest


def _agent(agent_id="100", period="2026-04", name="Alice", email="alice@x.com", **metrics):
    """Build a single agent record. Default metrics are a small set; override with kwargs."""
    if not metrics:
        metrics = {"csat": 0.85, "response_time": 120.0}
    return {
        "agent_id": agent_id,
        "name": name,
        "email": email,
        "period": period,
        **metrics,
    }


# ── normalize_period ──────────────────────────────────────────────────────────


class TestNormalizePeriod:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("2026-04", "2026-04"),
            ("2026-04-15", "2026-04"),
            ("April 2026", "2026-04"),
            ("Apr 2026", "2026-04"),
            ("  April 2026  ", "2026-04"),
        ],
    )
    def test_accepts_recognized_formats(self, raw, expected):
        from src.storage import normalize_period

        assert normalize_period(raw) == expected

    def test_raises_on_unrecognized_format(self):
        from src.storage import normalize_period

        with pytest.raises(ValueError, match="Unrecognized period format"):
            normalize_period("not a period")


# ── period_label ──────────────────────────────────────────────────────────────


class TestPeriodLabel:
    def test_canonical_to_human(self):
        from src.storage import period_label

        assert period_label("2026-04") == "April 2026"

    def test_returns_input_unchanged_on_garbage(self):
        from src.storage import period_label

        assert period_label("garbage") == "garbage"


# ── save_period / load_period ─────────────────────────────────────────────────


class TestSavePeriod:
    def test_round_trip(self, isolated_db):
        from src import storage

        run_id = storage.save_period([_agent()], source="csv")
        assert isinstance(run_id, int) and run_id > 0

        loaded = storage.load_period("2026-04")
        assert len(loaded) == 1
        assert loaded[0]["agent_id"] == "100"
        assert loaded[0]["name"] == "Alice"
        assert loaded[0]["email"] == "alice@x.com"
        assert loaded[0]["csat"] == 0.85

    def test_idempotent_upsert(self, isolated_db):
        """Re-saving the same agent+period upserts metrics, doesn't duplicate rows."""
        from src import storage

        storage.save_period([_agent(csat=0.80)], source="csv")
        storage.save_period([_agent(csat=0.95)], source="csv")

        loaded = storage.load_period("2026-04")
        assert len(loaded) == 1
        assert loaded[0]["csat"] == 0.95  # latest value wins

    def test_raises_on_empty_input(self, isolated_db):
        from src import storage

        with pytest.raises(ValueError, match="no agent records"):
            storage.save_period([], source="csv")

    def test_updates_existing_run_id(self, isolated_db):
        """When run_id is provided, update that row instead of inserting a new one."""
        from src import storage

        run_id = storage.start_run(source="fub")
        result_id = storage.save_period([_agent()], source="fub", run_id=run_id)
        assert result_id == run_id

        with storage.connect() as conn:
            rows = conn.execute("SELECT id, status, row_count FROM runs").fetchall()
        assert len(rows) == 1
        assert rows[0]["status"] == "ok"
        assert rows[0]["row_count"] == 1

    def test_skips_non_numeric_metric_values(self, isolated_db):
        """Metrics that aren't int/float/None (e.g., strings) are ignored."""
        from src import storage

        agent = _agent()
        agent["weird_string_field"] = "should be ignored"
        storage.save_period([agent], source="csv")

        loaded = storage.load_period("2026-04")
        assert "weird_string_field" not in loaded[0]
        assert loaded[0]["csat"] == 0.85  # numeric metrics still saved

    def test_meta_upsert_updates_email(self, isolated_db):
        """Re-ingesting an agent with a new email updates agent_meta."""
        from src import storage

        storage.save_period([_agent(email="old@x.com")], source="csv")
        storage.save_period(
            [_agent(period="2026-05", email="new@x.com")],
            source="csv",
        )

        loaded = storage.load_period("2026-05")
        assert loaded[0]["email"] == "new@x.com"


# ── runs lifecycle ────────────────────────────────────────────────────────────


class TestRuns:
    def test_start_finish_run_round_trip(self, isolated_db):
        from src import storage

        run_id = storage.start_run(source="fub", period="2026-04")
        assert isinstance(run_id, int) and run_id > 0

        storage.finish_run(run_id, "ok", "completed cleanly")

        with storage.connect() as conn:
            row = conn.execute("SELECT status, notes FROM runs WHERE id = ?", (run_id,)).fetchone()
        assert row["status"] == "ok"
        assert row["notes"] == "completed cleanly"

    def test_finish_run_with_error_status(self, isolated_db):
        from src import storage

        run_id = storage.start_run(source="fub")
        storage.finish_run(run_id, "error", "boom")

        with storage.connect() as conn:
            row = conn.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()
        assert row["status"] == "error"

    def test_finish_run_rejects_invalid_status(self, isolated_db):
        from src import storage

        run_id = storage.start_run(source="fub")
        with pytest.raises(ValueError, match="invalid status"):
            storage.finish_run(run_id, "weird", None)

    def test_get_active_run_returns_running_row(self, isolated_db):
        from src import storage

        run_id = storage.start_run(source="fub")
        active = storage.get_active_run()

        assert active is not None
        assert active["id"] == run_id
        assert active["status"] == "running"

    def test_get_active_run_returns_none_when_no_running(self, isolated_db):
        from src import storage

        run_id = storage.start_run(source="fub")
        storage.finish_run(run_id, "ok")
        assert storage.get_active_run() is None

    def test_latest_run_unfiltered(self, isolated_db):
        from src import storage

        a = storage.start_run(source="csv")
        storage.finish_run(a, "ok")
        b = storage.start_run(source="fub")
        storage.finish_run(b, "ok")

        latest = storage.latest_run()
        assert latest["id"] == b

    def test_latest_run_filtered_by_source(self, isolated_db):
        from src import storage

        a = storage.start_run(source="csv")
        storage.finish_run(a, "ok")
        b = storage.start_run(source="fub")
        storage.finish_run(b, "ok")
        c = storage.start_run(source="csv")
        storage.finish_run(c, "ok")

        assert storage.latest_run(source="csv")["id"] == c
        assert storage.latest_run(source="fub")["id"] == b

    def test_latest_run_returns_none_when_empty(self, isolated_db):
        from src import storage

        assert storage.latest_run() is None


# ── load_period / list_periods ────────────────────────────────────────────────


class TestLoadAndList:
    def test_list_periods_returns_descending(self, isolated_db):
        from src import storage

        storage.save_period([_agent(period="2026-03")], source="csv")
        storage.save_period([_agent(period="2026-05")], source="csv")
        storage.save_period([_agent(period="2026-04")], source="csv")

        periods = storage.list_periods()
        assert periods == ["2026-05", "2026-04", "2026-03"]

    def test_load_missing_period_returns_empty(self, isolated_db):
        from src import storage

        assert storage.load_period("1999-01") == []

    def test_load_period_normalizes_input(self, isolated_db):
        from src import storage

        storage.save_period([_agent(period="2026-04")], source="csv")

        # Both representations should hit the same canonical period
        assert len(storage.load_period("2026-04")) == 1
        assert len(storage.load_period("April 2026")) == 1


# ── load_history / team_history ───────────────────────────────────────────────


class TestHistory:
    def test_load_history_returns_recent_periods_newest_first(self, isolated_db):
        from src import storage

        for period, val in [("2026-02", 0.7), ("2026-03", 0.8), ("2026-04", 0.9)]:
            storage.save_period([_agent(period=period, csat=val)], source="csv")

        history = storage.load_history("100", "csat", window_months=3)
        assert history == [("2026-04", 0.9), ("2026-03", 0.8), ("2026-02", 0.7)]

    def test_load_history_window_truncation(self, isolated_db):
        from src import storage

        for period, val in [("2026-02", 0.7), ("2026-03", 0.8), ("2026-04", 0.9)]:
            storage.save_period([_agent(period=period, csat=val)], source="csv")

        history = storage.load_history("100", "csat", window_months=2)
        assert len(history) == 2
        assert history[0] == ("2026-04", 0.9)

    def test_team_history_averages(self, isolated_db):
        from src import storage

        # Two agents, two periods
        storage.save_period(
            [_agent("100", "2026-04", csat=0.8), _agent("200", "2026-04", "Bob", "b@x", csat=0.6)],
            source="csv",
        )
        storage.save_period(
            [_agent("100", "2026-05", csat=0.9), _agent("200", "2026-05", "Bob", "b@x", csat=0.7)],
            source="csv",
        )

        team = storage.team_history("csat", window_months=2)

        # Returned oldest → newest per the docstring
        assert list(team.keys()) == ["2026-04", "2026-05"]
        assert team["2026-04"] == pytest.approx(0.7)
        assert team["2026-05"] == pytest.approx(0.8)


# ── Draft queue: state machine ────────────────────────────────────────────────


class TestDraftStateMachine:
    def _seed_agent(self, storage):
        storage.save_period([_agent()], source="csv")

    def test_queue_then_list_pending(self, isolated_db):
        from src import storage

        self._seed_agent(storage)
        storage.queue_draft("100", "2026-04", "<html>v1</html>")

        pending = storage.list_drafts(status="pending")
        assert len(pending) == 1
        assert pending[0]["agent_id"] == "100"
        assert pending[0]["status"] == "pending"

    def test_queue_replaces_existing_draft_for_agent_period(self, isolated_db):
        from src import storage

        self._seed_agent(storage)
        first_id = storage.queue_draft("100", "2026-04", "<html>v1</html>")
        second_id = storage.queue_draft("100", "2026-04", "<html>v2</html>")

        # Same row id (UNIQUE upsert)
        assert first_id == second_id

        full = storage.get_draft(first_id)
        assert "v2" in full["html"]

    def test_approve_then_send(self, isolated_db):
        from src import storage

        self._seed_agent(storage)
        draft_id = storage.queue_draft("100", "2026-04", "<html>x</html>")

        storage.approve_draft(draft_id)
        approved = storage.list_drafts(status="approved")
        assert len(approved) == 1
        assert approved[0]["approved_at"] is not None

        storage.mark_sent(draft_id)
        sent = storage.list_drafts(status="sent")
        assert len(sent) == 1
        assert sent[0]["sent_at"] is not None
        assert storage.list_drafts(status="approved") == []

    def test_reject_moves_to_rejected_status(self, isolated_db):
        from src import storage

        self._seed_agent(storage)
        draft_id = storage.queue_draft("100", "2026-04", "<html/>")

        storage.reject_draft(draft_id)
        rejected = storage.list_drafts(status="rejected")
        assert len(rejected) == 1
        assert storage.list_drafts(status="pending") == []

    def test_get_draft_returns_full_row_with_meta(self, isolated_db):
        from src import storage

        self._seed_agent(storage)
        draft_id = storage.queue_draft("100", "2026-04", "<html>full body</html>")

        full = storage.get_draft(draft_id)
        assert full is not None
        assert full["html"] == "<html>full body</html>"
        assert full["name"] == "Alice"
        assert full["email"] == "alice@x.com"

    def test_get_draft_returns_none_for_missing_id(self, isolated_db):
        from src import storage

        assert storage.get_draft(99999) is None

    def test_approve_all_marks_only_pending(self, isolated_db):
        from src import storage

        # Two pending drafts in one period, one already approved
        storage.save_period(
            [
                _agent("100", "2026-04"),
                _agent("200", "2026-04", "Bob", "b@x"),
                _agent("300", "2026-04", "Carol", "c@x"),
            ],
            source="csv",
        )
        d1 = storage.queue_draft("100", "2026-04", "<html/>")
        storage.queue_draft("200", "2026-04", "<html/>")
        storage.queue_draft("300", "2026-04", "<html/>")
        storage.approve_draft(d1)  # pre-approve one

        rowcount = storage.approve_all("2026-04")

        assert rowcount == 2  # only the two still-pending
        approved = storage.list_drafts(status="approved")
        assert len(approved) == 3

    def test_list_drafts_filtered_by_period(self, isolated_db):
        from src import storage

        storage.save_period([_agent(period="2026-03"), _agent(period="2026-04")], source="csv")
        storage.queue_draft("100", "2026-03", "<html>3</html>")
        storage.queue_draft("100", "2026-04", "<html>4</html>")

        only_march = storage.list_drafts(period="2026-03")
        assert len(only_march) == 1
        assert only_march[0]["period"] == "2026-03"


# ── Connection management ─────────────────────────────────────────────────────


class TestConnection:
    def test_connect_yields_row_factory(self, isolated_db):
        from src import storage

        with storage.connect() as conn:
            row = conn.execute("SELECT 1 AS x").fetchone()
        assert row["x"] == 1
