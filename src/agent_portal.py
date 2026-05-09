"""
Per-agent self-service portal — Flask blueprint mounted at /metrics.

Each agent enters their email on /metrics/login, gets a magic-link via SMTP
(or a server-log line in DEV mode), clicks it to land on /metrics/dashboard
which renders their gauges + 6-month trend charts. Auth is decoupled from the
admin dashboard's ADMIN_PASSWORD: a separate HTTP-only cookie backed by the
portal_sessions table.

Public surface:
    GET  /metrics                Redirect to /metrics/dashboard or /metrics/login
    GET  /metrics/login          Render email form
    POST /metrics/login          Issue magic link (no email enumeration)
    GET  /metrics/verify         Consume token, set session cookie, redirect
    GET  /metrics/dashboard      Render gauges + trends for the logged-in agent
    POST /metrics/logout         Clear cookie + delete session row
    GET  /metrics/healthz        200 OK for health checks
"""

from __future__ import annotations

import json
import logging
import os
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from flask import (
    Blueprint,
    abort,
    current_app,
    make_response,
    redirect,
    render_template,
    request,
    url_for,
)

from config.settings import (
    BRAND,
    EMAIL_FROM_ADDRESS,
    EMAIL_FROM_NAME,
    SMTP_HOST,
    SMTP_PASSWORD,
    SMTP_PORT,
    SMTP_USER,
)
from src import portal_storage, storage
from src.gauges import build_all_gauges
from src.metrics import (
    load_thresholds,
    metric_keys,
    overall_status_color,
    score_agent,
)

log = logging.getLogger(__name__)


# ── Configuration knobs ───────────────────────────────────────────────────────

PORTAL_COOKIE_NAME = "anchor_portal"
MAGIC_LINK_TTL_MINUTES = int(os.environ.get("PORTAL_MAGIC_LINK_TTL_MINUTES", "15"))
SESSION_TTL_DAYS = int(os.environ.get("PORTAL_SESSION_TTL_DAYS", "30"))
TREND_WINDOW_MONTHS = int(os.environ.get("PORTAL_TREND_MONTHS", "6"))
PORTAL_BASE_URL = os.environ.get(
    "PORTAL_BASE_URL", ""
).rstrip("/")  # e.g. https://anchor.joelycannoli.com — empty falls back to request.url_root
DEV_LOG_MAGIC_LINK = os.environ.get("DEV_LOG_MAGIC_LINK", "").lower() in (
    "1", "true", "yes",
)

MAGIC_LINK_SUBJECT = "Sign in to your Anchor Group dashboard"


# ── Blueprint ─────────────────────────────────────────────────────────────────

bp = Blueprint(
    "portal",
    __name__,
    url_prefix="/metrics",
    template_folder=None,  # falls through to the app-level templates/ dir
)


def _is_secure() -> bool:
    """Cookie Secure flag — only when the public URL is https."""
    return request.url.startswith("https://") or PORTAL_BASE_URL.startswith("https://")


def _set_session_cookie(response, token: str) -> None:
    response.set_cookie(
        PORTAL_COOKIE_NAME,
        token,
        max_age=SESSION_TTL_DAYS * 24 * 3600,
        httponly=True,
        secure=_is_secure(),
        samesite="Lax",
        path="/metrics",
    )


def _clear_session_cookie(response) -> None:
    response.delete_cookie(PORTAL_COOKIE_NAME, path="/metrics")


def _current_agent() -> dict | None:
    return portal_storage.lookup_session(request.cookies.get(PORTAL_COOKIE_NAME))


# ── Magic-link email ─────────────────────────────────────────────────────────


def _render_magic_email(magic_url: str) -> str:
    return f"""<!DOCTYPE html>
<html><body style="font-family: Helvetica, Arial, sans-serif; background:{BRAND['color_bg']}; padding:32px;">
  <table cellpadding="0" cellspacing="0" border="0" width="100%" style="max-width:520px;margin:0 auto;background:#fff;border-radius:10px;">
    <tr><td style="padding:28px 32px;">
      <h1 style="font-size:20px;color:{BRAND['color_primary']};margin:0 0 12px;">Anchor Group Dashboard</h1>
      <p style="font-size:15px;line-height:1.5;color:{BRAND['color_text']};margin:0 0 18px;">
        Click the button below to sign in. This link expires in
        {MAGIC_LINK_TTL_MINUTES} minutes and can only be used once.
      </p>
      <p style="margin:24px 0;">
        <a href="{magic_url}" style="background:{BRAND['color_primary']};color:#fff;text-decoration:none;padding:12px 24px;border-radius:7px;font-weight:600;display:inline-block;">
          Sign in
        </a>
      </p>
      <p style="font-size:12px;color:#888;line-height:1.4;margin:24px 0 0;">
        If you didn't request this, you can ignore this email — no action is needed.
      </p>
      <p style="font-size:11px;color:#aaa;word-break:break-all;margin:18px 0 0;">
        Or copy this URL into your browser:<br>{magic_url}
      </p>
    </td></tr>
  </table>
</body></html>
"""


def _send_magic_email(to_addr: str, html: str) -> None:
    """Send via the same SMTP path used by main.py / dashboard.py."""
    if not SMTP_USER or not SMTP_PASSWORD:
        if DEV_LOG_MAGIC_LINK:
            log.warning(
                "DEV mode — SMTP not configured. Magic-link email NOT sent. "
                "URL was logged at issuance time.",
            )
            return
        raise RuntimeError(
            "SMTP_USER / SMTP_PASSWORD must be set to send magic links. "
            "Set DEV_LOG_MAGIC_LINK=1 in dev to log the URL instead."
        )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = MAGIC_LINK_SUBJECT
    msg["From"] = f"{EMAIL_FROM_NAME} <{EMAIL_FROM_ADDRESS}>"
    msg["To"] = to_addr
    msg.attach(MIMEText(html, "html", "utf-8"))

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.ehlo()
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(EMAIL_FROM_ADDRESS, to_addr, msg.as_string())


def _public_origin() -> str:
    """Where the magic-link URL points — production (PORTAL_BASE_URL) or local."""
    if PORTAL_BASE_URL:
        return PORTAL_BASE_URL
    # Strip trailing slash from request.url_root so we can append /metrics/verify…
    return request.url_root.rstrip("/")


def _issue_magic_link(email: str) -> bool:
    """
    Returns True when an actual link was issued. False (silently) when the
    email doesn't match any agent — caller should still render the same UI.
    """
    agent = portal_storage.find_agent_by_email(email)
    if not agent:
        log.info("Portal magic-link request for unknown email %r — ignoring", email)
        return False

    token = portal_storage.create_magic_link(agent["email"], MAGIC_LINK_TTL_MINUTES)
    magic_url = f"{_public_origin()}/metrics/verify?token={token}"

    if DEV_LOG_MAGIC_LINK and not (SMTP_USER and SMTP_PASSWORD):
        log.warning(
            "DEV mode — magic link for %s:\n  %s",
            agent["email"], magic_url,
        )
        return True

    try:
        _send_magic_email(agent["email"], _render_magic_email(magic_url))
    except (smtplib.SMTPException, RuntimeError, OSError):
        log.exception("Magic-link delivery failed for %s", agent["email"])
        # Re-raise so the caller can render a generic error to the user.
        raise
    log.info("Sent portal magic link to %s", agent["email"])
    return True


# ── Trend payload (XSS-safe JSON encoding) ───────────────────────────────────


_JSON_SCRIPT_ESCAPES = {
    ord("<"): "\\u003c",
    ord(">"): "\\u003e",
    ord("&"): "\\u0026",
    0x2028:    "\\u2028",
    0x2029:    "\\u2029",
}


def _safe_script_json(payload) -> str:
    """JSON-encode safely for inclusion inside an HTML <script> data island."""
    return json.dumps(payload).translate(_JSON_SCRIPT_ESCAPES)


def _build_trend_payload(agent_id: str, thresholds: dict) -> dict:
    """
    Build the Chart.js payload: labels (periods, oldest→newest) and per-metric
    values + target line. Reuses storage.load_history for each metric_key.
    """
    keys = metric_keys(thresholds)
    metric_cfg = thresholds.get("metrics", {})

    # Use the union of periods across all of this agent's metrics so the
    # x-axis lines up even when one metric is missing for a given month.
    period_set: set[str] = set()
    histories: dict[str, dict[str, float]] = {}
    for key in keys:
        rows = storage.load_history(agent_id, key, TREND_WINDOW_MONTHS)
        histories[key] = {p: v for (p, v) in rows if v is not None}
        period_set.update(p for (p, _) in rows)

    labels = sorted(period_set)
    out = {"labels": labels, "metrics": {}}
    for key in keys:
        cfg = metric_cfg.get(key, {})
        out["metrics"][key] = {
            "label":  cfg.get("label", key),
            "unit":   cfg.get("unit", ""),
            "target": cfg.get("target"),
            "values": [histories[key].get(p) for p in labels],
        }
    return out


# ── Latest-period view-model ─────────────────────────────────────────────────


def _latest_period_for(agent_id: str) -> str | None:
    with storage.connect() as conn:
        row = conn.execute(
            "SELECT period FROM agent_periods WHERE agent_id = ? "
            "ORDER BY period DESC LIMIT 1",
            (agent_id,),
        ).fetchone()
    return row["period"] if row else None


def _agent_data_for(agent_id: str, period: str) -> dict:
    """Build an agent_data dict (the input shape for score_agent) for this
    agent + period out of the stored long-form metric rows."""
    with storage.connect() as conn:
        rows = conn.execute(
            "SELECT metric_key, value FROM agent_periods "
            "WHERE agent_id = ? AND period = ?",
            (agent_id, period),
        ).fetchall()
        meta = conn.execute(
            "SELECT name, email FROM agent_meta WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()

    record = {
        "agent_id": agent_id,
        "name":     meta["name"] if meta else "",
        "email":    meta["email"] if meta else "",
        "period":   period,
    }
    for r in rows:
        record[r["metric_key"]] = r["value"]
    return record


# ── Routes ────────────────────────────────────────────────────────────────────


@bp.route("/", strict_slashes=False)
def root():
    if _current_agent():
        return redirect(url_for("portal.dashboard"))
    return redirect(url_for("portal.login"))


@bp.route("/healthz")
def healthz():
    return ("ok", 200, {"Content-Type": "text/plain"})


@bp.route("/login", methods=["GET", "POST"])
def login():
    # CSRF on this form is exempt — see dashboard.create_app where the
    # blueprint is registered. POST /login from a third-party site would
    # only let them trigger an email to themselves; not a real attack vector.
    if request.method == "POST":
        email = (request.form.get("email") or "").strip()
        if not email:
            return render_template(
                "portal/login.html", brand=BRAND, error="Please enter an email.",
            )
        try:
            _issue_magic_link(email)
        except Exception:
            # Don't leak SMTP errors to the user. Operator log already has it.
            log.exception("Magic-link issuance failed for %s", email)
        return render_template(
            "portal/verify_sent.html", brand=BRAND, email=email,
        )
    return render_template("portal/login.html", brand=BRAND, error=None)


@bp.route("/verify")
def verify():
    token = request.args.get("token", "")
    email = portal_storage.consume_magic_link(token)
    if not email:
        return render_template(
            "portal/login.html",
            brand=BRAND,
            error="That sign-in link is invalid or has expired. Try again.",
        )

    agent = portal_storage.find_agent_by_email(email)
    if not agent:
        # Race: agent record was deleted after the link was minted.
        return render_template(
            "portal/login.html",
            brand=BRAND,
            error="No agent record found for that email anymore.",
        )

    session_token = portal_storage.create_session(agent["agent_id"], SESSION_TTL_DAYS)
    response = make_response(redirect(url_for("portal.dashboard")))
    _set_session_cookie(response, session_token)
    return response


@bp.route("/dashboard")
def dashboard():
    agent = _current_agent()
    if not agent:
        return redirect(url_for("portal.login"))

    thresholds = load_thresholds()
    period = _latest_period_for(agent["agent_id"])

    if not period:
        return render_template(
            "portal/dashboard.html",
            brand=BRAND,
            agent=agent,
            empty=True,
            scored=None,
            gauges={},
            trend_payload="{}",
            as_of=None,
        )

    agent_data = _agent_data_for(agent["agent_id"], period)
    scored = score_agent(agent_data, thresholds)
    scored["overall_color"] = overall_status_color(scored["overall_status"])
    gauges = build_all_gauges(scored)
    trend_payload = _build_trend_payload(agent["agent_id"], thresholds)

    return render_template(
        "portal/dashboard.html",
        brand=BRAND,
        agent=agent,
        empty=False,
        scored=scored,
        gauges=gauges,
        trend_payload=_safe_script_json(trend_payload),
        as_of=period,
    )


@bp.route("/logout", methods=["POST"])
def logout():
    token = request.cookies.get(PORTAL_COOKIE_NAME)
    if token:
        portal_storage.delete_session(token)
    response = make_response(redirect(url_for("portal.login")))
    _clear_session_cookie(response)
    return response
