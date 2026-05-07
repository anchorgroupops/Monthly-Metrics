"""
Magic-link authentication and browser session management.

Login flow:
  1. POST /login {email}  → issue_magic_link(email) → SMTP sends WEB_BASE_URL + token
  2. GET  /verify?token=  → consume_magic_link → start_session → set HTTP-only cookie
  3. GET  /dashboard      → require_agent dependency reads cookie → looks up session

We deliberately avoid revealing whether an email matches an active agent: every
POST /login renders the same "check your email" page.
"""

from __future__ import annotations

import logging

from config.settings import (
    MAGIC_LINK_TTL_MINUTES,
    SESSION_COOKIE_NAME,
    SESSION_TTL_DAYS,
    WEB_BASE_URL,
)
from src import storage
from src.mailer import SMTPCredentialsMissing, send_html

log = logging.getLogger(__name__)


MAGIC_LINK_SUBJECT = "Sign in to your Anchor Group dashboard"


def _email_html(magic_url: str) -> str:
    """
    Plain HTML for the magic-link email. Inline styles only — must render
    in any client (Outlook, Gmail mobile, Apple Mail) without external CSS.
    """
    return f"""<!DOCTYPE html>
<html><body style="font-family: Helvetica, Arial, sans-serif; background:#F5EDE0; padding:32px;">
  <table cellpadding="0" cellspacing="0" border="0" width="100%" style="max-width:520px;margin:0 auto;background:#fff;border-radius:10px;">
    <tr><td style="padding:28px 32px;">
      <h1 style="font-size:20px;color:#167272;margin:0 0 12px;">Anchor Group Dashboard</h1>
      <p style="font-size:15px;line-height:1.5;color:#1A1A1A;margin:0 0 18px;">
        Click the button below to sign in. This link expires in
        {MAGIC_LINK_TTL_MINUTES} minutes and can only be used once.
      </p>
      <p style="margin:24px 0;">
        <a href="{magic_url}" style="background:#167272;color:#fff;text-decoration:none;padding:12px 24px;border-radius:7px;font-weight:600;display:inline-block;">
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


def issue_magic_link(email: str) -> bool:
    """
    Issue and email a magic link if `email` matches an active agent.

    Returns True when an email was actually sent. Returns False (silently) when
    no matching agent exists — the caller should still show the same UI either
    way to avoid email-enumeration leaks.
    """
    agent = storage.get_agent_by_email(email)
    if not agent:
        log.info("Magic-link request for unknown email %s — ignoring", email)
        return False

    token = storage.create_magic_link(agent["email"], MAGIC_LINK_TTL_MINUTES)
    magic_url = f"{WEB_BASE_URL}/verify?token={token}"
    try:
        send_html(agent["email"], MAGIC_LINK_SUBJECT, _email_html(magic_url))
    except SMTPCredentialsMissing:
        log.error(
            "Cannot send magic link — SMTP credentials missing. "
            "Magic URL was: %s",
            magic_url,
        )
        raise
    log.info("Sent magic link to %s", agent["email"])
    return True


def verify_token(token: str) -> dict | None:
    """
    Validate a magic-link token. Returns the agent dict on success, else None.
    Token is consumed (marked used) atomically.
    """
    email = storage.consume_magic_link(token)
    if not email:
        return None
    return storage.get_agent_by_email(email)


def start_session(agent_id: int) -> str:
    """Create a session row, return the cookie value to set."""
    return storage.create_session(agent_id, SESSION_TTL_DAYS)


def end_session(token: str) -> None:
    storage.delete_session(token)


def current_agent(request) -> dict | None:
    """Resolve the session cookie on a Starlette/FastAPI request."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    return storage.lookup_session(token) if token else None
