"""Forward-only migration runner.

Looks for `NNN_*.sql` files in this package directory, applies each in order
that hasn't already been recorded in `schema_migrations`.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent
TRACKING_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename   TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
)
"""


def apply_pending_migrations(db_path: Path) -> list[str]:
    """Run all *.sql files not already recorded. Returns the filenames applied."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        # PRAGMAs must run outside any transaction. Apply before any DDL.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")

        conn.execute(TRACKING_TABLE_SQL)
        applied = {r[0] for r in conn.execute("SELECT filename FROM schema_migrations").fetchall()}

        files = sorted(p for p in MIGRATIONS_DIR.glob("*.sql"))
        new_applied: list[str] = []
        for sql_file in files:
            if sql_file.name in applied:
                continue
            log.info("Applying migration %s", sql_file.name)
            conn.executescript(sql_file.read_text())
            conn.execute(
                "INSERT INTO schema_migrations (filename) VALUES (?)",
                (sql_file.name,),
            )
            new_applied.append(sql_file.name)

        conn.commit()
    finally:
        conn.close()

    return new_applied
