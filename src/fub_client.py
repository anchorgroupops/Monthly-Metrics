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
from typing import Optional

import requests

from config.settings import (
    FUB_API_KEY,
    FUB_BASE_URL,
    FUB_MAX_RETRIES,
    FUB_TIMEOUT_SECONDS,
    AGENTS,
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


def _get(path: str, params: Optional[dict] = None) -> dict:
    """
    GET from FUB API with retry logic and exponential backoff.
    Raises on non-2xx after exhausting retries.
    """
    url = f"{FUB_BASE_URL}/{path.lstrip('/')}"
    headers = {**_auth_header(), "Content-Type": "application/json"}
    delay = 2

    for attempt in range(1, FUB_MAX_RETRIES + 1):
        try:
            resp = requests.get(
                url, headers=headers, params=params, timeout=FUB_TIMEOUT_SECONDS
            )
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", delay))
                log.warning("Rate limited by FUB. Waiting %ds…", retry_after)
                time.sleep(retry_after)
                delay *= 2
                continue
            resp.raise_for_status()
            return resp.json()
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
        "source": "Zillow",          # Filter to Zillow-source leads only
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


def fetch_all_agents(period: Optional[str] = None) -> list[dict]:
    """
    Fetch and normalize Zillow Preferred metrics for every agent in AGENTS.

    Returns a list of dicts:
    {
        "agent_id":       str,
        "name":           str,
        "email":          str,
        "period":         str,   # e.g. "March 2026"
        "start_date":     str,
        "end_date":       str,
        "pCVR":           float | None,   # 0.0–1.0
        "pickup_rate":    float | None,   # 0.0–1.0
        "csat":           float | None,   # raw score
        "zhl_transfers":  int   | None,
        "_raw":           dict,           # untouched API response for debugging
    }
    """
    if not FUB_API_KEY:
        raise EnvironmentError(
            "FUB_API_KEY is not set. Export it before running:\n"
            "  export FUB_API_KEY=your_key_here"
        )

    if not AGENTS:
        log.warning("No agents configured in config/settings.py — returning empty list.")
        return []

    start_date, end_date = _report_period()
    start_dt = date.fromisoformat(start_date)
    period_label = start_dt.strftime("%B %Y")

    results = []
    for agent_cfg in AGENTS:
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

    NOTE: FUB field names in the ZP report may vary — update the keys below
    once you confirm the actual response shape from your account.
    Common observed fields are listed; add alternatives if needed.
    """
    pCVR_raw       = _first_present(raw, ["predictedConversionRate", "pCVR", "conversionRatePredicted"])
    pickup_raw     = _first_present(raw, ["pickupRate", "callPickupRate", "answerRate"])
    csat_raw       = _first_present(raw, ["csatScore", "csat", "satisfactionScore"])
    zhl_raw        = _first_present(raw, ["zhlTransfers", "zillowHomeLoanTransfers", "transferCount"])

    return {
        "agent_id":      agent_cfg["fub_agent_id"],
        "name":          agent_cfg["name"],
        "email":         agent_cfg["email"],
        "period":        period,
        "start_date":    start,
        "end_date":      end,
        "pCVR":          _to_float(pCVR_raw),
        "pickup_rate":   _to_float(pickup_raw),
        "csat":          _to_float(csat_raw),
        "zhl_transfers": _to_int(zhl_raw),
        "_raw":          raw,
    }


def _first_present(raw: dict, keys: list[str]):
    """Return the first key whose value is not None — distinguishes 0.0 from missing."""
    for key in keys:
        if raw.get(key) is not None:
            return raw[key]
    return None


def _to_float(value) -> Optional[float]:
    """Coerce to float; return None for None or unparseable values."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        log.warning("Could not coerce %r to float; treating as missing.", value)
        return None


def _to_int(value) -> Optional[int]:
    """Coerce to int; tolerates float-like strings ('3.0', '3.6') by going through float."""
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        log.warning("Could not coerce %r to int; treating as missing.", value)
        return None


def _null_record(agent_cfg: dict, period: str, start: str, end: str) -> dict:
    """Return a placeholder record when the API call fails for an agent."""
    return {
        "agent_id":      agent_cfg["fub_agent_id"],
        "name":          agent_cfg["name"],
        "email":         agent_cfg["email"],
        "period":        period,
        "start_date":    start,
        "end_date":      end,
        "pCVR":          None,
        "pickup_rate":   None,
        "csat":          None,
        "zhl_transfers": None,
        "_raw":          {},
        "_error":        True,
    }


# ── Mock data for local testing ───────────────────────────────────────────────

def mock_agents(period: Optional[str] = None) -> list[dict]:
    """
    Returns synthetic agent data for Review Mode testing without a live API key.
    Run: python main.py --mode review --mock
    """
    period_label = period or "March 2026"
    return [
        {
            "agent_id": "mock-001", "name": "Alex Rivera", "email": "alex@example.com",
            "period": period_label, "start_date": "2026-03-01", "end_date": "2026-03-31",
            "pCVR": 0.038, "pickup_rate": 0.91, "csat": 4.7, "zhl_transfers": 5, "_raw": {},
        },
        {
            "agent_id": "mock-002", "name": "Jordan Lee", "email": "jordan@example.com",
            "period": period_label, "start_date": "2026-03-01", "end_date": "2026-03-31",
            "pCVR": 0.021, "pickup_rate": 0.74, "csat": 4.1, "zhl_transfers": 2, "_raw": {},
        },
        {
            "agent_id": "mock-003", "name": "Morgan Chen", "email": "morgan@example.com",
            "period": period_label, "start_date": "2026-03-01", "end_date": "2026-03-31",
            "pCVR": 0.015, "pickup_rate": 0.61, "csat": 3.8, "zhl_transfers": 1, "_raw": {},
        },
    ]
