"""
Daily operational activity metrics derived from the FUB /v1/people endpoint.

This is the **daily pulse** counterpart to the monthly Zillow Preferred report.
Where ``src/fub_client.py`` queries a (mostly nonexistent) aggregate reporting
endpoint, this module computes metrics directly from per-lead data so we can
run it hourly or daily without depending on FUB's UI-only Performance Report.

Window
------
Each run measures **month-to-date**: leads with ``created >= start of the
current calendar month``. Today / This Week views are derived by the dashboard
by diffing snapshots from different days — keeps storage simple and idempotent.

Metric set (the 8 daily metrics + activity points)
--------------------------------------------------
- ``response_time_seconds``      avg seconds from lead created to first contact
- ``contact_rate``               fraction of leads with contacted=1            (0.0-1.0)
- ``pickup_rate``                fraction of leads where firstCall connected   (0.0-1.0)
- ``appointment_rate``           fraction of leads with stageId in (29, 30)    (0.0-1.0)
- ``lead_acceptance_rate``       fraction of leads moved past 'New' (stage>26) (0.0-1.0)
- ``call_volume``                sum of callsOutgoing                          (count)
- ``texts_sent``                 sum of textsSent                              (count)
- ``emails_sent``                sum of emailsSent                             (count)
- ``conversations_2min``         count of leads with callsDuration >= 120s     (count)
- ``appointments_set``           count of leads with stageId in (29, 30)       (count)
- ``new_leads_not_acted_on``     count of leads with stageId == 26 and contacted=0
- ``total_zillow_leads``         count of filtered Zillow Preferred leads      (count)
- ``activity_points``            weighted leaderboard score (see POINTS below)

Activity-point weights match the gamification scheme:
    Appointments Set     × 500
    Conversations 2+ min × 100
    Call Attempts        × 10
    Texts Sent           × 2
    Emails Sent          × 1

What "Zillow Preferred lead" means
----------------------------------
A lead is counted when either source == 'Zillow Preferred' (case-insensitive
match) or sourceId == 14. Belt-and-suspenders against FUB tenant-specific
labeling drift.

Response-time approximation
---------------------------
FUB's person record exposes ``firstCall`` (timestamp of the first call ever
placed to this person), plus ``lastSentText`` and ``lastSentEmail`` (the most
recent outbound text/email). We take the earliest non-null of those three and
subtract ``created``. That's a conservative proxy — if an agent texted the lead
multiple times, we measure to the *last* text, biasing the number upward. The
fully correct path would be ``/v1/events?personId=...`` per lead, but that's
~N more API calls per agent per run. Acceptable for a daily-pulse view.
"""

from __future__ import annotations

import base64
import logging
import time
from datetime import UTC, date, datetime
from typing import Any

import requests

from config.settings import (
    FUB_API_KEY,
    FUB_BASE_URL,
    FUB_MAX_RETRIES,
    FUB_TIMEOUT_SECONDS,
)

log = logging.getLogger(__name__)


# ── Activity-point weights ────────────────────────────────────────────────────

POINTS = {
    "appointments_set": 500,
    "conversations_2min": 100,
    "call_volume": 10,
    "texts_sent": 2,
    "emails_sent": 1,
}

# Empirically observed FUB stage ids for The Anchor Group:
#   26 = New
#   27 = Attempted Contact
#   28 = Contacted
#   29 = Appointment Set
#   30 = Met
# These ids are tenant-specific. If they change, override via env or update
# here. The constants keep meaning consistent across this module.
STAGE_NEW = 26
APPT_STAGE_IDS = (29, 30)
CONVERSATION_DURATION_SECONDS = 120

# Zillow Preferred source identification (either match wins).
ZILLOW_SOURCE_ID = 14
ZILLOW_SOURCE_NAMES = ("zillow preferred", "zillow flex")


# ── HTTP layer ────────────────────────────────────────────────────────────────


def _auth_header() -> dict:
    """FUB uses HTTP Basic auth with the API key as the username."""
    token = base64.b64encode(f"{FUB_API_KEY}:".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _get(path: str, params: dict | None = None) -> dict:
    """
    GET from FUB API with exponential-backoff retries on 5xx/network errors.
    Honors Retry-After on 429. Raises after FUB_MAX_RETRIES attempts.
    """
    url = f"{FUB_BASE_URL}/{path.lstrip('/')}"
    headers = {**_auth_header(), "Content-Type": "application/json"}
    delay = 2

    for attempt in range(1, FUB_MAX_RETRIES + 1):
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=FUB_TIMEOUT_SECONDS)
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", delay))
                log.warning("FUB rate-limit; sleeping %ds", retry_after)
                time.sleep(retry_after)
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status is not None and 400 <= status < 500:
                log.warning("FUB %d for %s (no retry)", status, url)
                raise
            log.warning("FUB request failed (%d/%d): %s", attempt, FUB_MAX_RETRIES, exc)
            if attempt < FUB_MAX_RETRIES:
                time.sleep(delay)
                delay *= 2
            else:
                raise
        except requests.RequestException as exc:
            log.warning("FUB network error (%d/%d): %s", attempt, FUB_MAX_RETRIES, exc)
            if attempt < FUB_MAX_RETRIES:
                time.sleep(delay)
                delay *= 2
            else:
                raise

    raise RuntimeError(f"FUB API unreachable after {FUB_MAX_RETRIES} attempts: {url}")


# ── Window / date helpers ─────────────────────────────────────────────────────


def month_start(today: date | None = None) -> str:
    """Return ISO YYYY-MM-DD for the first day of the calendar month of ``today``."""
    d = today or date.today()
    return d.replace(day=1).isoformat()


def _parse_ts(value: Any) -> datetime | None:
    """
    Parse a FUB timestamp. Accepts:
      - ISO 8601 strings (with or without trailing 'Z')
      - epoch seconds as int/float (>0)
      - empty / 0 / None -> None
    """
    if value is None or value == "" or value == 0:
        return None
    if isinstance(value, (int, float)):
        if value <= 0:
            return None
        return datetime.fromtimestamp(value, tz=UTC)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


# ── Filtering ─────────────────────────────────────────────────────────────────


def is_zillow_preferred(person: dict) -> bool:
    """True if the person record looks like a Zillow Preferred lead."""
    if person.get("sourceId") == ZILLOW_SOURCE_ID:
        return True
    source = (person.get("source") or "").strip().lower()
    return any(name in source for name in ZILLOW_SOURCE_NAMES)


# ── People fetch (paginated) ──────────────────────────────────────────────────


def fetch_people_for_agent(assigned_user_id: str, created_after: str) -> list[dict]:
    """
    Page through /v1/people for one agent, returning every record created on
    or after ``created_after`` (ISO YYYY-MM-DD). Uses offset pagination since
    that's what the people endpoint supports.
    """
    if not FUB_API_KEY:
        raise OSError("FUB_API_KEY is not set; cannot fetch people.")

    collected: list[dict] = []
    offset = 0
    limit = 100

    while True:
        params = {
            "assignedUserId": assigned_user_id,
            "createdAfter": created_after,
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
        # Defensive cap so a buggy total doesn't infinite-loop on us.
        if total is not None and offset >= total:
            break
        if offset >= 5000:
            log.warning(
                "Stopping at offset 5000 for user %s — review pagination assumptions",
                assigned_user_id,
            )
            break

    return collected


# ── Metric calculation ────────────────────────────────────────────────────────


def _response_time_seconds(person: dict) -> float | None:
    """Earliest of (firstCall, lastSentText, lastSentEmail) minus created."""
    created = _parse_ts(person.get("created"))
    if created is None:
        return None

    candidates: list[datetime] = []
    for key in ("firstCall", "lastSentText", "lastSentEmail"):
        ts = _parse_ts(person.get(key))
        if ts is not None and ts >= created:
            candidates.append(ts)

    if not candidates:
        return None

    delta = min(candidates) - created
    return max(0.0, delta.total_seconds())


def _last_call_duration(person: dict) -> float:
    """Total call duration in seconds for this person (cumulative). 0 if absent."""
    raw = person.get("callsDuration") or person.get("lastCallDuration") or 0
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _picked_up(person: dict) -> bool | None:
    """
    Did the first outbound call connect? FUB doesn't expose this directly. We
    approximate "yes" when firstCall has a timestamp AND callsDuration is at
    least 10s (i.e. it wasn't a voicemail drop). Returns None when the agent
    never attempted a call — those leads are not in the pickup denominator.
    """
    first_call = _parse_ts(person.get("firstCall"))
    if first_call is None:
        return None
    return _last_call_duration(person) >= 10.0


def calculate_agent_metrics(zillow_people: list[dict]) -> dict[str, float | int | None]:
    """
    Aggregate the 8 daily metrics + activity points across one agent's filtered
    Zillow Preferred leads. Returns a flat dict suitable for save_daily_snapshot.
    """
    total = len(zillow_people)
    if total == 0:
        return {
            "response_time_seconds": None,
            "contact_rate": None,
            "pickup_rate": None,
            "appointment_rate": None,
            "lead_acceptance_rate": None,
            "call_volume": 0,
            "texts_sent": 0,
            "emails_sent": 0,
            "conversations_2min": 0,
            "appointments_set": 0,
            "new_leads_not_acted_on": 0,
            "total_zillow_leads": 0,
            "activity_points": 0,
        }

    response_times: list[float] = []
    pickup_outcomes: list[bool] = []
    contacted_count = 0
    appointment_count = 0
    accepted_count = 0
    call_volume = 0
    texts_sent = 0
    emails_sent = 0
    conversations_2min = 0
    new_not_acted_on = 0

    for p in zillow_people:
        rt = _response_time_seconds(p)
        if rt is not None:
            response_times.append(rt)

        pickup = _picked_up(p)
        if pickup is not None:
            pickup_outcomes.append(pickup)

        if int(p.get("contacted") or 0) == 1:
            contacted_count += 1

        stage_id = p.get("stageId")
        if isinstance(stage_id, int):
            if stage_id in APPT_STAGE_IDS:
                appointment_count += 1
            if stage_id > STAGE_NEW:
                accepted_count += 1
            if stage_id == STAGE_NEW and int(p.get("contacted") or 0) == 0:
                new_not_acted_on += 1

        call_volume += int(p.get("callsOutgoing") or 0)
        texts_sent += int(p.get("textsSent") or 0)
        emails_sent += int(p.get("emailsSent") or 0)

        if _last_call_duration(p) >= CONVERSATION_DURATION_SECONDS:
            conversations_2min += 1

    activity_points = (
        appointment_count * POINTS["appointments_set"]
        + conversations_2min * POINTS["conversations_2min"]
        + call_volume * POINTS["call_volume"]
        + texts_sent * POINTS["texts_sent"]
        + emails_sent * POINTS["emails_sent"]
    )

    return {
        "response_time_seconds": (
            sum(response_times) / len(response_times) if response_times else None
        ),
        "contact_rate": contacted_count / total,
        "pickup_rate": (
            sum(1 for ok in pickup_outcomes if ok) / len(pickup_outcomes)
            if pickup_outcomes
            else None
        ),
        "appointment_rate": appointment_count / total,
        "lead_acceptance_rate": accepted_count / total,
        "call_volume": call_volume,
        "texts_sent": texts_sent,
        "emails_sent": emails_sent,
        "conversations_2min": conversations_2min,
        "appointments_set": appointment_count,
        "new_leads_not_acted_on": new_not_acted_on,
        "total_zillow_leads": total,
        "activity_points": activity_points,
    }


# ── Top-level run ─────────────────────────────────────────────────────────────


def pull_daily_metrics(today: date | None = None) -> list[dict]:
    """
    Discover the agent roster (via fub_client.fetch_users, which respects the
    AGENTS config) and compute MTD metrics for each. Returns a list of:

        {
            "agent_id": str,
            "name": str,
            "email": str,
            "snapshot_date": "YYYY-MM-DD",
            "window_start": "YYYY-MM-DD",
            "metrics": {...},   # see calculate_agent_metrics
            "_error": Optional[str],
        }

    Errors on individual agents are caught — the agent's row is included with
    the error populated and metrics set to None so the dashboard still renders.
    """
    from config.settings import AGENTS
    from src.fub_client import fetch_users

    if not FUB_API_KEY:
        raise OSError("FUB_API_KEY is not set; set it before running --mode daily.")

    roster = list(AGENTS)
    if not roster:
        log.info("AGENTS is empty — auto-discovering from FUB /v1/users.")
        roster = fetch_users()
    if not roster:
        log.warning("No agents to process.")
        return []

    today = today or date.today()
    snapshot_date = today.isoformat()
    window_start = month_start(today)

    results: list[dict] = []
    for cfg in roster:
        agent_id = str(cfg["fub_agent_id"])
        name = cfg["name"]
        email = cfg["email"]
        log.info("Daily pull for %s (FUB user %s) MTD from %s", name, agent_id, window_start)
        try:
            people = fetch_people_for_agent(agent_id, window_start)
            zillow_people = [p for p in people if is_zillow_preferred(p)]
            metrics = calculate_agent_metrics(zillow_people)
            results.append(
                {
                    "agent_id": agent_id,
                    "name": name,
                    "email": email,
                    "snapshot_date": snapshot_date,
                    "window_start": window_start,
                    "metrics": metrics,
                }
            )
        except Exception as exc:
            log.exception("Daily pull failed for %s", name)
            results.append(
                {
                    "agent_id": agent_id,
                    "name": name,
                    "email": email,
                    "snapshot_date": snapshot_date,
                    "window_start": window_start,
                    "metrics": calculate_agent_metrics([]),
                    "_error": str(exc),
                }
            )

    return results


def save_results(results: list[dict]) -> int:
    """Persist a list of pull_daily_metrics results to SQLite. Returns count saved."""
    from src import storage

    saved = 0
    for r in results:
        storage.save_daily_snapshot(
            agent_id=r["agent_id"],
            snapshot_date=r["snapshot_date"],
            metrics=r["metrics"],
            name=r["name"],
            email=r["email"],
        )
        saved += 1
    return saved


# ── Mock data for local testing without an API key ────────────────────────────


def mock_daily_results(today: date | None = None) -> list[dict]:
    """Synthetic results that exercise the full snapshot/save path."""
    today = today or date.today()
    snapshot_date = today.isoformat()
    window_start = month_start(today)
    return [
        {
            "agent_id": "mock-001",
            "name": "Alex Rivera",
            "email": "alex@example.com",
            "snapshot_date": snapshot_date,
            "window_start": window_start,
            "metrics": {
                "response_time_seconds": 180.0,
                "contact_rate": 0.92,
                "pickup_rate": 0.42,
                "appointment_rate": 0.28,
                "lead_acceptance_rate": 0.85,
                "call_volume": 42,
                "texts_sent": 88,
                "emails_sent": 31,
                "conversations_2min": 14,
                "appointments_set": 6,
                "new_leads_not_acted_on": 2,
                "total_zillow_leads": 21,
                "activity_points": 6 * 500 + 14 * 100 + 42 * 10 + 88 * 2 + 31 * 1,
            },
        },
        {
            "agent_id": "mock-002",
            "name": "Jordan Lee",
            "email": "jordan@example.com",
            "snapshot_date": snapshot_date,
            "window_start": window_start,
            "metrics": {
                "response_time_seconds": 540.0,
                "contact_rate": 0.71,
                "pickup_rate": 0.22,
                "appointment_rate": 0.14,
                "lead_acceptance_rate": 0.62,
                "call_volume": 19,
                "texts_sent": 35,
                "emails_sent": 12,
                "conversations_2min": 5,
                "appointments_set": 2,
                "new_leads_not_acted_on": 4,
                "total_zillow_leads": 14,
                "activity_points": 2 * 500 + 5 * 100 + 19 * 10 + 35 * 2 + 12 * 1,
            },
        },
    ]
