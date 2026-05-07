"""
SQLite persistence for monthly metrics, ingest audit, and the draft-approval
queue.

Tables
------
agent_periods   one row per (agent_id, period, metric_key) — long format so the
                dynamic metric registry can change month to month.
runs            audit log of CSV/JSON ingests.
drafts          approval queue: rendered HTML emails awaiting admin approval.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime

from config.settings import BASE_DIR

log = logging.getLogger(__name__)

DB_PATH = BASE_DIR / "data" / "metrics.db"


SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_periods (
    agent_id     TEXT NOT NULL,
    period       TEXT NOT NULL,           -- e.g. "2026-04"
    metric_key   TEXT NOT NULL,
    value        REAL,
    raw_json     TEXT,
    ingested_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (agent_id, period, metric_key)
);

CREATE TABLE IF NOT EXISTS agent_meta (
    agent_id     TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    email        TEXT NOT NULL,
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    period       TEXT NOT NULL,
    source       TEXT NOT NULL,           -- 'csv' | 'json' | 'fub' | 'mock'
    file_path    TEXT,
    row_count    INTEGER,
    status       TEXT NOT NULL,           -- 'ok' | 'error'
    notes        TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS drafts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id     TEXT NOT NULL,
    period       TEXT NOT NULL,
    html         TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending|approved|sent|rejected
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    approved_at  TEXT,
    sent_at      TEXT,
    UNIQUE (agent_id, period)
);

CREATE INDEX IF NOT EXISTS idx_periods_agent ON agent_periods(agent_id, period);
CREATE INDEX IF NOT EXISTS idx_drafts_status ON drafts(period, status);
"""


# ── Connection management ─────────────────────────────────────────────────────


def _ensure_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(SCHEMA)


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    _ensure_db()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ── Period normalization ──────────────────────────────────────────────────────


def normalize_period(period: str) -> str:
    """
    Accept "April 2026", "2026-04", "2026-04-15" → return canonical "2026-04".
    """
    period = period.strip()
    try:
        # Already YYYY-MM or YYYY-MM-DD
        return datetime.strptime(period[:7], "%Y-%m").strftime("%Y-%m")
    except ValueError:
        pass
    for fmt in ("%B %Y", "%b %Y"):
        try:
            return datetime.strptime(period, fmt).strftime("%Y-%m")
        except ValueError:
            continue
    raise ValueError(f"Unrecognized period format: {period!r}")


# ── Saving ingested data ──────────────────────────────────────────────────────


def save_period(
    agents: list[dict],
    source: str,
    file_path: str | None = None,
    run_id: int | None = None,
) -> int:
    """
    Persist a list of normalized agent records to SQLite.

    Each agent dict should contain agent_id, name, email, period, plus one key
    per metric defined in thresholds.json. Extra keys are ignored. Inserts are
    upserts on (agent_id, period, metric_key).

    If ``run_id`` is provided, that existing row is updated to status='ok' with
    the final row_count. Otherwise a new run row is inserted. The two-call
    pattern lets the dashboard's manual-pull flow track an in-progress job
    before the save_period payload exists.

    Returns the run id.
    """
    if not agents:
        raise ValueError("save_period: no agent records provided")

    period = normalize_period(agents[0]["period"])
    standard_keys = {
        "agent_id",
        "name",
        "email",
        "period",
        "start_date",
        "end_date",
        "_raw",
        "_error",
    }

    with connect() as conn:
        cur = conn.cursor()

        if run_id is None:
            cur.execute(
                """
                INSERT INTO runs (period, source, file_path, row_count, status)
                VALUES (?, ?, ?, ?, 'ok')
                """,
                (period, source, file_path, len(agents)),
            )
            run_id = cur.lastrowid
        else:
            cur.execute(
                """
                UPDATE runs
                SET period = ?, row_count = ?, status = 'ok'
                WHERE id = ?
                """,
                (period, len(agents), run_id),
            )

        for agent in agents:
            cur.execute(
                """
                INSERT INTO agent_meta (agent_id, name, email, updated_at)
                VALUES (?, ?, ?, datetime('now'))
                ON CONFLICT(agent_id) DO UPDATE SET
                    name = excluded.name,
                    email = excluded.email,
                    updated_at = excluded.updated_at
                """,
                (agent["agent_id"], agent["name"], agent["email"]),
            )

            for key, value in agent.items():
                if key in standard_keys:
                    continue
                if not isinstance(value, (int, float, type(None))):
                    continue
                cur.execute(
                    """
                    INSERT INTO agent_periods (agent_id, period, metric_key, value, raw_json)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(agent_id, period, metric_key) DO UPDATE SET
                        value = excluded.value,
                        raw_json = excluded.raw_json,
                        ingested_at = datetime('now')
                    """,
                    (
                        agent["agent_id"],
                        period,
                        key,
                        float(value) if value is not None else None,
                        json.dumps({"original_period": agent["period"]}),
                    ),
                )

        log.info("Saved %d agents for period %s (run #%d)", len(agents), period, run_id)
        return run_id


# ── Run lifecycle (manual-pull / cron tracking) ───────────────────────────────


def start_run(source: str, period: str | None = None) -> int:
    """
    Create a runs row with status='running'. Returns its id. Use for the
    manual-pull background thread and the cron pipeline so the dashboard can
    show "in progress" state while an actual save_period() hasn't happened yet.
    """
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO runs (period, source, status, row_count)
            VALUES (?, ?, 'running', 0)
            """,
            (period or "", source),
        )
        return cur.lastrowid


def finish_run(run_id: int, status: str, notes: str | None = None) -> None:
    """
    Terminal update for a run row. ``status`` is 'ok' or 'error'. If
    save_period() was called with run_id, it has already moved the row to 'ok'
    — calling finish_run('ok') again is harmless. For failures, call this
    instead of save_period() so the running state clears.
    """
    if status not in ("ok", "error"):
        raise ValueError(f"finish_run: invalid status {status!r}")
    with connect() as conn:
        conn.execute(
            "UPDATE runs SET status = ?, notes = ? WHERE id = ?",
            (status, notes, run_id),
        )


def get_active_run() -> dict | None:
    """Return the currently-running run row, or None. Newest wins on ties."""
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM runs WHERE status = 'running' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


def latest_run(source: str | None = None) -> dict | None:
    """Return the most recent run, optionally filtered by source ('fub' etc)."""
    sql = "SELECT * FROM runs"
    params: list = []
    if source:
        sql += " WHERE source = ?"
        params.append(source)
    sql += " ORDER BY id DESC LIMIT 1"
    with connect() as conn:
        row = conn.execute(sql, params).fetchone()
    return dict(row) if row else None


# ── Reading history ───────────────────────────────────────────────────────────


def load_period(period: str) -> list[dict]:
    """
    Load all agents' metrics for a given period back into the same shape that
    csv_ingest / fub_client produce (suitable for score_all_agents).
    """
    period = normalize_period(period)
    with connect() as conn:
        meta_rows = conn.execute(
            """
            SELECT DISTINCT m.agent_id, m.name, m.email
            FROM agent_meta m
            JOIN agent_periods p ON p.agent_id = m.agent_id
            WHERE p.period = ?
            """,
            (period,),
        ).fetchall()

        results = []
        for m in meta_rows:
            metric_rows = conn.execute(
                "SELECT metric_key, value FROM agent_periods WHERE agent_id=? AND period=?",
                (m["agent_id"], period),
            ).fetchall()
            record = {
                "agent_id": m["agent_id"],
                "name": m["name"],
                "email": m["email"],
                "period": period_label(period),
                "_raw": {},
            }
            for r in metric_rows:
                record[r["metric_key"]] = r["value"]
            results.append(record)
        return results


def load_history(
    agent_id: str, metric_key: str, window_months: int = 3
) -> list[tuple[str, float | None]]:
    """Return last N (period, value) rows for one agent + metric, newest first."""
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT period, value FROM agent_periods
            WHERE agent_id = ? AND metric_key = ?
            ORDER BY period DESC
            LIMIT ?
            """,
            (agent_id, metric_key, window_months),
        ).fetchall()
    return [(r["period"], r["value"]) for r in rows]


def team_history(metric_key: str, window_months: int = 3) -> dict[str, float]:
    """
    Return team average for a metric per period.
    {period: avg_value}, sorted oldest → newest.
    """
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT period, AVG(value) AS avg_value
            FROM agent_periods
            WHERE metric_key = ? AND value IS NOT NULL
            GROUP BY period
            ORDER BY period DESC
            LIMIT ?
            """,
            (metric_key, window_months),
        ).fetchall()
    return {r["period"]: r["avg_value"] for r in reversed(rows)}


def list_periods() -> list[str]:
    """All distinct periods in the database, newest first."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT period FROM agent_periods ORDER BY period DESC"
        ).fetchall()
    return [r["period"] for r in rows]


# ── Draft approval queue ──────────────────────────────────────────────────────


def queue_draft(agent_id: str, period: str, html: str) -> int:
    period = normalize_period(period)
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO drafts (agent_id, period, html, status)
            VALUES (?, ?, ?, 'pending')
            ON CONFLICT(agent_id, period) DO UPDATE SET
                html = excluded.html,
                status = 'pending',
                created_at = datetime('now'),
                approved_at = NULL,
                sent_at = NULL
            RETURNING id
            """,
            (agent_id, period, html),
        )
        return cur.fetchone()[0]


def list_drafts(period: str | None = None, status: str | None = None) -> list[dict]:
    sql = """
        SELECT d.id, d.agent_id, m.name, m.email, d.period, d.status,
               d.created_at, d.approved_at, d.sent_at
        FROM drafts d
        LEFT JOIN agent_meta m ON m.agent_id = d.agent_id
        WHERE 1=1
    """
    params: list = []
    if period:
        sql += " AND d.period = ?"
        params.append(normalize_period(period))
    if status:
        sql += " AND d.status = ?"
        params.append(status)
    sql += " ORDER BY d.period DESC, m.name"

    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_draft(draft_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT d.*, m.name, m.email
            FROM drafts d
            LEFT JOIN agent_meta m ON m.agent_id = d.agent_id
            WHERE d.id = ?
            """,
            (draft_id,),
        ).fetchone()
    return dict(row) if row else None


def approve_draft(draft_id: int) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE drafts SET status='approved', approved_at=datetime('now') WHERE id=?",
            (draft_id,),
        )


def reject_draft(draft_id: int) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE drafts SET status='rejected' WHERE id=?",
            (draft_id,),
        )


def mark_sent(draft_id: int) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE drafts SET status='sent', sent_at=datetime('now') WHERE id=?",
            (draft_id,),
        )


def approve_all(period: str) -> int:
    period = normalize_period(period)
    with connect() as conn:
        cur = conn.execute(
            "UPDATE drafts SET status='approved', approved_at=datetime('now') "
            "WHERE period=? AND status='pending'",
            (period,),
        )
        return cur.rowcount


# ── Helpers ───────────────────────────────────────────────────────────────────


def period_label(canonical: str) -> str:
    """'2026-04' -> 'April 2026' for display."""
    try:
        return datetime.strptime(canonical, "%Y-%m").strftime("%B %Y")
    except ValueError:
        return canonical


# Backwards-compat alias for internal callers.
_period_label = period_label


__all__ = [
    "DB_PATH",
    "normalize_period",
    "period_label",
    "save_period",
    "start_run",
    "finish_run",
    "get_active_run",
    "latest_run",
    "load_period",
    "load_history",
    "team_history",
    "list_periods",
    "queue_draft",
    "list_drafts",
    "get_draft",
    "approve_draft",
    "reject_draft",
    "mark_sent",
    "approve_all",
]
