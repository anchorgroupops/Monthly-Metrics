"""
SMTP delivery for both monthly reports and magic-link login emails.

Single connection per `send_html_batch` call; STARTTLS on. Pulls credentials
from `config.settings` so callers stay clean.
"""

from __future__ import annotations

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Iterable

from config.settings import (
    EMAIL_FROM_ADDRESS,
    EMAIL_FROM_NAME,
    SMTP_HOST,
    SMTP_PASSWORD,
    SMTP_PORT,
    SMTP_USER,
)

log = logging.getLogger(__name__)


class SMTPCredentialsMissing(RuntimeError):
    """SMTP_USER / SMTP_PASSWORD are required for real sending."""


def _build_message(to_addr: str, subject: str, html: str) -> MIMEMultipart:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{EMAIL_FROM_NAME} <{EMAIL_FROM_ADDRESS}>"
    msg["To"] = to_addr
    msg.attach(MIMEText(html, "html", "utf-8"))
    return msg


def send_html(to_addr: str, subject: str, html: str) -> None:
    """Send a single HTML email. Opens its own SMTP connection."""
    send_html_batch([{"to": to_addr, "subject": subject, "html": html}])


def send_html_batch(messages: Iterable[dict]) -> None:
    """
    Send a batch of HTML emails over a single SMTP connection.

    Each `messages` item must be a dict with keys: to, subject, html.

    Raises SMTPCredentialsMissing if SMTP_USER / SMTP_PASSWORD are unset.
    """
    items = list(messages)
    if not items:
        return

    if not SMTP_USER or not SMTP_PASSWORD:
        raise SMTPCredentialsMissing(
            "SMTP_USER / SMTP_PASSWORD must be set to send mail. "
            "Use --dry-run to skip delivery."
        )

    log.info("Connecting to %s:%s", SMTP_HOST, SMTP_PORT)
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.ehlo()
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        for item in items:
            msg = _build_message(item["to"], item["subject"], item["html"])
            server.sendmail(EMAIL_FROM_ADDRESS, item["to"], msg.as_string())
            log.info("Sent to %s", item["to"])
