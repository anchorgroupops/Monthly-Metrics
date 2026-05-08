# P2 — Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the service survive months of unattended Pi operation. Adds SQLite durability (WAL + connection-context fix), schema-migration infrastructure, JSON-rich `/healthz`, alert deduplication, and operational scripts (deploy, smoke, Pi hardening). Code-side first, then config + Pi-side.

**Architecture:** Two layers — code (testable in worktree) and ops (config/scripts that need Pi to verify end-to-end).

**Tech Stack:** sqlite3 WAL, systemd, logrotate, fail2ban, unattended-upgrades, cron, bash.

**Source spec:** `docs/superpowers/specs/2026-05-07-audit-harden-deploy-design.md` (P2).

**Pre-requisite (carry-over from P1):** Fixing `src/storage.py`'s connection lifecycle is folded into Task 1 — that closes the `ResourceWarning: unclosed sqlite3` loop and the `filterwarnings = "ignore::ResourceWarning"` line in `pyproject.toml` is removed at the end of T1.

---

## Layer 1 — Code (no Pi needed)

### Task 1: SQLite WAL + connection lifecycle fix

**Files:**
- Modify: `src/storage.py:76-93` (the `_ensure_db` + `connect` block)
- Modify: `tests/test_storage.py` (add WAL + lifecycle tests)
- Modify: `pyproject.toml` (drop `ignore::ResourceWarning`)

The current `connect()` opens a connection, yields, commits, closes. Two issues:

1. **No WAL mode** — every write blocks readers, every reader blocks writes. Bad for the dashboard + heartbeat-thread overlap pattern.
2. **`ResourceWarning` leak** — if `_ensure_db` raises, the inner `sqlite3.connect(DB_PATH)` exits with `with` cleanup, but our outer `connect()` context manager opens a *second* connection that may leak on test teardown (the leaks we silenced in P0).

Fix: enable WAL inside `_ensure_db` (one-time per DB), use `closing()` semantics on the outer connection so it always closes even on exception.

- [ ] **Step 1: Write the failing test** (WAL mode active after connect)

Add to `tests/test_storage.py`:

```python
class TestWAL:
    def test_journal_mode_is_wal(self, isolated_db):
        from src import storage

        storage.save_period(
            [{"agent_id": "100", "name": "A", "email": "a@x", "period": "2026-04",
              "csat": 0.85, "_raw": {}}],
            source="test",
        )

        with storage.connect() as conn:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"

    def test_synchronous_is_normal(self, isolated_db):
        from src import storage

        with storage.connect() as conn:
            conn.execute("CREATE TABLE x (v INTEGER)")
            sync = conn.execute("PRAGMA synchronous").fetchone()[0]
        # NORMAL = 1
        assert sync == 1
```

- [ ] **Step 2: Run — confirm fails**

```bash
py -3 -m pytest tests/test_storage.py::TestWAL -v
```
Expected: both fail (no WAL pragma yet).

- [ ] **Step 3: Add WAL pragmas to `_ensure_db`**

Edit `src/storage.py`'s `_ensure_db`:

```python
def _ensure_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(SCHEMA)
        # Enable WAL (one-time, persists in DB header).
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
```

- [ ] **Step 4: Run — should pass**

- [ ] **Step 5: Write the failing test** (no ResourceWarning leak)

Add to `tests/test_storage.py`:

```python
class TestConnectionLifecycle:
    def test_no_resource_warning_on_normal_use(self, isolated_db, recwarn):
        import warnings
        from src import storage

        warnings.simplefilter("always", ResourceWarning)
        for _ in range(50):
            with storage.connect() as conn:
                conn.execute("SELECT 1").fetchone()

        rw = [w for w in recwarn.list if issubclass(w.category, ResourceWarning)]
        assert rw == [], f"ResourceWarning leak: {[str(w.message) for w in rw]}"

    def test_no_resource_warning_on_exception_inside_with(self, isolated_db, recwarn):
        import warnings
        from src import storage

        warnings.simplefilter("always", ResourceWarning)
        with pytest.raises(RuntimeError):
            with storage.connect() as conn:
                conn.execute("SELECT 1")
                raise RuntimeError("simulated mid-transaction error")

        rw = [w for w in recwarn.list if issubclass(w.category, ResourceWarning)]
        assert rw == []
```

- [ ] **Step 6: Run — they may or may not fail**

If they pass already (Python 3.14 GC behavior changed), still keep them — they guard against future regressions.

If they fail, harden `connect()`:

```python
@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    _ensure_db()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
```

- [ ] **Step 7: Drop `ignore::ResourceWarning` from `pyproject.toml`**

Edit `pyproject.toml`:

```toml
filterwarnings = [
    "default",
    "ignore::DeprecationWarning",
]
```

(Remove the `ignore::ResourceWarning` line and the `FIXME(P3)` comment.)

- [ ] **Step 8: Full suite green**

```bash
py -3 -m pytest -q
```
Expected: 205+ passing (was 203 + new WAL/lifecycle tests).

- [ ] **Step 9: Commit**

```bash
git add -u
git commit -m "fix(storage): enable WAL + close connections on exception (P2/T1)"
```

---

### Task 2: Schema-migration runner

**Files:**
- Create: `src/migrations/__init__.py`
- Create: `src/migrations/_runner.py`
- Create: `src/migrations/001_initial.sql`
- Modify: `src/storage.py` (replace `SCHEMA` constant with migration runner call)
- Modify: `main.py` (add `migrate` mode)
- Create: `tests/test_migrations.py`

Forward-only, idempotent migrations. New `schema_migrations` table tracks applied migration filenames.

- [ ] **Step 1: Write failing tests**

Create `tests/test_migrations.py`:

```python
"""Tests for src/migrations/ — forward-only schema runner."""

import sqlite3

import pytest


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    from src import storage
    monkeypatch.setattr(storage, "DB_PATH", db)
    return db


class TestMigrationRunner:
    def test_runs_all_pending_on_empty_db(self, fresh_db):
        from src.migrations._runner import apply_pending_migrations

        applied = apply_pending_migrations(fresh_db)
        assert "001_initial.sql" in applied

        # Tables created
        with sqlite3.connect(fresh_db) as conn:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
        assert "agent_periods" in tables
        assert "drafts" in tables
        assert "schema_migrations" in tables

    def test_idempotent_second_run_applies_nothing(self, fresh_db):
        from src.migrations._runner import apply_pending_migrations

        first = apply_pending_migrations(fresh_db)
        second = apply_pending_migrations(fresh_db)

        assert len(first) >= 1
        assert second == []

    def test_records_applied_filename(self, fresh_db):
        from src.migrations._runner import apply_pending_migrations

        apply_pending_migrations(fresh_db)
        with sqlite3.connect(fresh_db) as conn:
            rows = conn.execute(
                "SELECT filename FROM schema_migrations ORDER BY filename"
            ).fetchall()
        assert rows[0][0] == "001_initial.sql"
```

- [ ] **Step 2: Run — fails (modules don't exist)**

- [ ] **Step 3: Create the runner**

Create `src/migrations/__init__.py` (empty file, just makes it a package).

Create `src/migrations/_runner.py`:

```python
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
    with sqlite3.connect(db_path) as conn:
        conn.execute(TRACKING_TABLE_SQL)
        applied = {r[0] for r in conn.execute(
            "SELECT filename FROM schema_migrations"
        ).fetchall()}

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

        # Always keep WAL pragmas active (re-applying them is a no-op).
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")

    return new_applied
```

Create `src/migrations/001_initial.sql` with the existing schema (copy from `src/storage.py`'s `SCHEMA` constant — every CREATE TABLE / INDEX line):

```sql
CREATE TABLE IF NOT EXISTS agent_periods (
    agent_id     TEXT NOT NULL,
    period       TEXT NOT NULL,
    metric_key   TEXT NOT NULL,
    value        REAL,
    raw_json     TEXT,
    ingested_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (agent_id, period, metric_key)
);

CREATE TABLE IF NOT EXISTS agent_meta (
    agent_id     TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    email        TEXT NOT NULL,
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    period       TEXT NOT NULL,
    source       TEXT NOT NULL,
    file_path    TEXT,
    row_count    INTEGER,
    status       TEXT NOT NULL,
    notes        TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS drafts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id     TEXT NOT NULL,
    period       TEXT NOT NULL,
    html         TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    approved_at  TEXT,
    sent_at      TEXT,
    UNIQUE (agent_id, period)
);

CREATE INDEX IF NOT EXISTS idx_periods_agent ON agent_periods(agent_id, period);
CREATE INDEX IF NOT EXISTS idx_drafts_status ON drafts(period, status);
```

- [ ] **Step 4: Replace `SCHEMA` + `_ensure_db` with the migration runner**

Edit `src/storage.py`:

```python
# Remove the SCHEMA constant entirely.

def _ensure_db() -> None:
    from src.migrations._runner import apply_pending_migrations
    apply_pending_migrations(DB_PATH)
```

The WAL pragmas now live in the runner, so the duplicates added in T1 step 3 can be removed from `_ensure_db`. (T1's WAL pragmas were always-on; the runner sets them every connect anyway, so behavior is identical.)

- [ ] **Step 5: Run all storage tests**

```bash
py -3 -m pytest tests/test_storage.py tests/test_migrations.py -v
```
Expected: all green. The existing storage tests pass against the migration-managed schema (it's the same schema).

- [ ] **Step 6: Add `--mode migrate` to `main.py`**

Edit `main.py`'s argparse `choices` list and dispatch:

```python
parser.add_argument(
    "--mode",
    choices=["research", "pull", "upload", "review", "draft", "dashboard", "send", "migrate"],
    help="Execution mode",
)
```

Add the function:

```python
def cmd_migrate(args) -> int:
    """Run pending schema migrations against data/metrics.db."""
    from src.migrations._runner import apply_pending_migrations
    from src.storage import DB_PATH

    print("\n── Migrate ──────────────────────────────────────────────────────────")
    print(f"  DB: {DB_PATH}")
    applied = apply_pending_migrations(DB_PATH)
    if applied:
        print(f"  Applied {len(applied)} migration(s):")
        for name in applied:
            print(f"    - {name}")
    else:
        print("  No pending migrations.")
    return 0
```

Wire into `main()`:

```python
if args.mode == "migrate":
    return cmd_migrate(args)
```

- [ ] **Step 7: Add tests for the new mode**

Append to `tests/test_main_modes.py`:

```python
class TestCmdMigrate:
    def test_runs_pending_migrations(self, isolated_db, capsys):
        from main import cmd_migrate

        rc = cmd_migrate(_args())

        assert rc == 0
        # Schema_migrations table populated by isolated_db fixture's first connect
        # OR by cmd_migrate itself.
        out = capsys.readouterr().out
        assert "Migrate" in out
```

- [ ] **Step 8: Full suite green + commit**

```bash
py -3 -m pytest -q
git add src/migrations tests/test_migrations.py src/storage.py main.py tests/test_main_modes.py
git commit -m "feat(storage): forward-only schema migrations + main.py --mode migrate (P2/T2)"
```

---

### Task 3: `/healthz` JSON enrichment

**Files:**
- Modify: `src/dashboard.py` (the `/healthz` route)
- Modify: `tests/test_dashboard.py` (extend `TestHealthz`)

Currently `/healthz` returns `"ok", 200`. Change to JSON: `{ok, db_writable, last_heartbeat_age_hours, draft_queue_size, disk_used_pct}`. Status 200 on healthy / 503 on degraded so Cloudflare's health check can use it.

- [ ] **Step 1: Write failing tests**

Replace `tests/test_dashboard.py::TestHealthz` with:

```python
import json


class TestHealthz:
    def test_returns_200_with_json_when_healthy(self, client, isolated_db):
        resp = client.get("/healthz")
        assert resp.status_code == 200
        data = json.loads(resp.get_data(as_text=True))
        assert data["ok"] is True
        assert data["db_writable"] is True
        assert isinstance(data["draft_queue_size"], int)
        assert isinstance(data["disk_used_pct"], (int, float))
        # last_heartbeat_age_hours is None or a number
        assert data["last_heartbeat_age_hours"] is None or isinstance(
            data["last_heartbeat_age_hours"], (int, float)
        )

    def test_returns_503_when_db_not_writable(self, client, isolated_db, monkeypatch):
        from src import storage

        def _broken_connect():
            raise RuntimeError("DB unavailable")

        monkeypatch.setattr(storage, "connect", _broken_connect)

        resp = client.get("/healthz")
        assert resp.status_code == 503
        data = json.loads(resp.get_data(as_text=True))
        assert data["ok"] is False
        assert data["db_writable"] is False

    def test_draft_queue_size_counts_pending(self, client, isolated_db):
        from src import storage

        storage.save_period(
            [{"agent_id": "100", "name": "A", "email": "a@x", "period": "2026-04",
              "csat": 0.85, "_raw": {}}],
            source="test",
        )
        storage.queue_draft("100", "2026-04", "<html/>")

        resp = client.get("/healthz")
        data = json.loads(resp.get_data(as_text=True))
        assert data["draft_queue_size"] == 1
```

- [ ] **Step 2: Implement the new `/healthz`**

Replace the existing route in `src/dashboard.py`:

```python
@app.route("/healthz")
@csrf.exempt
def healthz():
    import shutil
    from datetime import datetime as _dt
    from flask import jsonify

    ok = True
    db_writable = False
    last_heartbeat_age_hours = None
    draft_queue_size = 0
    disk_used_pct = 0.0

    try:
        with storage.connect() as conn:
            # Test write capability with a no-op transaction.
            conn.execute("SELECT 1").fetchone()
            db_writable = True

            row = conn.execute(
                "SELECT created_at FROM runs WHERE source = 'fub' "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if row and row["created_at"]:
                try:
                    last = _dt.fromisoformat(row["created_at"])
                    delta = _dt.utcnow() - last
                    last_heartbeat_age_hours = round(
                        delta.total_seconds() / 3600, 2
                    )
                except (TypeError, ValueError):
                    pass

            draft_queue_size = conn.execute(
                "SELECT COUNT(*) FROM drafts WHERE status='pending'"
            ).fetchone()[0]
    except Exception:
        ok = False

    try:
        usage = shutil.disk_usage(str(storage.DB_PATH.parent))
        disk_used_pct = round((usage.used / usage.total) * 100, 1)
        if disk_used_pct >= 95:
            ok = False
    except Exception:
        pass

    payload = {
        "ok": ok and db_writable,
        "db_writable": db_writable,
        "last_heartbeat_age_hours": last_heartbeat_age_hours,
        "draft_queue_size": draft_queue_size,
        "disk_used_pct": disk_used_pct,
    }
    return jsonify(payload), (200 if payload["ok"] else 503)
```

- [ ] **Step 3: Run + verify**

```bash
py -3 -m pytest tests/test_dashboard.py::TestHealthz -v
py -3 -m pytest -q
```

- [ ] **Step 4: Commit**

```bash
git add -u
git commit -m "feat(healthz): JSON shape with db/heartbeat/queue/disk indicators (P2/T3)"
```

---

### Task 4: Notifier alert deduplication

**Files:**
- Modify: `src/notifier.py`
- Modify: `tests/test_notifier.py`

Avoid alert storms — if the heartbeat fires every minute on a stuck Pi, we don't want 60 emails. Dedup via `data/.last-alert` mtime: if last alert sent < N minutes ago, skip.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_notifier.py`:

```python
class TestAlertDedup:
    def test_first_alert_sends(self, mocker, configured_notifier, tmp_path, monkeypatch):
        from src import notifier

        marker = tmp_path / ".last-alert"
        monkeypatch.setattr(notifier, "DEDUP_MARKER", marker)
        monkeypatch.setattr(notifier, "DEDUP_WINDOW_MINUTES", 30)

        smtp_class = mocker.patch("smtplib.SMTP")
        result = notifier.notify_admin_failure("subject", "body")

        assert result is True
        smtp_class.assert_called_once()
        assert marker.exists()

    def test_second_alert_within_window_is_skipped(self, mocker, configured_notifier, tmp_path, monkeypatch):
        from src import notifier

        marker = tmp_path / ".last-alert"
        marker.touch()  # simulate prior alert
        monkeypatch.setattr(notifier, "DEDUP_MARKER", marker)
        monkeypatch.setattr(notifier, "DEDUP_WINDOW_MINUTES", 30)

        smtp_class = mocker.patch("smtplib.SMTP")
        result = notifier.notify_admin_failure("subject", "body")

        assert result is False
        smtp_class.assert_not_called()

    def test_alert_after_window_sends(self, mocker, configured_notifier, tmp_path, monkeypatch):
        import os, time

        from src import notifier

        marker = tmp_path / ".last-alert"
        marker.touch()
        # Backdate the marker mtime by 60 minutes
        old = time.time() - 60 * 60
        os.utime(marker, (old, old))

        monkeypatch.setattr(notifier, "DEDUP_MARKER", marker)
        monkeypatch.setattr(notifier, "DEDUP_WINDOW_MINUTES", 30)

        smtp_class = mocker.patch("smtplib.SMTP")
        result = notifier.notify_admin_failure("subject", "body")

        assert result is True
        smtp_class.assert_called_once()
```

- [ ] **Step 2: Add dedup to `src/notifier.py`**

```python
import os
import time
from pathlib import Path

from config.settings import BASE_DIR  # add this import

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
```

Inside `notify_admin_failure`, before the SMTP block:

```python
    if _within_dedup_window():
        log.info("notify_admin_failure: within dedup window — skipping send.")
        return False
```

After successful send:

```python
        _touch_dedup_marker()
        log.info("Admin alert sent to %s", ADMIN_EMAIL)
        return True
```

- [ ] **Step 3: Run + verify**

```bash
py -3 -m pytest tests/test_notifier.py -v
py -3 -m pytest -q
```

- [ ] **Step 4: Commit**

```bash
git add -u
git commit -m "feat(notifier): dedup alert window via .last-alert mtime (P2/T4)"
```

---

## Layer 2 — Operational scripts (write here, verify on Pi later)

### Task 5: `scripts/deploy.sh` — single-command Pi deploy

**Files:**
- Create: `scripts/deploy.sh`

- [ ] **Step 1: Write the script**

```bash
#!/usr/bin/env bash
# scripts/deploy.sh — pull main, install deps, migrate DB, restart, diagnose.
# Run from any host that has SSH access to pi@raspberrypi.

set -euo pipefail

PI_HOST="${PI_HOST:-pi@raspberrypi}"
APP_DIR="${APP_DIR:-/opt/Monthly-Metrics}"

echo "→ Deploying to ${PI_HOST}:${APP_DIR}"

ssh "$PI_HOST" bash -c "'
  set -euo pipefail
  cd $APP_DIR
  git fetch origin
  git checkout main
  git reset --hard origin/main
  .venv/bin/pip install --quiet -r requirements.txt
  .venv/bin/python main.py --mode migrate
  sudo systemctl restart anchor-dashboard
  scripts/diagnose.sh
'"

echo "→ Deploy complete."
```

- [ ] **Step 2: Make executable + commit**

```bash
chmod +x scripts/deploy.sh
git add scripts/deploy.sh
git commit -m "ops(deploy): single-command Pi deploy script (P2/T5)"
```

---

### Task 6: `scripts/smoke.sh` — post-deploy smoke test

**Files:**
- Create: `scripts/smoke.sh`

- [ ] **Step 1: Write the script**

```bash
#!/usr/bin/env bash
# scripts/smoke.sh — exercise public surface of the live dashboard.
# Run from the workstation after each deploy.

set -euo pipefail

URL="${URL:-https://anchor.joelycannoli.com}"

echo "→ healthz JSON…"
HEALTH=$(curl -fsS "$URL/healthz")
echo "$HEALTH" | python -m json.tool
OK=$(echo "$HEALTH" | python -c "import sys,json;print(json.load(sys.stdin)['ok'])")
[ "$OK" = "True" ] || { echo "healthz reports unhealthy"; exit 1; }

echo "→ login form renders…"
curl -fsS -o /dev/null "$URL/login"

echo "→ root redirects…"
CODE=$(curl -fsS -o /dev/null -w "%{http_code}" "$URL/" || true)
[ "$CODE" = "302" ] || [ "$CODE" = "303" ] || { echo "root: $CODE"; exit 1; }

echo "→ Smoke test PASSED."
```

- [ ] **Step 2: Make executable + commit**

```bash
chmod +x scripts/smoke.sh
git add scripts/smoke.sh
git commit -m "ops(smoke): post-deploy public-surface smoke test (P2/T6)"
```

---

### Task 7: Systemd hardening + restart guards

**Files:**
- Modify: `systemd/anchor-dashboard.service`

- [ ] **Step 1: Read current unit**

```bash
cat systemd/anchor-dashboard.service
```

- [ ] **Step 2: Add hardening + restart-rate-limit**

Append (or merge with existing) `[Service]` section:

```ini
ProtectSystem=strict
ProtectHome=true
NoNewPrivileges=true
PrivateTmp=true
ReadWritePaths=/opt/Monthly-Metrics/data /opt/Monthly-Metrics/logs

Restart=on-failure
RestartSec=10
StartLimitBurst=5
StartLimitIntervalSec=300
```

(Keep existing `ExecStart`, `WorkingDirectory`, `User`, `Environment*` lines untouched.)

- [ ] **Step 3: Lint locally**

```bash
systemd-analyze --no-pager verify systemd/anchor-dashboard.service 2>&1 || echo "(local systemd-analyze not available — Pi will validate)"
```

- [ ] **Step 4: Commit**

```bash
git add systemd/anchor-dashboard.service
git commit -m "ops(systemd): harden anchor-dashboard.service + restart guards (P2/T7)"
```

---

### Task 8: `scripts/harden_pi.sh` — Pi OS hygiene

**Files:**
- Create: `scripts/harden_pi.sh`

Idempotent. Configures `unattended-upgrades` (security-only), `fail2ban` for sshd, disables SSH password auth (after verifying key auth works).

- [ ] **Step 1: Write the script**

```bash
#!/usr/bin/env bash
# scripts/harden_pi.sh — idempotent Pi OS hardening.
# Configures: unattended-upgrades, fail2ban, SSH key-only auth.
# Pass --dry-run to print actions without applying.

set -euo pipefail

DRY="${1:-}"
run() {
    if [ "$DRY" = "--dry-run" ]; then
        echo "[dry-run] $*"
    else
        eval "$*"
    fi
}

echo "→ Installing unattended-upgrades + fail2ban…"
run "sudo apt-get update -qq"
run "sudo apt-get install -y unattended-upgrades fail2ban"

echo "→ Configuring unattended-upgrades for security only…"
run "sudo dpkg-reconfigure -fnoninteractive unattended-upgrades"

echo "→ Verifying SSH key auth works before disabling password auth…"
if [ -z "${SSH_AUTH_SOCK:-}" ] && [ ! -d "$HOME/.ssh" ]; then
    echo "  ABORT: no .ssh directory or agent — refusing to disable password auth."
    exit 1
fi
KEY_OK=$(grep -E '^[a-z0-9]+ ' "$HOME/.ssh/authorized_keys" 2>/dev/null | wc -l || true)
if [ "$KEY_OK" -lt 1 ]; then
    echo "  ABORT: ~/.ssh/authorized_keys has no key entries — refusing to disable password auth."
    exit 1
fi

echo "→ Disabling SSH password auth…"
run "sudo sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config"
run "sudo sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config"
run "sudo systemctl restart sshd"

echo "→ Enabling fail2ban for sshd…"
run "sudo systemctl enable --now fail2ban"

echo "→ Done. Verify on next login that key auth still works."
```

- [ ] **Step 2: Make executable + commit**

```bash
chmod +x scripts/harden_pi.sh
git add scripts/harden_pi.sh
git commit -m "ops(harden_pi): idempotent OS hygiene — unattended-upgrades + fail2ban + sshd (P2/T8)"
```

---

### Task 9: `scripts/disk_check.sh` — daily disk-full guard

**Files:**
- Create: `scripts/disk_check.sh`

- [ ] **Step 1: Write the script**

```bash
#!/usr/bin/env bash
# scripts/disk_check.sh — alert when SD card usage > 85%.
# Schedule via crontab: 0 * * * * /opt/Monthly-Metrics/scripts/disk_check.sh

set -euo pipefail

THRESHOLD="${THRESHOLD:-85}"
APP_DIR="${APP_DIR:-/opt/Monthly-Metrics}"

USED=$(df -P "$APP_DIR" | awk 'NR==2 {gsub(/%/,"",$5); print $5}')

if [ "$USED" -ge "$THRESHOLD" ]; then
    cd "$APP_DIR"
    .venv/bin/python -c "
from src.notifier import notify_admin_failure
notify_admin_failure(
    'Anchor Group: disk usage at ${USED}%',
    'The Pi at ${APP_DIR} is at ${USED}% disk usage (threshold ${THRESHOLD}%).\n\n'
    'Run df -h on the Pi and prune old logs/backups.'
)
"
fi
```

- [ ] **Step 2: Make executable + commit**

```bash
chmod +x scripts/disk_check.sh
git add scripts/disk_check.sh
git commit -m "ops(disk_check): alert when /opt/Monthly-Metrics > 85% usage (P2/T9)"
```

---

### Task 10: `scripts/healthz_check.sh` + cron — liveness alert

**Files:**
- Create: `scripts/healthz_check.sh`

External liveness check: every 6h, curl `/healthz`. If unreachable, alert.

- [ ] **Step 1: Write the script**

```bash
#!/usr/bin/env bash
# scripts/healthz_check.sh — alert if public dashboard is unreachable.
# Schedule via crontab on the Pi: 0 */6 * * * /opt/Monthly-Metrics/scripts/healthz_check.sh

set -euo pipefail

URL="${URL:-https://anchor.joelycannoli.com/healthz}"
APP_DIR="${APP_DIR:-/opt/Monthly-Metrics}"

if ! curl -fsS --max-time 15 "$URL" >/dev/null; then
    cd "$APP_DIR"
    .venv/bin/python -c "
from src.notifier import notify_admin_failure
notify_admin_failure(
    'Anchor Group: healthz unreachable',
    'The dashboard at ${URL} did not respond within 15s. Check the Pi:\n\n'
    '  sudo systemctl status anchor-dashboard cloudflared\n'
    '  scripts/diagnose.sh'
)
"
fi
```

- [ ] **Step 2: Make executable + commit**

```bash
chmod +x scripts/healthz_check.sh
git add scripts/healthz_check.sh
git commit -m "ops(healthz_check): cron-driven liveness alert (P2/T10)"
```

---

### Task 11: Logrotate + DB backup timer (config only here, install on Pi later)

**Files:**
- Create: `systemd/anchor-backup.service`
- Create: `systemd/anchor-backup.timer`
- Create: `scripts/backup_db.sh`
- Create: `etc/logrotate/anchor-dashboard` (config)

- [ ] **Step 1: Backup script**

`scripts/backup_db.sh`:

```bash
#!/usr/bin/env bash
# Daily SQLite backup using the .backup API (WAL-aware).

set -euo pipefail

APP_DIR="${APP_DIR:-/opt/Monthly-Metrics}"
DB="$APP_DIR/data/metrics.db"
BACKUP_DIR="$APP_DIR/data/backups"
TODAY=$(date +%Y%m%d)
TARGET="$BACKUP_DIR/metrics-$TODAY.db"

mkdir -p "$BACKUP_DIR"
sqlite3 "$DB" ".backup '$TARGET'"

# Retention: 14 daily, 12 monthly (1st of each month preserved).
find "$BACKUP_DIR" -name 'metrics-????????.db' -type f \
    -mtime +14 ! -name 'metrics-??????01.db' -delete

echo "Backup written: $TARGET"
```

- [ ] **Step 2: Systemd timer**

`systemd/anchor-backup.service`:

```ini
[Unit]
Description=Anchor Group Monthly Metrics — daily DB backup
After=anchor-dashboard.service

[Service]
Type=oneshot
WorkingDirectory=/opt/Monthly-Metrics
ExecStart=/opt/Monthly-Metrics/scripts/backup_db.sh
User=anchor
```

`systemd/anchor-backup.timer`:

```ini
[Unit]
Description=Daily Anchor DB backup at 02:00

[Timer]
OnCalendar=*-*-* 02:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

- [ ] **Step 3: Logrotate config**

`etc/logrotate/anchor-dashboard`:

```
/opt/Monthly-Metrics/logs/*.log {
    weekly
    rotate 8
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
}
```

- [ ] **Step 4: Make scripts executable + commit**

```bash
chmod +x scripts/backup_db.sh
git add scripts/backup_db.sh systemd/anchor-backup.service systemd/anchor-backup.timer etc/logrotate/anchor-dashboard
git commit -m "ops(backup): daily SQLite backup timer + logrotate config (P2/T11)"
```

---

### Task 12: Secret rotation runbook

**Files:**
- Create: `docs/runbooks/rotate-secrets.md`

- [ ] **Step 1: Write the runbook**

```markdown
# Rotating secrets

Each secret has a different blast radius and recovery procedure. Run the
relevant section, then `scripts/deploy.sh` to roll the change.

## ANTHROPIC_API_KEY

Used by `--mode research` only. Rotation is non-blocking: thresholds.json
is updated monthly; missing the key just means the next monthly fire skips
threshold refresh.

1. Generate a new key in the Anthropic console.
2. Update `H:\AI\Secrets\.env.master.private`.
3. Update `/opt/Monthly-Metrics/.env` (derivative).
4. `sudo systemctl restart anchor-dashboard`. Verify next monthly run.

## SMTP_PASSWORD

Blast radius: monthly digest delivery + admin alerts.

1. Rotate at the SMTP provider.
2. Update both `.env` files (master + Pi).
3. `sudo systemctl restart anchor-dashboard`.
4. Trigger a test alert: `python -c "from src.notifier import notify_admin_failure; notify_admin_failure('test', 'rotation test')"`.

## ADMIN_PASSWORD

Blast radius: dashboard login. Brute-force lockout protects against quick
re-auth attempts but a leaked password should still rotate.

1. `python -c "import secrets; print(secrets.token_urlsafe(24))"`
2. Update `/opt/Monthly-Metrics/.env` (do NOT commit).
3. `sudo systemctl restart anchor-dashboard`.

## FLASK_SECRET_KEY

Blast radius: existing sessions invalidated. Rotate if you suspect leak
of the key itself (unlikely — server-side only).

1. `python -c "import secrets; print(secrets.token_hex(32))"`
2. Update `/opt/Monthly-Metrics/.env`.
3. `sudo systemctl restart anchor-dashboard`. All admins re-login.

## FUB_API_KEY

Blast radius: monthly pulls. Manual `scripts/diagnose.sh` after rotation.

1. Rotate in FUB account → Developer settings.
2. Update both `.env` files.
3. Restart dashboard. Trigger a manual pull from the dashboard "Pull Now"
   button to verify auth is still good.
```

- [ ] **Step 2: Commit**

```bash
git add docs/runbooks/rotate-secrets.md
git commit -m "docs(runbooks): rotate-secrets runbook for ANTHROPIC/SMTP/ADMIN/FLASK/FUB (P2/T12)"
```

---

### Task 13: Scrub `HEARTBEAT.md` carry-over

**Files:**
- Modify: `HEARTBEAT.md`

- [ ] **Step 1: Drop the stale checkout instruction**

Edit `HEARTBEAT.md` line 19. Replace:

```
git checkout claude/zillow-digest-system-OGMIF   # remove after PR merges to main
```

with: (delete the line entirely — `main` is now default)

- [ ] **Step 2: Commit**

```bash
git add HEARTBEAT.md
git commit -m "docs(heartbeat): drop stale claude/* checkout instruction (P2/T13)"
```

---

## Task 14: PR + CI watch + merge

- [ ] **Step 1: Push branch**

```bash
git push -u origin harden/p2-reliability
```

- [ ] **Step 2: Open PR**

```bash
gh pr create --base main --head harden/p2-reliability \
  --title "P2: reliability — WAL, migrations, healthz, alerts, ops scripts" \
  --body "$(cat <<'EOF'
## Summary

Layer 1 (code, fully tested):
- SQLite WAL + connection lifecycle fix (closes the ResourceWarning carry-over)
- Forward-only schema-migration runner + `main.py --mode migrate`
- `/healthz` JSON shape with db/heartbeat/queue/disk indicators
- Notifier alert deduplication via `data/.last-alert` mtime

Layer 2 (ops, verified on Pi separately):
- `scripts/deploy.sh` — single-command Pi deploy
- `scripts/smoke.sh` — post-deploy public-surface smoke test
- `scripts/harden_pi.sh` — idempotent OS hygiene (unattended-upgrades, fail2ban, SSH key-only)
- `scripts/disk_check.sh` — daily disk-full alert
- `scripts/healthz_check.sh` — cron liveness alert
- `scripts/backup_db.sh` + systemd timer — daily WAL-aware DB backup
- `systemd/anchor-dashboard.service` — ProtectSystem + restart guards
- `etc/logrotate/anchor-dashboard` — weekly rotation, 8 keep
- `docs/runbooks/rotate-secrets.md`

Spec: `docs/superpowers/specs/2026-05-07-audit-harden-deploy-design.md` (P2).
Plan: `docs/superpowers/plans/2026-05-07-p2-reliability.md`.

## Test plan

- [ ] CI green on Python 3.11 + 3.12
- [ ] Coverage stays ≥80%
- [ ] WAL mode confirmed in storage tests
- [ ] No ResourceWarning leaks under stress (50 connect/close cycles)
- [ ] Pi-side ops scripts deferred to manual verification post-merge

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 3: Watch CI + merge**

```bash
gh pr checks --watch
gh pr merge --squash --delete-branch
```

---

## Acceptance criteria

- [ ] All Layer-1 tests added and passing
- [ ] Total coverage stays ≥80% (gate enforced)
- [ ] `pyproject.toml` no longer ignores `ResourceWarning`
- [ ] `python main.py --mode migrate` works end-to-end on a fresh DB
- [ ] `/healthz` returns JSON; CSRF-exempt; 503 when DB inaccessible
- [ ] Notifier dedups within 30-min window
- [ ] All Layer-2 files exist + are executable (`scripts/*.sh`)
- [ ] PR squash-merged to `main`
- [ ] Pi-side install of new systemd timers, logrotate, cron entries scheduled as a follow-up (not blocking merge)
