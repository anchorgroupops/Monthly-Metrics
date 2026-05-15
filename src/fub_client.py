"""
Follow Up Boss API client.

Fetches Zillow Preferred Performance Report metrics for each agent in the
configured roster. Returns normalized dicts ready for metrics.py to score.

FUB API docs: https://docs.followupboss.com/reference
"""

import base64
import logging
import time
from datetime import date, timedelta

import requests

from config.settings import (
    AGENTS,
    FUB_API_KEY,
    FUB_BASE_URL,
    FUB_MAX_RETRIES,
    FUB_TIMEOUT_SECONDS,
    OVERRIDE_REPORT_MONTH,
)

log = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _auth_header() -> dict:
    """FUB uses HTTP Basic auth with the API key as the username."""
    token = base64.b64encode(f"{FUB_API_KEY}:".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _report_period() -> tuple[str, str]:
    """
    Returns (start_date, end_date) strings for the prior calendar month,
    or the OVERRIDE_REPORT_MONTH if set (format: 'YYYY-MM').
    """
    if OVERRIDE_REPORT_MONTH:
        year, month = map(int, OVERRIDE_REPORT_MONTH.split("-"))
    else:
        today = date.today()
        first_of_this_month = today.replace(day=1)
        last_month_end = first_of_this_month - timedelta(days=1)
        year, month = last_month_end.year, last_month_end.month

    start = date(year, month, 1)
    # Last day of month: go to first of next month, subtract one day
    if month == 12:
        end = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        end = date(year, month + 1, 1) - timedelta(days=1)

    return start.isoformat(), end.isoformat()


def _get(path: str, params: dict | None = None) -> dict:
    """
    GET from FUB API with retry logic and exponential backoff.
    Raises on non-2xx after exhausting retries.
    """
    url = f"{FUB_BASE_URL}/{path.lstrip('/')}"
    headers = {**_auth_header(), "Content-Type": "application/json"}
    delay = 2

    for attempt in range(1, FUB_MAX_RETRIES + 1):
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=FUB_TIMEOUT_SECONDS)
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", delay))
                log.warning("Rate limited by FUB. Waiting %ds…", retry_after)
                time.sleep(retry_after)
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            # 4xx (except 429, handled above) are permanent — don't retry.
            if status is not None and 400 <= status < 500:
                log.warning("FUB %d for %s — not retrying", status, url)
                raise
            log.warning("FUB request failed (attempt %d/%d): %s", attempt, FUB_MAX_RETRIES, exc)
            if attempt < FUB_MAX_RETRIES:
                time.sleep(delay)
                delay *= 2
            else:
                raise
        except requests.RequestException as exc:
            log.warning("FUB request failed (attempt %d/%d): %s", attempt, FUB_MAX_RETRIES, exc)
            if attempt < FUB_MAX_RETRIES:
                time.sleep(delay)
                delay *= 2
            else:
                raise

    raise RuntimeError(f"FUB API unreachable after {FUB_MAX_RETRIES} attempts: {url}")


# ── Core fetch functions ──────────────────────────────────────────────────────


def fetch_zillow_preferred_report(agent_id: str, start_date: str, end_date: str) -> dict:
    """
    Fetch the Zillow Preferred Performance Report for a single agent.

    FUB exposes agent performance stats under /reporting or via custom
    report endpoints. The exact path may need adjustment based on your FUB
    account's Zillow Preferred integration settings.

    Returns a raw dict from the FUB API response.
    """
    # Primary endpoint: agent performance report scoped to Zillow leads
    params = {
        "agentId": agent_id,
        "startDate": start_date,
        "endDate": end_date,
        "source": "Zillow",  # Filter to Zillow-source leads only
    }

    # Try the dedicated Zillow Preferred report endpoint first
    try:
        data = _get("/reporting/zillow-preferred", params=params)
        return data
    except requests.HTTPError as e:
        if e.response.status_code == 404:
            log.info("Dedicated ZP endpoint not found; falling back to /reporting/agent")
            # Fallback: general agent performance report
            return _get("/reporting/agent", params=params)
        raise


def fetch_users() -> list[dict]:
    """
    Discover the agent roster from FUB's /v1/users endpoint.

    Used as a fallback when AGENTS in config/settings.py is empty — lets the
    monthly cron run without a hand-maintained roster file. Pulls only users
    with role "Agent" or "Broker"; skips anything marked deleted/inactive.

    Returns a list of agent_cfg dicts shaped like the entries in AGENTS:
      {"name": str, "email": str, "fub_agent_id": str}
    """
    if not FUB_API_KEY:
        raise OSError("FUB_API_KEY is not set; cannot auto-discover users.")

    roster: list[dict] = []
    next_token: str | None = None
    seen = 0

    while True:
        params = {"limit": 100}
        if next_token:
            params["next"] = next_token
        data = _get("/users", params=params)

        for u in data.get("users", []):
            seen += 1
            role = (u.get("role") or "").strip()
            if role not in ("Agent", "Broker"):
                continue
            if u.get("deleted") or u.get("status") in ("inactive", "disabled"):
                continue
            email = (u.get("email") or "").strip()
            name = (u.get("name") or "").strip()
            user_id = u.get("id")
            if not email or not name or user_id is None:
                continue
            roster.append(
                {
                    "name": name,
                    "email": email,
                    "fub_agent_id": str(user_id),
                }
            )

        next_token = data.get("_metadata", {}).get("next")
        if not next_token:
            break

    log.info("FUB user auto-discovery: %d users seen, %d kept as agents", seen, len(roster))
    return roster


def fetch_all_agents(period: str | None = None) -> list[dict]:
    """
    Fetch and normalize Zillow Preferred metrics for every agent in AGENTS.

    Returns a list of dicts:
    {
        "agent_id":        str,
        "name":            str,
        "email":           str,
        "period":          str,    # e.g. "April 2026"
        "start_date":      str,
        "end_date":        str,
        "speed_to_action": float | None,  # seconds (lower is better; target 300s)
        "work_with_rate":  float | None,  # 0.0–1.0
        "csat":            float | None,  # 0.0–1.0
        "appt_set_rate":   float | None,  # 0.0–1.0
        "appt_met_rate":   float | None,  # 0.0–1.0
        "_raw":            dict,          # untouched API response for debugging
    }
    """
    if not FUB_API_KEY:
        raise OSError(
            "FUB_API_KEY is not set. Export it before running:\n  export FUB_API_KEY=your_key_here"
        )

    roster = list(AGENTS)
    if not roster:
        log.info("AGENTS is empty in config/settings.py — auto-discovering from FUB /v1/users.")
        roster = fetch_users()
        if not roster:
            log.warning("FUB /v1/users returned no usable agents — returning empty list.")
            return []

    start_date, end_date = _report_period()
    start_dt = date.fromisoformat(start_date)
    period_label = start_dt.strftime("%B %Y")

    results = []
    for agent_cfg in roster:
        agent_id = agent_cfg["fub_agent_id"]
        name = agent_cfg["name"]
        log.info("Fetching metrics for %s (ID: %s)…", name, agent_id)

        try:
            raw = fetch_zillow_preferred_report(agent_id, start_date, end_date)
            normalized = _normalize(raw, agent_cfg, period_label, start_date, end_date)
            results.append(normalized)
        except Exception as exc:
            log.error("Failed to fetch metrics for %s: %s", name, exc)
            # Include the agent with nulls so the report still generates
            results.append(_null_record(agent_cfg, period_label, start_date, end_date))

    return results


def _normalize(raw: dict, agent_cfg: dict, period: str, start: str, end: str) -> dict:
    """
    Map raw FUB API response fields to our standardized schema.

    FUB field names in the ZP report may vary — update the fallback keys below
    once you confirm the actual response shape from your account.
    """
    # speed_to_action: median seconds from inbound lead to first contact
    sta_raw = (
        raw.get("speedToAction")
        or raw.get("responseTime")
        or raw.get("medianResponseTimeSeconds")
        or raw.get("speedToLead")
        or raw.get("firstResponseTimeSeconds")
    )

    # work_with_rate: fraction of qualified leads signed to a working relationship
    wwr_raw = (
        raw.get("workWithRate")
        or raw.get("workingRelationshipRate")
        or raw.get("workWithPercentage")
    )

    # csat: customer satisfaction as a 0–1 fraction (Zillow reports 0–100; divide if needed)
    csat_raw = (
        raw.get("csatScore")
        or raw.get("csat")
        or raw.get("satisfactionScore")
        or raw.get("customerSatisfactionScore")
    )

    # appt_set_rate: fraction of qualified leads where an appointment was booked
    asr_raw = (
        raw.get("appointmentSetRate")
        or raw.get("apptSetRate")
        or raw.get("appointmentRate")
        or raw.get("meetingSetRate")
    )

    # appt_met_rate: fraction of booked appointments the lead actually attended
    amr_raw = (
        raw.get("appointmentMetRate")
        or raw.get("apptMetRate")
        or raw.get("showRate")
        or raw.get("appointmentShowRate")
        or raw.get("meetingMetRate")
    )

    # Zillow sometimes returns CSAT as 0–100; normalise to 0–1
    csat_val: float | None = None
    if csat_raw is not None:
        csat_val = float(csat_raw)
        if csat_val > 1.0:
            csat_val = csat_val / 100.0

    return {
        "agent_id": agent_cfg["fub_agent_id"],
        "name": agent_cfg["name"],
        "email": agent_cfg["email"],
        "period": period,
        "start_date": start,
        "end_date": end,
        "speed_to_action": float(sta_raw) if sta_raw is not None else None,
        "work_with_rate": float(wwr_raw) if wwr_raw is not None else None,
        "csat": csat_val,
        "appt_set_rate": float(asr_raw) if asr_raw is not None else None,
        "appt_met_rate": float(amr_raw) if amr_raw is not None else None,
        "_raw": raw,
    }


def _null_record(agent_cfg: dict, period: str, start: str, end: str) -> dict:
    """Return a placeholder record when the API call fails for an agent."""
    return {
        "agent_id": agent_cfg["fub_agent_id"],
        "name": agent_cfg["name"],
        "email": agent_cfg["email"],
        "period": period,
        "start_date": start,
        "end_date": end,
        "speed_to_action": None,
        "work_with_rate": None,
        "csat": None,
        "appt_set_rate": None,
        "appt_met_rate": None,
        "_raw": {},
        "_error": True,
    }


# ── Mock data for local testing ───────────────────────────────────────────────


def mock_agents(period: str | None = None) -> list[dict]:
    """
    Returns synthetic agent data for Review Mode testing without a live API key.
    Run: python main.py --mode review --mock
    """
    period_label = period or "April 2026"
    return [
        {
            "agent_id": "mock-001",
            "name": "Alex Rivera",
            "email": "alex@example.com",
            "period": period_label,
            "start_date": "2026-04-01",
            "end_date": "2026-04-30",
            "speed_to_action": 210.0,   # 3.5 min — green
            "work_with_rate": 0.55,     # green
            "csat": 0.91,               # green
            "appt_set_rate": 0.65,      # green
            "appt_met_rate": 0.78,      # green
            "_raw": {},
        },
        {
            "agent_id": "mock-002",
            "name": "Jordan Lee",
            "email": "jordan@example.com",
            "period": period_label,
            "start_date": "2026-04-01",
            "end_date": "2026-04-30",
            "speed_to_action": 480.0,   # 8 min — yellow
            "work_with_rate": 0.42,     # yellow
            "csat": 0.78,               # yellow
            "appt_set_rate": 0.52,      # yellow
            "appt_met_rate": 0.58,      # yellow
            "_raw": {},
        },
        {
            "agent_id": "mock-003",
            "name": "Morgan Chen",
            "email": "morgan@example.com",
            "period": period_label,
            "start_date": "2026-04-01",
            "end_date": "2026-04-30",
            "speed_to_action": 750.0,   # 12.5 min — red
            "work_with_rate": 0.31,     # red
            "csat": 0.72,               # red
            "appt_set_rate": 0.38,      # red
            "appt_met_rate": 0.45,      # red
            "_raw": {},
        },
    ]
