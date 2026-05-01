"""
Admin-alert email helper.

Used when the monthly cron pipeline fails: the heartbeat script calls
notify_admin_failure() with a short context blob, and the same SMTP creds
that send agent reports send the alert. Soft-fails if SMTP is not configured.
"""

from __future__ import annotations

import logging
import smtplib
from email.mime.text import MIMEText

from config.settings import (
    ADMIN_EMAIL,
    EMAIL_FROM_ADDRESS,
    EMAIL_FROM_NAME,
    SMTP_HOST,
    SMTP_PASSWORD,
    SMTP_PORT,
    SMTP_USER,
)

log = logging.getLogger(__name__)


def notify_admin_failure(subject: str, body: str) -> bool:
    """
    Send a plain-text alert to ADMIN_EMAIL. Returns True on success.

    Soft-fails: logs and returns False if SMTP creds aren't configured or the
    send raises. We never want a notifier crash to mask the original failure.
    """
    if not (SMTP_USER and SMTP_PASSWORD and ADMIN_EMAIL):
        log.warning("notify_admin_failure: SMTP/admin config missing — skipping send.")
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
        log.info("Admin alert sent to %s", ADMIN_EMAIL)
        return True
    except smtplib.SMTPException as exc:
        log.error("notify_admin_failure: SMTP error %s", exc)
        return False
