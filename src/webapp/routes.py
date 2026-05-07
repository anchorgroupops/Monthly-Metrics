"""
HTTP routes for the agent dashboard.

Routes:
    GET  /            → / dashboard or /login depending on session
    GET  /login       → email entry form
    POST /login       → issue + email magic link (no enumeration leak)
    GET  /verify      → consume magic-link token and start session
    GET  /dashboard   → require session, render the agent's dashboard
    POST /logout      → clear session
    GET  /healthz     → 200 OK for tunnel/systemd health checks
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from config.settings import (
    BRAND,
    DASHBOARD_TREND_MONTHS,
    SESSION_COOKIE_NAME,
    SESSION_TTL_DAYS,
    WEB_BASE_URL,
)
from src import auth, storage
from src.gauges import build_all_gauges
from src.metrics import (
    METRIC_KEYS,
    load_thresholds,
    overall_status_color,
    score_agent,
)

log = logging.getLogger(__name__)

router = APIRouter()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _render(request: Request, template: str, **ctx) -> HTMLResponse:
    tmpl = request.app.state.jinja.get_template(template)
    return HTMLResponse(tmpl.render(**ctx))


def _cookie_is_secure() -> bool:
    """
    Browsers refuse `Set-Cookie; Secure` over plain HTTP, which would make
    the magic-link flow unable to set the session cookie during localhost
    development. Derive the flag from the configured public URL scheme so
    production (https://...) gets Secure and dev (http://localhost) doesn't.
    """
    return urlsplit(WEB_BASE_URL).scheme == "https"


def _session_cookie_kwargs() -> dict:
    return {
        "key": SESSION_COOKIE_NAME,
        "httponly": True,
        "secure": _cookie_is_secure(),
        "samesite": "lax",
        "max_age": SESSION_TTL_DAYS * 24 * 3600,
    }


_JSON_SCRIPT_ESCAPES = {
    ord("<"):  "\\u003c",
    ord(">"):  "\\u003e",
    ord("&"):  "\\u0026",
    ord(" "): "\\u2028",
    ord(" "): "\\u2029",
}


def _safe_script_json(payload) -> str:
    """
    JSON-encode a payload safely for inclusion inside an HTML <script> tag.

    `json.dumps` does not escape `</script>`, HTML comment tokens, or the JS
    line/paragraph separators (U+2028/2029). Encoding them as \\uXXXX keeps the
    output a valid JSON literal while preventing it from breaking out of the
    surrounding script element if a metric label or period ever contained
    those characters.
    """
    return json.dumps(payload).translate(_JSON_SCRIPT_ESCAPES)


# ── Auth gate ─────────────────────────────────────────────────────────────────

@router.get("/", include_in_schema=False)
def root(request: Request):
    if auth.current_agent(request):
        return RedirectResponse("/dashboard", status_code=302)
    return RedirectResponse("/login", status_code=302)


@router.get("/login", include_in_schema=False)
def login_form(request: Request):
    return _render(request, "login.html.j2", brand=BRAND, error=None)


@router.post("/login", include_in_schema=False)
async def login_submit(request: Request, email: str = Form(...)):
    import smtplib

    from src.mailer import SMTPCredentialsMissing

    try:
        auth.issue_magic_link(email)
    except (SMTPCredentialsMissing, smtplib.SMTPException) as exc:
        # Don't reveal SMTP problems to the user — log for operators and still
        # render the same page (no email enumeration either way).
        log.error("Magic-link delivery failed for %s: %s", email, exc)
    return _render(request, "verify_sent.html.j2", brand=BRAND, email=email)


@router.get("/verify", include_in_schema=False)
def verify(request: Request, token: str | None = None):
    agent = auth.verify_token(token or "")
    if not agent:
        return _render(
            request,
            "login.html.j2",
            brand=BRAND,
            error="That sign-in link is invalid or has expired. Try again.",
        )

    session_token = auth.start_session(agent["id"])
    response = RedirectResponse("/dashboard", status_code=302)
    response.set_cookie(value=session_token, **_session_cookie_kwargs())
    return response


@router.post("/logout", include_in_schema=False)
def logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        auth.end_session(token)
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response


# ── Dashboard ─────────────────────────────────────────────────────────────────

@router.get("/dashboard", include_in_schema=False)
def dashboard(request: Request):
    agent = auth.current_agent(request)
    if not agent:
        return RedirectResponse("/login", status_code=302)

    snapshot = storage.latest_snapshot(agent["id"])
    if snapshot is None:
        return _render(
            request,
            "dashboard.html.j2",
            brand=BRAND,
            agent=agent,
            empty=True,
            scored=None,
            gauges={},
            trend_payload="{}",
            as_of=None,
        )

    scored = _scored_from_snapshot(snapshot)
    gauges = build_all_gauges(scored)
    trend_payload = _build_trend_payload(agent["id"])

    return _render(
        request,
        "dashboard.html.j2",
        brand=BRAND,
        agent=agent,
        empty=False,
        scored=scored,
        gauges=gauges,
        trend_payload=_safe_script_json(trend_payload),
        as_of=snapshot["as_of_date"],
        next_refresh=_next_refresh_label(),
    )


@router.get("/healthz", include_in_schema=False)
def healthz():
    return Response("ok", media_type="text/plain")


# ── Build the dashboard view-model from stored snapshots ──────────────────────

def _scored_from_snapshot(snapshot: dict) -> dict:
    """
    Re-score a stored snapshot against the *current* thresholds so the gauges
    and overall status reflect today's bar even when reading a historical row.
    """
    raw_payload = {
        "agent_id":     snapshot.get("raw", {}).get("agent_id", ""),
        "name":         snapshot.get("raw", {}).get("name", ""),
        "email":        snapshot.get("raw", {}).get("email", ""),
        "period":       snapshot.get("period"),
        "pCVR":         snapshot["pcvr"],
        "pickup_rate":  snapshot["pickup_rate"],
        "csat":         snapshot["csat"],
        "zhl_transfers": snapshot["zhl_transfers"],
    }
    thresholds = load_thresholds()
    scored = score_agent(raw_payload, thresholds)
    scored["overall_color"] = overall_status_color(scored["overall_status"])
    return scored


def _build_trend_payload(agent_id: int) -> dict:
    """
    Return the payload consumed by Chart.js on the dashboard:
    {
        "labels": ["2025-12", "2026-01", …],
        "metrics": {
            "pCVR":          {"label": "…", "values": [...], "target": 0.035, "unit": "percent"},
            "pickup_rate":   {...},
            ...
        }
    }
    """
    rows = storage.trend_snapshots(agent_id, DASHBOARD_TREND_MONTHS)
    thresholds = load_thresholds().get("metrics", {})
    column_for_key = {
        "pCVR": "pcvr",
        "pickup_rate": "pickup_rate",
        "csat": "csat",
        "zhl_transfers": "zhl_transfers",
    }
    payload = {"labels": [r["period"] for r in rows], "metrics": {}}
    for key in METRIC_KEYS:
        cfg = thresholds.get(key, {})
        col = column_for_key[key]
        payload["metrics"][key] = {
            "label":  cfg.get("label", key),
            "unit":   cfg.get("unit", ""),
            "target": cfg.get("target"),
            "values": [r.get(col) for r in rows],
        }
    return payload


def _next_refresh_label() -> str:
    """Best-effort next-run timestamp for the daily timer (06:00 local)."""
    tomorrow = datetime.now() + timedelta(days=1)
    return tomorrow.strftime("%b %d, 06:00")
