"""
Follow Up Boss API client — monthly Zillow Preferred metrics.

Data sources (per FUB Open API guidance):

  speed_to_action  → /people: compare lead.created to first manual contact
                     (firstCall / lastSentText / lastSentEmail on person record)
  work_with_rate   → /people: fraction of leads that moved past the New stage
                     (best available proxy; FUB has no "signed working agreement" field)
  appt_set_rate    → /appointments: appointments created for agent in the period
                     divided by total Zillow leads in the period
  appt_met_rate    → /appointments: outcomes of "Completed"/"Met"/"Showed"
                     divided by total appointments set
  csat             → Not available via FUB API. Zillow's CSAT/NPS data lives in
                     their proprietary "Best of Zillow" report and is not exposed
                     through a standard FUB endpoint. Stored as None until a
                     Zillow API integration is added.

FUB API docs: https://docs.followupboss.com/reference
"""

import base64
import logging
import time
from datetime import date, timedelta
from statistics import median

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

# FUB appointment outcome values that indicate the lead actually showed up.
# The exact strings are tenant/FUB-version specific — add aliases as needed.
_APPT_MET_OUTCOMES = {"completed", "met", "showed", "show", "shown"}


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


def _is_appt_met(appt: dict) -> bool:
    """True if the appointment outcome indicates the lead showed up."""
    outcome = (appt.get("outcome") or appt.get("outcomeType") or "").strip().lower()
    return outcome in _APPT_MET_OUTCOMES


# ── Stage-id constants (used only for work_with_rate proxy) ──────────────────

# Empirically observed FUB stage ids for The Anchor Group:
#   26 = New, 27 = Attempted Contact, 28 = Contacted, 29 = Appt Set, 30 = Met
_STAGE_NEW = 26


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
    Compute monthly Zillow Preferred metrics for every agent from /v1/people.

    Returns a list of dicts:
    {
        "agent_id":        str,
        "name":            str,
        "email":           str,
        "period":          str,    # e.g. "April 2026"
        "start_date":      str,
        "end_date":        str,
        "speed_to_action": float | None,  # median seconds to first contact (lower_is_better)
        "work_with_rate":  float | None,  # fraction of leads moved past New stage (0.0–1.0)
        "csat":            float | None,  # always None — not available from FUB people data
        "appt_set_rate":   float | None,  # fraction with appointment set/met (0.0–1.0)
        "appt_met_rate":   float | None,  # fraction of set appts that were met (0.0–1.0)
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


def _first_contact_seconds(person: dict) -> float | None:
    """Seconds from lead created to earliest of (firstCall, lastSentText, lastSentEmail)."""
    from datetime import UTC, datetime

    def _parse(val: str | None) -> datetime | None:
        if not val:
            return None
        for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S+00:00", "%Y-%m-%dT%H:%M:%S"):
            try:
                dt = datetime.strptime(val, fmt)
                return dt.replace(tzinfo=UTC)
            except ValueError:
                continue
        return None

    created = _parse(person.get("created"))
    if created is None:
        return None

    candidates = [_parse(person.get(k)) for k in ("firstCall", "lastSentText", "lastSentEmail")]
    valid = [c for c in candidates if c is not None and c >= created]
    if not valid:
        return None
    return max(0.0, (min(valid) - created).total_seconds())


def _compute_monthly_metrics(
    people: list[dict],
    appointments: list[dict],
    agent_cfg: dict,
    period: str,
    start: str,
    end: str,
) -> dict:
    """
    Compute monthly ZP metrics.

    people       → /people (Zillow Preferred leads for the period)
    appointments → /appointments (appointments set for the period); may be []
                   if the endpoint was unavailable.
    """
    total = len(people)

    if total == 0:
        return _null_record(agent_cfg, period, start, end)

    # speed_to_action + work_with_rate from People data
    response_times: list[float] = []
    accepted_count = 0

    for p in people:
        rt = _first_contact_seconds(p)
        if rt is not None:
            response_times.append(rt)
        stage_id = p.get("stageId")
        if isinstance(stage_id, int) and stage_id > _STAGE_NEW:
            accepted_count += 1

    speed_to_action = median(response_times) if response_times else None
    work_with_rate = accepted_count / total

    # appt_set_rate + appt_met_rate from Appointments data (preferred)
    # Falls back to None if appointments list is empty (endpoint unavailable).
    if appointments:
        appt_set_count = len(appointments)
        appt_met_count = sum(1 for a in appointments if _is_appt_met(a))
        appt_set_rate = appt_set_count / total
        appt_met_rate = appt_met_count / appt_set_count if appt_set_count > 0 else None
    else:
        appt_set_rate = None
        appt_met_rate = None

    return {
        "agent_id": agent_cfg["fub_agent_id"],
        "name": agent_cfg["name"],
        "email": agent_cfg["email"],
        "period": period,
        "start_date": start,
        "end_date": end,
        "speed_to_action": speed_to_action,
        "work_with_rate": work_with_rate,
        "csat": None,  # Zillow proprietary — not available via FUB API
        "appt_set_rate": appt_set_rate,
        "appt_met_rate": appt_met_rate,
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
        "speed_to_action": None,
        "work_with_rate": None,
        "csat": None,
        "appt_set_rate": None,
        "appt_met_rate": None,
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
            "speed_to_action": 210.0,  # 3.5 min — green
            "work_with_rate": 0.55,  # green
            "csat": 0.91,  # green
            "appt_set_rate": 0.65,  # green
            "appt_met_rate": 0.78,  # green
        },
        {
            "agent_id": "mock-002",
            "name": "Jordan Lee",
            "email": "jordan@example.com",
            "period": period_label,
            "start_date": "2026-04-01",
            "end_date": "2026-04-30",
            "speed_to_action": 480.0,  # 8 min — yellow
            "work_with_rate": 0.42,  # yellow
            "csat": 0.78,  # yellow
            "appt_set_rate": 0.52,  # yellow
            "appt_met_rate": 0.58,  # yellow
        },
        {
            "agent_id": "mock-003",
            "name": "Morgan Chen",
            "email": "morgan@example.com",
            "period": period_label,
            "start_date": "2026-04-01",
            "end_date": "2026-04-30",
            "speed_to_action": 750.0,  # 12.5 min — red
            "work_with_rate": 0.31,  # red
            "csat": 0.72,  # red
            "appt_set_rate": 0.38,  # red
            "appt_met_rate": 0.45,  # red
        },
    ]
