"""
Admin-alert email helper.

Used when the monthly cron pipeline fails: the heartbeat script calls
notify_admin_failure() with a short context blob, and the same SMTP creds
that send agent reports send the alert. Soft-fails if SMTP is not configured.
"""

from __future__ import annotations

import logging
import smtplib
import time
from email.mime.text import MIMEText

from config.settings import (
    ADMIN_EMAIL,
    BASE_DIR,
    EMAIL_FROM_ADDRESS,
    EMAIL_FROM_NAME,
    SMTP_HOST,
    SMTP_PASSWORD,
    SMTP_PORT,
    SMTP_USER,
)

log = logging.getLogger(__name__)

# Dedup: don't fire the same alert again within this window.
DEDUP_MARKER = BASE_DIR / "data" / ".last-alert"
DEDUP_WINDOW_MINUTES = 30


def _within_dedup_window() -> bool:
    if not DEDUP_MARKER.exists():
        return False
    age_minutes = (time.time() - DEDUP_MARKER.stat().st_mtime) / 60
    return age_minutes < DEDUP_WINDOW_MINUTES


def _touch_dedup_marker() -> None:
    DEDUP_MARKER.parent.mkdir(parents=True, exist_ok=True)
    DEDUP_MARKER.touch()


def notify_admin_failure(subject: str, body: str) -> bool:
    """
    Send a plain-text alert to ADMIN_EMAIL. Returns True on success.

    Soft-fails: logs and returns False if SMTP creds aren't configured, the
    send raises, or another alert was sent within DEDUP_WINDOW_MINUTES.
    """
    if not (SMTP_USER and SMTP_PASSWORD and ADMIN_EMAIL):
        log.warning("notify_admin_failure: SMTP/admin config missing — skipping send.")
        return False

    if _within_dedup_window():
        log.info("notify_admin_failure: within dedup window — skipping send.")
        return False

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = f"{EMAIL_FROM_NAME} <{EMAIL_FROM_ADDRESS}>"
    msg["To"] = ADMIN_EMAIL

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(EMAIL_FROM_ADDRESS, [ADMIN_EMAIL], msg.as_string())
        _touch_dedup_marker()
        log.info("Admin alert sent to %s", ADMIN_EMAIL)
        return True
    except smtplib.SMTPException as exc:
        log.error("notify_admin_failure: SMTP error %s", exc)
        return False
