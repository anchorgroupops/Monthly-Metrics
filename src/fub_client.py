"""
Follow Up Boss API client — monthly Zillow Preferred scorecard.

The 2026 scorecard has four metrics. Only one is derivable from FUB; the
other three come from Zillow's monthly Performance Report (CSV upload):

  pCVR              → Zillow Premier Performance Report only. None from FUB.
  pickup_rate       → /people: fraction of Zillow leads with at least one
                      inbound call connected (callsIncoming > 0). Best proxy
                      available from /people without per-call data.
  zhl_pre_approval  → Zillow Home Loans report only. None from FUB.
  csat              → Zillow "Best of Zillow" report only. None from FUB.

The pull therefore exists primarily to seed the roster and any pickup_rate
proxy. The admin uploads the Zillow CSV via /upload to populate the rest.

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


# ── Per-lead people fetch ─────────────────────────────────────────────────────


def _fetch_people_raw(agent_id: str, start_date: str, end_date: str) -> list[dict]:
    """
    Page through /v1/people for one agent without any source filtering.

    Returns every person assigned to ``agent_id`` whose ``created`` falls in
    [start_date, end_date]. Used both by the production pull (which then
    applies ``is_zillow_preferred``) and by the diagnostic CLI (which wants
    the unfiltered raw view to surface mis-tagged Zillow leads).
    """
    collected: list[dict] = []
    offset = 0
    limit = 100

    while True:
        params = {
            "assignedUserId": agent_id,
            "createdAfter": start_date,
            "createdBefore": end_date,
            "limit": limit,
            "offset": offset,
            "fields": "allFields",
        }
        data = _get("/people", params=params)
        people = data.get("people") or []
        collected.extend(people)

        meta = data.get("_metadata") or {}
        total = meta.get("total")
        if not people or len(people) < limit:
            break
        offset += limit
        if total is not None and offset >= total:
            break
        if offset >= 5000:
            log.warning("Stopping at offset 5000 for agent %s — check pagination", agent_id)
            break

    return collected


def _fetch_people_for_agent(agent_id: str, start_date: str, end_date: str) -> list[dict]:
    """
    Page through /v1/people for one agent, returning every Zillow Preferred lead
    created within [start_date, end_date] (ISO YYYY-MM-DD).
    """
    from src.fub_daily_metrics import is_zillow_preferred

    return [p for p in _fetch_people_raw(agent_id, start_date, end_date) if is_zillow_preferred(p)]


# ── Appointments fetch ────────────────────────────────────────────────────────


def _fetch_appointments_for_agent(agent_id: str, start_date: str, end_date: str) -> list[dict]:
    """
    Fetch appointments created by/for this agent within [start_date, end_date].

    FUB Appointments fields used:
      userId    — FUB user id of the agent who owns the appointment
      outcome   — appointment result ("Completed", "Met", "No Show", etc.)
      created   — ISO timestamp when the appointment was booked in FUB

    Returns an empty list (soft-fail) if the endpoint returns a non-2xx that
    suggests the resource is unavailable, so missing appointment data doesn't
    abort the rest of the per-agent computation.
    """
    import requests as _req

    collected: list[dict] = []
    offset = 0
    limit = 100

    while True:
        params = {
            "userId": agent_id,
            "createdAfter": start_date,
            "createdBefore": end_date,
            "limit": limit,
            "offset": offset,
        }
        try:
            data = _get("/appointments", params=params)
        except _req.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status in (404, 403):
                log.warning("/appointments returned %s — appt metrics will be None", status)
                return []
            raise
        except Exception as exc:
            log.warning("/appointments fetch failed (%s) — appt metrics will be None", exc)
            return []
        appts = data.get("appointments") or []
        collected.extend(appts)

        meta = data.get("_metadata") or {}
        total = meta.get("total")
        if not appts or len(appts) < limit:
            break
        offset += limit
        if total is not None and offset >= total:
            break
        if offset >= 5000:
            log.warning("Stopping at offset 5000 for agent %s appointments", agent_id)
            break

    return collected


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
    Compute monthly Zillow Preferred scorecard metrics for every agent.

    Returns a list of dicts:
    {
        "agent_id":         str,
        "name":             str,
        "email":            str,
        "period":           str,    # e.g. "April 2026"
        "start_date":       str,
        "end_date":         str,
        "pCVR":             float | None,  # Zillow CSV-only
        "pickup_rate":      float | None,  # proxy from /people callsIncoming
        "zhl_pre_approval": float | None,  # Zillow CSV-only
        "csat":             float | None,  # Zillow CSV-only
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
    empty_names: list[str] = []
    error_names: list[str] = []
    for agent_cfg in roster:
        agent_id = agent_cfg["fub_agent_id"]
        name = agent_cfg["name"]
        log.info("Fetching metrics for %s (ID: %s)…", name, agent_id)

        try:
            people = _fetch_people_for_agent(agent_id, start_date, end_date)
            appointments = _fetch_appointments_for_agent(agent_id, start_date, end_date)
            status = "ok" if people else "empty"
            log.info(
                "pull: %s id=%s leads=%d appts=%d status=%s",
                name,
                agent_id,
                len(people),
                len(appointments),
                status,
            )
            if not people:
                empty_names.append(name)
            results.append(
                _compute_monthly_metrics(
                    people, appointments, agent_cfg, period_label, start_date, end_date
                )
            )
        except Exception as exc:
            log.error("pull: %s id=%s status=error error=%s", name, agent_id, exc)
            error_names.append(name)
            results.append(_null_record(agent_cfg, period_label, start_date, end_date))

    with_leads = len(results) - len(empty_names) - len(error_names)
    log.info("pull summary: %d/%d agents with leads", with_leads, len(results))
    if empty_names:
        log.info("pull summary: no-leads agents: %s", ", ".join(empty_names))
    if error_names:
        log.warning("pull summary: errored agents: %s", ", ".join(error_names))

    return results


def _compute_monthly_metrics(
    people: list[dict],
    appointments: list[dict],
    agent_cfg: dict,
    period: str,
    start: str,
    end: str,
) -> dict:
    """
    Compute monthly Zillow Preferred scorecard metrics for one agent.

    Only ``pickup_rate`` is derivable from FUB person data — the other three
    scorecard metrics (``pCVR``, ``zhl_pre_approval``, ``csat``) live in the
    Zillow Premier Performance Report and must be populated via CSV upload.
    They're emitted as None here so storage.save_period still writes a row
    per agent/metric and the dashboard can render "No Data" until the CSV is
    ingested.

    ``appointments`` is accepted for API compatibility with the existing
    fetch loop but isn't part of the current 4-metric scorecard.
    """
    del appointments  # not used by the current scorecard

    total = len(people)

    if total == 0:
        return _null_record(agent_cfg, period, start, end)

    # Pickup rate: fraction of Zillow leads where any inbound call connected.
    # FUB's person record exposes ``callsIncoming`` (count of incoming calls
    # that reached the agent). Lacking call-level pickup data on /people, the
    # next-best proxy is: lead had at least one connected call.
    connected = sum(1 for p in people if int(p.get("callsIncoming") or 0) > 0)
    pickup_rate = connected / total if total else None

    return {
        "agent_id": agent_cfg["fub_agent_id"],
        "name": agent_cfg["name"],
        "email": agent_cfg["email"],
        "period": period,
        "start_date": start,
        "end_date": end,
        "pCVR": None,
        "pickup_rate": pickup_rate,
        "zhl_pre_approval": None,
        "csat": None,
    }


def _null_record(agent_cfg: dict, period: str, start: str, end: str) -> dict:
    """Return a placeholder record when the API call fails or yields no leads."""
    return {
        "agent_id": agent_cfg["fub_agent_id"],
        "name": agent_cfg["name"],
        "email": agent_cfg["email"],
        "period": period,
        "start_date": start,
        "end_date": end,
        "pCVR": None,
        "pickup_rate": None,
        "zhl_pre_approval": None,
        "csat": None,
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
            "pCVR": 0.32,  # green (target 0.25)
            "pickup_rate": 0.42,  # green
            "zhl_pre_approval": 0.14,  # green
            "csat": 0.91,  # green
        },
        {
            "agent_id": "mock-002",
            "name": "Jordan Lee",
            "email": "jordan@example.com",
            "period": period_label,
            "start_date": "2026-04-01",
            "end_date": "2026-04-30",
            "pCVR": 0.22,  # yellow
            "pickup_rate": 0.21,  # yellow
            "zhl_pre_approval": 0.08,  # yellow
            "csat": 0.78,  # yellow
        },
        {
            "agent_id": "mock-003",
            "name": "Morgan Chen",
            "email": "morgan@example.com",
            "period": period_label,
            "start_date": "2026-04-01",
            "end_date": "2026-04-30",
            "pCVR": 0.15,  # red
            "pickup_rate": 0.15,  # red
            "zhl_pre_approval": 0.04,  # red
            "csat": 0.72,  # red
        },
    ]
