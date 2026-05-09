"""
SQLite helpers for the per-agent self-service portal.

Lives alongside src/storage.py (which owns the admin/ingest tables) so the
portal's auth tables stay contained. Agent identity is sourced from
agent_meta.email — we do NOT store agent passwords.

Schema is defined in src/migrations/002_agent_portal.sql and applied
automatically by storage.connect() on first use.
"""

from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime, timedelta

from src import storage

log = logging.getLogger(__name__)


# ── Agent lookup ──────────────────────────────────────────────────────────────


def find_agent_by_email(email: str) -> dict | None:
    """
    Case-insensitive lookup against agent_meta. Returns
    {"agent_id", "name", "email"} or None when no agent has logged a period yet.
    """
    target = (email or "").strip().lower()
    if not target:
        return None
    with storage.connect() as conn:
        row = conn.execute(
            "SELECT agent_id, name, email FROM agent_meta WHERE LOWER(email) = ?",
            (target,),
        ).fetchone()
    return dict(row) if row else None


def get_agent(agent_id: str) -> dict | None:
    with storage.connect() as conn:
        row = conn.execute(
            "SELECT agent_id, name, email FROM agent_meta WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()
    return dict(row) if row else None


# ── Magic links ───────────────────────────────────────────────────────────────


def create_magic_link(email: str, ttl_minutes: int) -> str:
    """Insert a single-use magic-link token. Returns the random token string."""
    token = secrets.token_urlsafe(32)
    expires = (datetime.now(UTC) + timedelta(minutes=ttl_minutes)).isoformat()
    with storage.connect() as conn:
        conn.execute(
            "INSERT INTO portal_magic_links (token, email, expires_at) VALUES (?, ?, ?)",
            (token, email.strip().lower(), expires),
        )
    return token


def consume_magic_link(token: str) -> str | None:
    """
    Atomically mark a magic-link token used. Returns the email it was issued
    for, or None if the token is missing, expired, or already consumed.
    """
    if not token:
        return None
    now_iso = datetime.now(UTC).isoformat()
    with storage.connect() as conn:
        row = conn.execute(
            "SELECT email, expires_at, used_at FROM portal_magic_links WHERE token = ?",
            (token,),
        ).fetchone()
        if not row or row["used_at"] is not None or row["expires_at"] < now_iso:
            return None
        conn.execute(
            "UPDATE portal_magic_links SET used_at = ? WHERE token = ? AND used_at IS NULL",
            (now_iso, token),
        )
        return row["email"]


# ── Sessions ──────────────────────────────────────────────────────────────────


def create_session(agent_id: str, ttl_days: int) -> str:
    token = secrets.token_urlsafe(32)
    expires = (datetime.now(UTC) + timedelta(days=ttl_days)).isoformat()
    with storage.connect() as conn:
        conn.execute(
            "INSERT INTO portal_sessions (token, agent_id, expires_at) VALUES (?, ?, ?)",
            (token, agent_id, expires),
        )
    return token


def lookup_session(token: str) -> dict | None:
    """Return agent dict for a valid session token, else None."""
    if not token:
        return None
    now_iso = datetime.now(UTC).isoformat()
    with storage.connect() as conn:
        row = conn.execute(
            """
            SELECT a.agent_id, a.name, a.email
            FROM portal_sessions s
            JOIN agent_meta a ON a.agent_id = s.agent_id
            WHERE s.token = ? AND s.expires_at > ?
            """,
            (token, now_iso),
        ).fetchone()
    return dict(row) if row else None


def delete_session(token: str) -> None:
    if not token:
        return
    with storage.connect() as conn:
        conn.execute("DELETE FROM portal_sessions WHERE token = ?", (token,))
