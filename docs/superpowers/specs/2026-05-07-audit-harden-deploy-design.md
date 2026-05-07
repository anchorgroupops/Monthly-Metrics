# Audit, Harden & Deploy — Design

- **Date:** 2026-05-07
- **Branch (current):** `claude/zillow-digest-system-OGMIF`
- **Branch (default, stale):** `claude/notebooklm-mcp-access-js94b`
- **Live URL:** https://anchor.joelycannoli.com (responding 200)
- **Deploy target:** Raspberry Pi at `/opt/Monthly-Metrics`, reachable via Tailscale (`pi@raspberrypi`)
- **Author:** Anchor Group Ops (with Claude)

## Goal

Bring the Anchor Monthly Metrics service to a state where:

1. The codebase is high-quality (clean module boundaries, strict typing, ≥80% test coverage, no dead code, lint/format clean).
2. The deployment is reliable for months of unattended operation (durable SQLite, backups, log rotation, monitoring, OS hygiene, alerting on failure).
3. Git history is normalized (real `main`, archived `claude/*` artifacts).
4. Each change is deployable and revertable in isolation.

Out of scope (explicitly): Cloudflare Access SSO, multi-user auth, swapping SQLite for another DB, containerization, GitHub Actions self-hosted runner on the Pi, security overhaul beyond what falls out of code-quality + reliability work.

## Approach

Phased branches off a clean new `main`. Each phase = its own short-lived branch → PR → merge → deploy → verify before the next phase starts.

Phases:

| # | Branch | Purpose |
|---|---|---|
| P0 | `harden/p0-baseline` | Branch reset, CI, tooling config |
| P1 | `harden/p1-tests` | Test characterization to ≥80% coverage |
| P2 | `harden/p2-reliability` | Backups, migrations, alerts, systemd hardening, OS hygiene |
| P3 | `harden/p3-refactor` | Module boundary refactor + strict typing + dead-code sweep |
| P4 | (in-place verification, not a branch) | Per-phase deploys + final 24h soak + smoke tests |

Rationale for ordering: P0 unlocks tooling; P1 must precede P3 so the refactor is mechanically safe; P2 stands alone and is the highest reliability ROI so we land it before the bigger refactor diff.

## Architecture

### Branching architecture & rollback model

After P0:

```
main                            ← stable, deployed
└─ harden/p1-tests
└─ harden/p2-reliability
└─ harden/p3-refactor
```

Stale branches (`claude/notebooklm-mcp-access-js94b`, `claude/zillow-digest-system-OGMIF`, `claude/analyze-test-coverage-pKsDU`) are tag-archived (`archive/<old-name>`) and deleted from `origin`. A `git bundle` of the whole repo is written to `data/backups/pre-reset-<YYYYMMDD>.bundle` before any branch surgery.

**Rollback model.** Every phase merges as one commit on `main`. Rollback = `git revert <merge-sha>` + `scripts/deploy.sh` on the Pi. SQLite migrations in P2/P3 are forward-compatible only — no `DROP COLUMN`, no destructive schema change — so a code revert never strands the DB.

**Deploy contract.** Each merge to `main` is followed by a manual deploy from the workstation: `scripts/deploy.sh` (introduced in P2) does `git pull && pip install && migrate && systemctl restart && diagnose.sh`. No auto-deploy — too risky for an unattended Pi.

### Module layout (post-P3)

```
src/
  web/
    __init__.py        # app factory: create_app(config)
    auth.py            # login, brute-force lockout
    security.py        # CSRF, rate-limit, response headers (CSP, X-Frame-Options, Referrer-Policy, Permissions-Policy)
    routes/
      review.py
      upload.py
      pull.py
  storage/
    db.py              # connection, WAL, migration runner
    drafts.py          # draft state machine (queued → approved → sent)
    history.py         # historical period queries
    migrations/
      001_initial.sql
      002_<next>.sql
  scoring/
    scorer.py          # was metrics.py
    gauges.py
    thresholds.py      # threshold loader/validator
  render/
    email.py           # was email_builder.py
    deck.py            # was deck_builder.py
    review.py          # was review_mode.py
  delivery/
    smtp.py            # transactional sends
    alerts.py          # operational alerts (heartbeat-failed, healthz-down)
  fub_client.py        # unchanged
  threshold_researcher.py  # unchanged

config/
  settings.py          # pydantic.BaseSettings, env-driven
```

`main.py` and `wsgi.py` keep their roles (CLI entry point and gunicorn entry point); both call `create_app()`.

## Phase details

### P0 — Branch reset + CI baseline

Goal: clean git, automated quality gates before any code changes.

1. `git bundle create data/backups/pre-reset-<YYYYMMDD>.bundle --all` (local backup before touching origin).
2. Tag-archive each `claude/*` branch on origin (`archive/<name>`), then delete from origin.
3. Cut a fresh `main` from current `claude/zillow-digest-system-OGMIF` HEAD. Update `origin/HEAD`.
4. Add `.github/workflows/ci.yml` running on every PR to main:
   - `ruff check`
   - `ruff format --check`
   - `mypy src/`
   - `pytest -q --cov --cov-fail-under=80` (coverage gate kicks in once P1 is merged; until then `--cov-fail-under=0`)
   - Python matrix: 3.11 (Pi default) + 3.12.
5. Branch protection on `main`: required CI green + 1 review.
6. Add `pyproject.toml` with `ruff`, `mypy`, `pytest`, `coverage` config so local + CI agree.
7. Update `architecture/SOP.md` to reference the new `main` (currently lies about the default).
8. Drop the stray `COMMIT_EDITMSG` from the worktree; ensure `.gitignore` covers it.

**Artifact:** PR #1 — small, mostly config. No app behavior change.

### P1 — Test characterization

Goal: lock current behavior with tests *before* refactoring.

| Module | Test target |
|---|---|
| `storage.py` | Round-trip persist + retrieve; draft state machine (`queued → approved → sent`); idempotent re-ingest. |
| `dashboard.py` | Login flow; CSRF enforcement; brute-force lockout (existing 429); upload→draft→approve happy path via Flask test client. |
| `email_builder.py` | Snapshot test — render with a fixed agent + period; golden HTML in `tests/snapshots/`. |
| `deck_builder.py` | Snapshot — same pattern. |
| `gauges.py` | SVG output stable for representative score/threshold combinations. |
| `notifier.py` | SMTP dry-run mock; assert headers, single-recipient, no BCC leak. |
| `threshold_researcher.py` | Mock the Anthropic call; assert the JSON round-trips through `thresholds.json`. |
| `fub_client.py` | Mock httpx; pagination + auth header + retry. |

Coverage target: **≥80% line, 100% on `storage`/`dashboard`/`email_builder`** (privacy/integrity-critical surfaces). Enforced in CI by raising `--cov-fail-under=80` once this PR merges.

Test seams added to source only where strictly necessary (e.g., dependency-inject a clock or SMTP client). Anything else stays for P3.

**Artifact:** PR #2 — tests + fixtures only.

### P2 — Reliability layer

Goal: survive months of unattended Pi operation.

| Concern | Mechanism |
|---|---|
| **SQLite durability** | Enable WAL at connect time (`PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL`). Backups via `sqlite3 .backup` API (not file copy) so they're WAL-aware. |
| **DB backups** | Daily 02:00 systemd timer → `data/backups/metrics-YYYYMMDD.db`; retain 14 daily + 12 monthly. Optional `rclone` sync to a Cloudflare R2 bucket (config-flag, off by default). |
| **Schema migrations** | New `src/migrations/` (still flat layout in P2 — moves under `src/storage/migrations/` in P3) with numbered SQL files. Tiny migrator invoked via `main.py --mode migrate` (stable CLI surface across phases) and on app start. Forward-only; no `DROP COLUMN`. |
| **Log rotation** | Existing `logs/heartbeat-*.log` already date-stamped. Add `logs/dashboard.log` from gunicorn. `logrotate` config installed by `install_dashboard_service.sh`: weekly, keep 8, compress. |
| **Heartbeat-failure alert** | `heartbeat.sh` exit-code-checks already; on non-zero, call `alerts.send(subject, body)` (SMTP to `ALERT_EMAIL`, defaults to `EMAIL_FROM`). Dedup via `data/.last-alert` mtime — one alert per failure window. |
| **Liveness alert** | Cron on the Pi: every 6h, curl `https://anchor.joelycannoli.com/healthz`; on failure send the same alert. Catches "Pi died silently". |
| **healthz enrichment** | Returns JSON: `{ok, db_writable, last_heartbeat_age_hours, draft_queue_size, disk_used_pct}`. Status code 200 healthy / 503 degraded so Cloudflare can use it. |
| **systemd hardening** | Add to `anchor-dashboard.service`: `ProtectSystem=strict`, `ProtectHome=true`, `NoNewPrivileges=true`, `PrivateTmp=true`, `ReadWritePaths=/opt/Monthly-Metrics/data /opt/Monthly-Metrics/logs`. |
| **Restart guards** | Already `Restart=on-failure`. Add `RestartSec=10`, `StartLimitBurst=5`, `StartLimitIntervalSec=300` to prevent crash-loop SD-card thrash. |
| **Pi OS hygiene** | New idempotent `scripts/harden_pi.sh`: `unattended-upgrades` (security only), SSH key-only auth, `fail2ban` for sshd. |
| **Secret rotation runbook** | `docs/runbooks/rotate-secrets.md` covering ANTHROPIC_API_KEY, SMTP_PASSWORD, ADMIN_PASSWORD, FLASK_SECRET_KEY. |
| **Disk-full guard** | `scripts/disk_check.sh` daily; alert at 85% used. |
| **Deploy script** | `scripts/deploy.sh` — single command: ssh to Pi, pull, install, migrate, restart, diagnose. |

**Artifact:** PR #3 — biggest by line count among the infra phases.

### P3 — Code refactor

Goal: clean module boundaries + strict typing + dead-code sweep, with P1 tests as the safety net.

**Module-level moves** (see "Module layout" above). Each move is one rename commit + an import-fix commit; squashed to one merge commit.

**Cross-cutting refactors:**

- **Strict typing:** type hints everywhere; `mypy --strict` clean. `# type: ignore[reason]` allowed only with explicit reason.
- **Single config object:** `config/settings.py` becomes `pydantic.BaseSettings`. Replaces ad-hoc `os.environ.get(...)` calls.
- **No global Flask app:** `create_app(config)` factory; tests get clean instances.
- **Inject the clock:** `Clock` dependency replaces direct `datetime.utcnow()`; frozen in tests.
- **Inject SMTP:** `notifier.send(transport=...)`; capture-list in tests.
- **Logging:** `print()` → `logging.getLogger(__name__)`. Root config in `src/web/__init__.py` and `main.py`. JSON in prod, plain in dev.

**Dead-code sweep (confirm before deletion):**

- `findings.md` — leftover scratchpad? Delete unless load-bearing.
- `preview.html` at repo root — pre-`templates/email.html.j2` mockup.
- `COMMIT_EDITMSG` — already handled in P0.
- Empty `__init__.py` files — keep only if needed for package layout.
- `--mock` data paths in prod modules — move under `tests/fixtures/` or behind `if settings.mock_mode`.

**Tooling enforced:**

- `ruff` (default + `I`/`B`/`S`/`UP` rule sets).
- `ruff format`.
- `mypy --strict`.
- Python 3.11 minimum.

**Artifact:** PR #4 — largest line-diff but mechanically driven by P1 tests.

### P4 — Deploy + verify

`scripts/deploy.sh` (lands in P2) is the per-phase deploy ritual:

```sh
ssh pi@raspberrypi <<'EOF'
  cd /opt/Monthly-Metrics
  git fetch origin
  git checkout main
  git reset --hard origin/main
  .venv/bin/pip install -r requirements.txt
  .venv/bin/python main.py --mode migrate
  sudo systemctl restart anchor-dashboard
  scripts/diagnose.sh
EOF
```

`scripts/smoke.sh` (also P2) runs after each deploy:

1. `curl -fsS https://anchor.joelycannoli.com/healthz` → JSON with `ok: true, db_writable: true`.
2. Login + CSRF round-trip via `curl` cookie jar.
3. POST `tests/fixtures/april_2026_sample.csv` to `/healthz/smoke` (guarded by `SMOKE_TOKEN`, only set in env for smoke runs); endpoint runs ingest+score+render to a tempdir, returns counts, doesn't touch real `metrics.db`.
4. `--dry-run` SMTP send to a sink address.

**Final verification (P4 close-out):**

- All four phase deploys executed end-to-end on the Pi.
- 24h soak: monitor `journalctl -u anchor-dashboard -u cloudflared` for errors.
- Trigger a fake heartbeat with a sample CSV; approve via dashboard; deliver to a test inbox.
- Confirm next monthly fire on June 1, 2026 via `systemd-analyze calendar "0 9 1 * *"` (or cron equivalent).

## Cross-cutting concerns

### Testing strategy

- **Unit:** `pytest`, ≥80% line coverage, 100% on `storage`/`web`/`render` (privacy/integrity).
- **Characterization:** snapshot HTML/SVG for renderers (golden files in `tests/snapshots/`).
- **Integration:** Flask test client for routes — full request/response cycle.
- **End-to-end smoke:** `scripts/smoke.sh` post-deploy.
- **No browser tests** — deferred indefinitely; out of scope.

### Observability

- Structured `logging` (JSON in prod, plain in dev) → journald.
- `journalctl -u anchor-dashboard -u cloudflared` for live tail.
- Optional later: Cloudflare Logpush. Pi-friendly, no Sentry/Grafana.
- Heartbeat + liveness alerts via SMTP to `ALERT_EMAIL`.
- `/healthz` JSON exposes disk %, DB writability, last heartbeat age, draft queue size.

### Secrets

- Master at `H:\AI\Secrets\.env.master.private` (per `H:\AI\.claude\CLAUDE.md`).
- Pi gets a derivative `.env` at `/opt/Monthly-Metrics/.env`, mode `0600`, owned by the `anchor` service user (not `pi`). `harden_pi.sh` enforces.
- Rotation runbook lands in P2.

### Error handling philosophy

- Web routes: catch `domain` errors, return user-visible message; everything else → 500 + journald log.
- Heartbeat: any non-zero subprocess exit → alert + halt; do not push partial state.
- SMTP send failures: log + leave draft as `approved` (so a manual retry is possible), do **not** silently mark sent.
- Migrations: fail fast if a migration errors; service won't start until DB matches schema.

### Out-of-scope (explicit non-goals)

- Cloudflare Access SSO (mentioned in PUBLISH.md as future work).
- Multi-user auth.
- Replacing SQLite.
- Containerization.
- Self-hosted GitHub Actions runner on the Pi.
- Security review beyond what falls out of code-quality + reliability work (response headers do get added in P3 because of the `web/security.py` split, but no formal pen-test pass).

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| Branch reset destroys ancestor history someone relies on | `git bundle` + `archive/<branch>` tags before any deletion. Reversible. |
| Refactor in P3 breaks behavior tests didn't cover | P1's coverage gate (≥80%, 100% on critical surfaces) + characterization snapshots. If a regression slips through, `git revert` of the merge is one command. |
| Migration in P2/P3 strands the Pi | Forward-only migrations, no destructive changes. Backup written immediately before each migration run. |
| `harden_pi.sh` locks me out (e.g., disabling password auth before keys work) | Script is idempotent and dry-run-able (`scripts/harden_pi.sh --dry-run`). Verifies key auth works *before* disabling password auth. Run from a screen/tmux session in case of disconnect. |
| Cron/timer drifts after refactor | P4 verification step explicitly checks `systemd-analyze calendar`. |
| SMTP credentials rotated without app restart leaves heartbeat broken | Rotation runbook documents the restart step. `/healthz` would expose this via the `last_heartbeat_age_hours` field after the next monthly attempt. |

## Sequencing & wall-clock estimate

(Indicative, assuming you and I are pairing live; sub-agent help can compress.)

1. **P0** — half-day. Mostly config + git surgery.
2. **P1** — 1–2 days. Bulk of the test backfill is in `dashboard.py` and the renderers.
3. **P2** — 1–2 days. Lots of small files, low individual risk.
4. **P3** — 1 day if P1 is solid; 2–3 days if any seams need to grow before the moves are safe.
5. **P4** — half-day for the cumulative deploy + 24h soak (which is wall-clock, not work).

## Open questions to resolve before P0 starts

- Confirm `pi@raspberrypi` is reachable from this workstation (currently denied — needs key auth set up).
- Confirm whether `findings.md` and `preview.html` are safe to delete (P3 dead-code sweep).
- Confirm `ALERT_EMAIL` for operational alerts (default `EMAIL_FROM` is fine if not specified).
