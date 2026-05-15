"""
FUB Daily Metrics Calculator.

Pulls live agent activity data from Follow Up Boss API and calculates
daily approximations of Zillow Preferred performance metrics.

This module provides an operational activity view that lives alongside
the monthly scored metrics. Metrics that map to the existing scoring
engine (response time, appointment rate) use the same definitions;
supplemental activity metrics (call volume, text activity, contact rate)
are added for daily visibility.
"""

from __future__ import annotations

import base64
import logging
import os
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import requests

from config.settings import FUB_API_KEY, FUB_BASE_URL, FUB_TIMEOUT_SECONDS

log = logging.getLogger(__name__)

# Zillow Preferred targets (used for color-coding on dashboard)
TARGETS = {
    "response_time_sec": 300,  # < 5 minutes = green
    "contact_rate": 0.80,  # > 80% = green
    "appointment_rate": 0.20,  # > 20% = green
    "calls_per_lead": 2.0,  # >= 2 calls per lead = green
    "texts_per_lead": 3.0,  # >= 3 texts per lead = green
}

ZILLOW_SOURCE_ID = 14
ZILLOW_SOURCE_NAME = "Zillow Preferred"


# ── FUB API helpers ──────────────────────────────────────────────


def _auth_header() -> dict:
    """HTTP Basic auth with API key as username, empty password."""
    key = FUB_API_KEY or os.environ.get("FUB_API_KEY", "")
    if not key:
        raise OSError("FUB_API_KEY is not set.")
    token = base64.b64encode(f"{key}:".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _get(endpoint: str, params: dict | None = None) -> dict:
    """GET request to FUB API with retry on 429."""
    url = f"{FUB_BASE_URL}{endpoint}"
    headers = _auth_header()
    for attempt in range(3):
        resp = requests.get(url, headers=headers, params=params, timeout=FUB_TIMEOUT_SECONDS)
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 5))
            log.warning("FUB 429 — waiting %ds (attempt %d)", wait, attempt + 1)
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError(f"FUB API rate-limited after 3 retries: {endpoint}")


def _paginate(endpoint: str, params: dict, collection_key: str) -> list[dict]:
    """Paginate through FUB API results."""
    all_items: list[dict] = []
    offset = 0
    limit = 100
    while True:
        p = {**params, "limit": limit, "offset": offset}
        data = _get(endpoint, p)
        items = data.get(collection_key, [])
        all_items.extend(items)
        meta = data.get("_metadata", {})
        total = meta.get("total", len(items))
        if offset + limit >= total or not items:
            break
        offset += limit
    return all_items


# ── Data fetchers ────────────────────────────────────────────────


def fetch_active_agents() -> list[dict]:
    """Fetch all active agents from FUB."""
    users = _paginate("/users", {}, "users")
    return [
        u for u in users if u.get("status") == "Active" and u.get("role") in ("Agent", "Broker")
    ]


def fetch_agent_leads(agent_id: int, days: int = 30) -> list[dict]:
    """Fetch leads assigned to an agent created in the last N days."""
    since = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d")
    return _paginate(
        "/people",
        {
            "assignedUserId": agent_id,
            "created": f">{since}",
            "sort": "-created",
            "fields": "allFields",
        },
        "people",
    )


def is_zillow_lead(lead: dict) -> bool:
    """Check if a lead came from Zillow Preferred."""
    return lead.get("sourceId") == ZILLOW_SOURCE_ID or (
        lead.get("source") or ""
    ).lower().startswith("zillow")


# ── Metric calculations ─────────────────────────────────────────


def _parse_dt(val: Any) -> datetime | None:
    """Parse a FUB datetime string to UTC datetime."""
    if not val or val == "0":
        return None
    try:
        if isinstance(val, str):
            # FUB uses ISO format with Z suffix
            return datetime.fromisoformat(val.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return None


def calc_response_time(lead: dict) -> float | None:
    """
    Calculate time-to-first-contact in seconds.
    Uses the earliest of: first call, first sent text, first sent email.
    Returns None if no contact was made.
    """
    created = _parse_dt(lead.get("created"))
    if not created:
        return None

    candidates: list[datetime] = []

    # First call — FUB stores firstCall as duration in seconds, not a timestamp.
    # Use lastOutgoingCall as the best proxy for first contact attempt.
    first_call = _parse_dt(lead.get("lastOutgoingCall"))
    if first_call:
        candidates.append(first_call)

    first_text = _parse_dt(lead.get("lastSentText"))
    if first_text:
        candidates.append(first_text)

    first_email = _parse_dt(lead.get("lastSentEmail"))
    if first_email:
        candidates.append(first_email)

    if not candidates:
        return None

    earliest = min(candidates)
    delta = (earliest - created).total_seconds()
    return max(0, delta)  # Clamp to non-negative


def calc_agent_metrics(leads: list[dict]) -> dict:
    """
    Calculate all daily metrics for one agent's leads.
    Only considers Zillow Preferred leads.
    """
    zillow_leads = [ld for ld in leads if is_zillow_lead(ld)]
    total = len(zillow_leads)

    if total == 0:
        return {
            "total_zillow_leads": 0,
            "total_all_leads": len(leads),
            "response_time_avg": None,
            "response_time_median": None,
            "contact_rate": None,
            "calls_outgoing": 0,
            "calls_per_lead": 0,
            "texts_sent": 0,
            "texts_per_lead": 0,
            "emails_sent": 0,
            "appointment_rate": None,
            "lead_acceptance_rate": None,
        }

    # Response times
    response_times = []
    for lead in zillow_leads:
        rt = calc_response_time(lead)
        if rt is not None:
            response_times.append(rt)

    rt_avg = sum(response_times) / len(response_times) if response_times else None
    rt_sorted = sorted(response_times)
    rt_median = rt_sorted[len(rt_sorted) // 2] if rt_sorted else None

    # Contact rate
    contacted = sum(1 for ld in zillow_leads if ld.get("contacted") == 1)
    contact_rate = contacted / total

    # Call volume
    calls_out = sum(ld.get("callsOutgoing", 0) or 0 for ld in zillow_leads)
    calls_per_lead = calls_out / total

    # Text volume
    texts = sum(ld.get("textsSent", 0) or 0 for ld in zillow_leads)
    texts_per_lead = texts / total

    # Email volume
    emails = sum(ld.get("emailsSent", 0) or 0 for ld in zillow_leads)

    # Appointment rate (stageId >= 29 means "Appointment set" or beyond)
    appointments = sum(1 for ld in zillow_leads if (ld.get("stageId") or 0) >= 29)
    appointment_rate = appointments / total

    # Lead acceptance (moved past "New" stage, stageId > 26)
    accepted = sum(1 for ld in zillow_leads if (ld.get("stageId") or 0) > 26)
    lead_acceptance_rate = accepted / total

    return {
        "total_zillow_leads": total,
        "total_all_leads": len(leads),
        "response_time_avg": round(rt_avg, 1) if rt_avg is not None else None,
        "response_time_median": round(rt_median, 1) if rt_median is not None else None,
        "contact_rate": round(contact_rate, 3),
        "calls_outgoing": calls_out,
        "calls_per_lead": round(calls_per_lead, 2),
        "texts_sent": texts,
        "texts_per_lead": round(texts_per_lead, 2),
        "emails_sent": emails,
        "appointment_rate": round(appointment_rate, 3),
        "lead_acceptance_rate": round(lead_acceptance_rate, 3),
    }


# ── Main orchestrator ────────────────────────────────────────────


def fetch_daily_metrics(days: int = 30) -> list[dict]:
    """
    Pull daily metrics for all active agents.
    Returns a list of dicts: {agent_id, agent_name, agent_email, metrics: {...}}
    """
    agents = fetch_active_agents()
    log.info("Fetched %d active agents from FUB", len(agents))

    results = []
    for agent in agents:
        agent_id = agent["id"]
        name = agent.get("name", f"Agent {agent_id}")
        email = agent.get("email", "")

        try:
            leads = fetch_agent_leads(agent_id, days=days)
            metrics = calc_agent_metrics(leads)
            log.info(
                "  %s: %d Zillow leads, %d total leads",
                name,
                metrics["total_zillow_leads"],
                metrics["total_all_leads"],
            )
        except Exception as e:
            log.error("Failed to fetch metrics for %s: %s", name, e)
            metrics = calc_agent_metrics([])  # Empty metrics on failure

        results.append(
            {
                "agent_id": agent_id,
                "agent_name": name,
                "agent_email": email,
                "metrics": metrics,
            }
        )

    return results


def calc_team_averages(agent_results: list[dict]) -> dict:
    """Calculate team-wide averages from individual agent results."""
    agents_with_data = [r for r in agent_results if r["metrics"]["total_zillow_leads"] > 0]
    if not agents_with_data:
        return calc_agent_metrics([])

    n = len(agents_with_data)
    rts = [
        r["metrics"]["response_time_avg"]
        for r in agents_with_data
        if r["metrics"]["response_time_avg"] is not None
    ]

    return {
        "total_zillow_leads": sum(r["metrics"]["total_zillow_leads"] for r in agents_with_data),
        "total_all_leads": sum(r["metrics"]["total_all_leads"] for r in agents_with_data),
        "response_time_avg": round(sum(rts) / len(rts), 1) if rts else None,
        "contact_rate": round(sum(r["metrics"]["contact_rate"] for r in agents_with_data) / n, 3),
        "calls_outgoing": sum(r["metrics"]["calls_outgoing"] for r in agents_with_data),
        "calls_per_lead": round(
            sum(r["metrics"]["calls_per_lead"] for r in agents_with_data) / n, 2
        ),
        "texts_sent": sum(r["metrics"]["texts_sent"] for r in agents_with_data),
        "texts_per_lead": round(
            sum(r["metrics"]["texts_per_lead"] for r in agents_with_data) / n, 2
        ),
        "emails_sent": sum(r["metrics"]["emails_sent"] for r in agents_with_data),
        "appointment_rate": round(
            sum(r["metrics"]["appointment_rate"] for r in agents_with_data) / n, 3
        ),
        "lead_acceptance_rate": round(
            sum(r["metrics"]["lead_acceptance_rate"] for r in agents_with_data) / n, 3
        ),
    }


# ── SQLite storage ───────────────────────────────────────────────


DAILY_SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_date TEXT NOT NULL,
    agent_id INTEGER NOT NULL,
    agent_name TEXT NOT NULL,
    total_zillow_leads INTEGER,
    total_all_leads INTEGER,
    response_time_avg REAL,
    contact_rate REAL,
    calls_outgoing INTEGER,
    calls_per_lead REAL,
    texts_sent INTEGER,
    texts_per_lead REAL,
    emails_sent INTEGER,
    appointment_rate REAL,
    lead_acceptance_rate REAL,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(snapshot_date, agent_id)
);
"""


def save_daily_snapshot(agent_results: list[dict], db_path: str | None = None):
    """Save daily metrics snapshot to SQLite."""
    from config.settings import BASE_DIR

    if db_path is None:
        db_path = os.path.join(BASE_DIR, "data", "metrics.db")

    today = datetime.now().strftime("%Y-%m-%d")

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(DAILY_SCHEMA)

    for r in agent_results:
        m = r["metrics"]
        conn.execute(
            """INSERT OR REPLACE INTO daily_snapshots
               (snapshot_date, agent_id, agent_name,
                total_zillow_leads, total_all_leads,
                response_time_avg, contact_rate,
                calls_outgoing, calls_per_lead,
                texts_sent, texts_per_lead, emails_sent,
                appointment_rate, lead_acceptance_rate)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                today,
                r["agent_id"],
                r["agent_name"],
                m["total_zillow_leads"],
                m["total_all_leads"],
                m["response_time_avg"],
                m["contact_rate"],
                m["calls_outgoing"],
                m["calls_per_lead"],
                m["texts_sent"],
                m["texts_per_lead"],
                m["emails_sent"],
                m["appointment_rate"],
                m["lead_acceptance_rate"],
            ),
        )

    conn.commit()
    conn.close()
    log.info("Saved daily snapshot for %d agents (%s)", len(agent_results), today)
