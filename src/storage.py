"""
SQLite persistence for the agent dashboard.

Schema (see init_schema()):
- agents          : roster mirror keyed by email; populated by daily sync
- metric_snapshots: one row per agent per day; current month + month-end history
- magic_links    : one-time login tokens
- sessions       : long-lived browser sessions (HTTP-only cookie)

All public functions accept an optional `db_path` so tests can run against an
isolated tmp file without touching the real database.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import secrets
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

from config.settings import DATABASE_PATH

log = logging.getLogger(__name__)


SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    email         TEXT NOT NULL UNIQUE,
    fub_agent_id  TEXT,
    slug          TEXT NOT NULL UNIQUE,
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS metric_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id        INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    period          TEXT NOT NULL,
    as_of_date      TEXT NOT NULL,
    pcvr            REAL,
    pickup_rate     REAL,
    csat            REAL,
    zhl_transfers   INTEGER,
    overall_status  TEXT,
    raw_json        TEXT NOT NULL,
    UNIQUE(agent_id, period, as_of_date)
);
CREATE INDEX IF NOT EXISTS idx_snap_agent_period
    ON metric_snapshots(agent_id, period);

CREATE TABLE IF NOT EXISTS magic_links (
    token       TEXT PRIMARY KEY,
    email       TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    used_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_magic_email ON magic_links(email);

CREATE TABLE IF NOT EXISTS sessions (
    token       TEXT PRIMARY KEY,
    agent_id    INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    expires_at  TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_session_agent ON sessions(agent_id);
"""


# ── Connection / schema ───────────────────────────────────────────────────────

def connect(db_path: Optional[Path] = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else DATABASE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(db_path: Optional[Path] = None) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
        conn.commit()


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


# ── Agents ────────────────────────────────────────────────────────────────────

def upsert_agents(roster: Iterable[dict], db_path: Optional[Path] = None) -> None:
    """Insert/update roster rows. `active=0` flag deactivates without deleting."""
    with connect(db_path) as conn:
        existing_emails = {
            row["email"] for row in conn.execute("SELECT email FROM agents")
        }
        roster_emails = set()
        for entry in roster:
            email = entry["email"].strip().lower()
            roster_emails.add(email)
            slug = _slugify(entry["name"])
            conn.execute(
                """
                INSERT INTO agents (name, email, fub_agent_id, slug, active)
                VALUES (?, ?, ?, ?, 1)
                ON CONFLICT(email) DO UPDATE SET
                    name = excluded.name,
                    fub_agent_id = excluded.fub_agent_id,
                    slug = excluded.slug,
                    active = 1
                """,
                (entry["name"], email, entry.get("fub_agent_id"), slug),
            )
        # Soft-deactivate agents that disappeared from the roster
        for stale in existing_emails - roster_emails:
            conn.execute("UPDATE agents SET active = 0 WHERE email = ?", (stale,))
        conn.commit()


def get_agent_by_email(email: str, db_path: Optional[Path] = None) -> Optional[dict]:
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM agents WHERE email = ? AND active = 1",
            (email.strip().lower(),),
        ).fetchone()
    return dict(row) if row else None


def get_agent_by_id(agent_id: int, db_path: Optional[Path] = None) -> Optional[dict]:
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM agents WHERE id = ?", (agent_id,)
        ).fetchone()
    return dict(row) if row else None


# ── Snapshots ─────────────────────────────────────────────────────────────────

def write_snapshot(scored_agent: dict, db_path: Optional[Path] = None) -> int:
    """
    Upsert today's snapshot for one agent. Returns the snapshot row id.

    `scored_agent` is the dict produced by metrics.score_agent — it must include
    name, email, period, metrics{pCVR/pickup_rate/csat/zhl_transfers},
    overall_status. Date is derived from `as_of` if present, else today.
    """
    metrics = scored_agent.get("metrics", {})
    today = scored_agent.get("as_of_date") or date.today().isoformat()
    period = _period_from_scored(scored_agent)

    with connect(db_path) as conn:
        agent_row = conn.execute(
            "SELECT id FROM agents WHERE email = ?",
            (scored_agent["email"].strip().lower(),),
        ).fetchone()
        if not agent_row:
            slug = _slugify(scored_agent["name"])
            cursor = conn.execute(
                """
                INSERT INTO agents (name, email, fub_agent_id, slug, active)
                VALUES (?, ?, ?, ?, 1)
                """,
                (scored_agent["name"], scored_agent["email"].strip().lower(),
                 scored_agent.get("agent_id"), slug),
            )
            agent_id = cursor.lastrowid
        else:
            agent_id = agent_row["id"]

        cursor = conn.execute(
            """
            INSERT INTO metric_snapshots
                (agent_id, period, as_of_date, pcvr, pickup_rate, csat,
                 zhl_transfers, overall_status, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(agent_id, period, as_of_date) DO UPDATE SET
                pcvr = excluded.pcvr,
                pickup_rate = excluded.pickup_rate,
                csat = excluded.csat,
                zhl_transfers = excluded.zhl_transfers,
                overall_status = excluded.overall_status,
                raw_json = excluded.raw_json
            """,
            (
                agent_id, period, today,
                _metric_value(metrics, "pCVR"),
                _metric_value(metrics, "pickup_rate"),
                _metric_value(metrics, "csat"),
                _metric_value(metrics, "zhl_transfers"),
                scored_agent.get("overall_status"),
                json.dumps(scored_agent, default=str),
            ),
        )
        conn.commit()
        return cursor.lastrowid


def latest_snapshot(agent_id: int, db_path: Optional[Path] = None) -> Optional[dict]:
    """Most-recent snapshot for an agent (any period)."""
    with connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT * FROM metric_snapshots
            WHERE agent_id = ?
            ORDER BY as_of_date DESC, id DESC
            LIMIT 1
            """,
            (agent_id,),
        ).fetchone()
    return _hydrate_snapshot(row) if row else None


def trend_snapshots(
    agent_id: int,
    months: int,
    db_path: Optional[Path] = None,
) -> list[dict]:
    """
    Last N months of history for trend charts. For each prior period we keep the
    final (max as_of_date) snapshot — that's the canonical month value. The
    current period is also included with its latest snapshot.
    """
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT s.* FROM metric_snapshots s
            JOIN (
                SELECT period, MAX(as_of_date) AS latest_date
                FROM metric_snapshots
                WHERE agent_id = ?
                GROUP BY period
                ORDER BY period DESC
                LIMIT ?
            ) m ON s.period = m.period AND s.as_of_date = m.latest_date
            WHERE s.agent_id = ?
            ORDER BY s.period ASC
            """,
            (agent_id, months, agent_id),
        ).fetchall()
    return [_hydrate_snapshot(r) for r in rows]


def _hydrate_snapshot(row: sqlite3.Row) -> dict:
    out = dict(row)
    if out.get("raw_json"):
        try:
            out["raw"] = json.loads(out["raw_json"])
        except json.JSONDecodeError:
            out["raw"] = None
    return out


def _metric_value(metrics: dict, key: str):
    m = metrics.get(key) or {}
    return m.get("value")


def _period_from_scored(scored_agent: dict) -> str:
    """Return 'YYYY-MM' for a scored agent. Falls back to today's month."""
    period_raw = scored_agent.get("period")
    if period_raw:
        # Existing pipeline gives 'March 2026' — convert to '2026-03'.
        try:
            return datetime.strptime(period_raw, "%B %Y").strftime("%Y-%m")
        except ValueError:
            # Already YYYY-MM or other shape — accept as-is if it looks right.
            if re.fullmatch(r"\d{4}-\d{2}", period_raw):
                return period_raw
    return date.today().strftime("%Y-%m")


# ── Magic links ───────────────────────────────────────────────────────────────

def create_magic_link(
    email: str,
    ttl_minutes: int,
    db_path: Optional[Path] = None,
) -> str:
    """Insert a magic-link row and return the random token."""
    token = secrets.token_urlsafe(32)
    expires = (
        datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)
    ).isoformat()
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO magic_links (token, email, expires_at) VALUES (?, ?, ?)",
            (token, email.strip().lower(), expires),
        )
        conn.commit()
    return token


def consume_magic_link(token: str, db_path: Optional[Path] = None) -> Optional[str]:
    """
    Atomically mark a magic-link token used. Returns the email it was issued
    for, or None if the token is missing, expired, or already consumed.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    with connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT email, expires_at, used_at FROM magic_links WHERE token = ?
            """,
            (token,),
        ).fetchone()
        if not row or row["used_at"] is not None or row["expires_at"] < now_iso:
            return None
        conn.execute(
            "UPDATE magic_links SET used_at = ? WHERE token = ? AND used_at IS NULL",
            (now_iso, token),
        )
        conn.commit()
        return row["email"]


# ── Sessions ──────────────────────────────────────────────────────────────────

def create_session(
    agent_id: int,
    ttl_days: int,
    db_path: Optional[Path] = None,
) -> str:
    token = secrets.token_urlsafe(32)
    expires = (
        datetime.now(timezone.utc) + timedelta(days=ttl_days)
    ).isoformat()
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO sessions (token, agent_id, expires_at) VALUES (?, ?, ?)",
            (token, agent_id, expires),
        )
        conn.commit()
    return token


def lookup_session(token: str, db_path: Optional[Path] = None) -> Optional[dict]:
    """Return the agent dict for a valid session token, else None."""
    if not token:
        return None
    now_iso = datetime.now(timezone.utc).isoformat()
    with connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT a.* FROM sessions s
            JOIN agents a ON a.id = s.agent_id
            WHERE s.token = ? AND s.expires_at > ? AND a.active = 1
            """,
            (token, now_iso),
        ).fetchone()
    return dict(row) if row else None


def delete_session(token: str, db_path: Optional[Path] = None) -> None:
    with connect(db_path) as conn:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        conn.commit()


# ── Admin helpers ─────────────────────────────────────────────────────────────

def seed_history(email: str, db_path: Optional[Path] = None) -> int:
    """
    Insert six months of synthetic history for one agent so trend charts can be
    eyeballed during local QA. Returns the number of rows inserted.
    """
    agent = get_agent_by_email(email, db_path=db_path)
    if not agent:
        raise ValueError(f"No active agent with email {email!r}")

    today = date.today().replace(day=1)
    rows_written = 0
    base = {"pcvr": 0.030, "pickup_rate": 0.78, "csat": 4.2, "zhl_transfers": 2}
    for offset in range(5, -1, -1):
        year = today.year
        month = today.month - offset
        while month <= 0:
            month += 12
            year -= 1
        period = f"{year:04d}-{month:02d}"
        as_of = date(year, month, 28).isoformat()
        scored_stub = {
            "name": agent["name"],
            "email": agent["email"],
            "agent_id": agent.get("fub_agent_id"),
            "period": period,
            "as_of_date": as_of,
            "metrics": {
                "pCVR":          {"value": round(base["pcvr"] + 0.002 * offset, 4)},
                "pickup_rate":   {"value": round(base["pickup_rate"] + 0.01 * offset, 3)},
                "csat":          {"value": round(base["csat"] + 0.05 * offset, 2)},
                "zhl_transfers": {"value": base["zhl_transfers"] + offset},
            },
            "overall_status": "Preferred" if offset == 0 else "At Risk",
        }
        write_snapshot(scored_stub, db_path=db_path)
        rows_written += 1
    return rows_written


# ── CLI ───────────────────────────────────────────────────────────────────────

def _cli(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Storage admin helpers")
    parser.add_argument("--init", action="store_true", help="Create schema if missing")
    parser.add_argument(
        "--seed-history",
        metavar="EMAIL",
        help="Insert 6 months of fake snapshots for one agent (QA only)",
    )
    args = parser.parse_args(argv)

    if args.init:
        init_schema()
        print(f"Initialized schema at {DATABASE_PATH}")
    if args.seed_history:
        n = seed_history(args.seed_history)
        print(f"Inserted {n} historical snapshots for {args.seed_history}")
    if not (args.init or args.seed_history):
        parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
