"""
Agent roster loader.

Reads `config/agents.csv` (gitignored) and returns a list of agent dicts in
the shape `fub_client` and the rest of the pipeline already expect.

CSV columns:
    name, email, fub_agent_id, active

Rows where `active` is one of (0, false, no) are skipped. Blank `active` is
treated as active.

This module is the single source of truth for the roster — `config/settings.py`
also re-exports `AGENTS` populated from the same file for backward compat with
the existing fub_client and tests.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Optional

from config.settings import ROSTER_FILE


def load_agents(path: Optional[Path] = None) -> list[dict]:
    """
    Load active agents from the roster CSV. Re-reads on every call so the
    daily-sync timer picks up roster edits without a service restart.
    """
    csv_path = path or ROSTER_FILE
    if not csv_path.exists():
        return []
    out: list[dict] = []
    with csv_path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            active = (row.get("active") or "1").strip().lower()
            if active in ("0", "false", "no"):
                continue
            name = (row.get("name") or "").strip()
            email = (row.get("email") or "").strip().lower()
            if not name or not email:
                continue
            out.append({
                "name": name,
                "email": email,
                "fub_agent_id": (row.get("fub_agent_id") or "").strip() or None,
            })
    return out


def find_by_email(email: str, agents: Optional[list[dict]] = None) -> Optional[dict]:
    """Case-insensitive lookup. Returns None when not found."""
    target = email.strip().lower()
    for a in agents if agents is not None else load_agents():
        if a["email"].lower() == target:
            return a
    return None
