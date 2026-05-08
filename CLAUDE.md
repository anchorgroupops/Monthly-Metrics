# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Anchor Group Monthly Metrics — Zillow Preferred performance reporting for The Anchor Group. Each month it researches the current Zillow Preferred KPIs, ingests the prior month's per-agent data (admin CSV/JSON upload or live Follow Up Boss pull), persists history in SQLite for rolling trends, and queues per-agent HTML emails plus a Reveal.js team deck. A Flask + HTMX + Tailwind admin dashboard (mobile-first, single-admin password) runs the **draft → approve → send** workflow. Designed to live on a Raspberry Pi behind a Cloudflare Tunnel and trigger on a `systemd` timer at 09:00 on the 1st of each month.

## Commands

```bash
# Install (production: scripts/install.sh handles venv, .env scaffolding, pytest)
pip install -r requirements.txt
pip install -r requirements-dev.txt    # pytest, pytest-mock, responses

# DB migrations (forward-only, NNN_*.sql under src/migrations/)
python main.py --mode migrate

# Refresh KPIs + thresholds via Claude web_search (writes config/thresholds.json)
python main.py --mode research

# Ingest a month of agent data
python main.py --mode upload tests/fixtures/april_2026_sample.csv   # CSV or JSON
python main.py --mode pull                                          # live FUB pull → SQLite

# Build per-agent + deck preview to output/review/ (gitignored)
python main.py --mode review
python main.py --mode review --mock                # synthetic data, no API key
python main.py --agent "Jane Smith" --mock         # single-agent shortcut → review

# Queue draft emails for admin approval (does NOT send)
python main.py --mode draft

# Start the Flask admin UI on http://127.0.0.1:5050 (login: ADMIN_PASSWORD env)
python main.py --mode dashboard

# Send only drafts in status='approved' from the queue
python main.py --mode send
python main.py --mode send --dry-run               # print recipients, don't deliver

# Tests, lint, types
pytest                                             # uses pyproject.toml config
ruff check src config main.py
ruff format --check src config main.py
mypy src/                                          # non-blocking in CI

# Production
gunicorn -b 127.0.0.1:5050 wsgi:application        # what scripts/serve.sh runs
scripts/heartbeat.sh                               # what anchor-monthly.timer triggers
```

`--verbose` enables debug logging. Source resolution for `review`/`draft`: `--mock` → `--period YYYY-MM` (SQLite) → `--source fub` (live API) → auto (most-recent SQLite period, falling back to FUB if empty). `OVERRIDE_REPORT_MONTH` in `config/settings.py` reruns a specific period.

## Required environment variables

`.env` in the repo root is auto-loaded by `scripts/heartbeat.sh` and `scripts/serve.sh`. `scripts/install.sh` scaffolds `.env` from `.env.example` and auto-generates `ADMIN_PASSWORD` and `FLASK_SECRET_KEY` on first run.

- `ANTHROPIC_API_KEY` — only for `--mode research`.
- `FUB_API_KEY` — Follow Up Boss API key (HTTP Basic, key as username). Only for `--mode pull` or `--source fub`. Not needed for `--mode upload` or `--mock`.
- `ADMIN_PASSWORD` — dashboard login (defaults to `"anchor"` if unset — change in production).
- `FLASK_SECRET_KEY` — Flask session signing key.
- `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `EMAIL_FROM` — only for actual delivery (defaults to Gmail SMTP on 587 with STARTTLS).
- `ADMIN_EMAIL` — where heartbeat failure alerts go (falls back to `EMAIL_FROM`).
- `DEPLOYMENT_MODE=production` — enables `Secure`/`HttpOnly`/`SameSite=Lax` cookies and `ProxyFix` for Cloudflare Tunnel headers.

`output/`, `logs/`, `data/*.db*`, `.env`, and `config/secrets.py` are gitignored — never commit generated reports, the SQLite history, or credentials.

## Architecture

The pipeline is no longer a single linear path: data ingest, scoring, draft generation, and delivery are decoupled via SQLite, and the human-in-the-loop approval queue sits between scoring and SMTP.

```
research  → config/thresholds.json (KPIs + targets)

upload (CSV/JSON)  ─┐
pull (FUB API)     ─┤→  storage.save_period  →  agent_periods + runs (SQLite)
                                                       ↓
                                       metrics.score_all_agents
                                                       ↓
                          ┌────────────────────────────┴──────────────────────────┐
                          ↓                                                       ↓
              review_mode (output/review/)                       email_builder → storage.queue_draft
                                                                                       ↓
                                                              dashboard.py: review/approve/reject
                                                                                       ↓
                                                                             send → SMTP + mark_sent
```

The monthly cron pipeline (`scripts/heartbeat.sh`) runs `pull → research → draft`. Sending is **always** human-gated through the dashboard; neither the cron job nor `--mode send` will deliver mail unless the admin has approved drafts in the UI first.

**`main.py`** is the only CLI entry. Modes: `research`, `pull`, `upload`, `review`, `draft`, `dashboard`, `send`, `migrate`. `--agent NAME` with no `--mode` defaults to review. Source precedence in `_load_source_agents`: `--mock` → `--period` (SQLite) → `--source fub` → auto (latest SQLite period, falling back to FUB).

**`wsgi.py`** is the gunicorn entry — `application = create_app()`. `scripts/serve.sh` runs `gunicorn wsgi:application` and is what `anchor-dashboard.service` exec's.

**`config/settings.py`** — single source of truth for paths (`BASE_DIR`, `TEMPLATES_DIR`, `OUTPUT_DIR`, `THRESHOLDS_FILE`), FUB/Claude/SMTP config, the `AGENTS` roster (intentionally empty in the repo — populated only via `.env`/private deployment), `OVERRIDE_REPORT_MONTH`, and the `BRAND` dict (Anchor Team palette: Clear Water teal `#167272`, Pearl Aqua `#5DC8BE`, Sandy Shore cream `#F5EDE0`; Collier headings, Dax Pro body). Templates and the dashboard inject `BRAND` so theme changes happen in one place.

**`config/thresholds.json`** is auto-managed by `threshold_researcher.py`. Researched fields (`target`, `yellow_floor`, `unit`, `last_updated`, `source`, `program_year`) are overwritten each run; static fields (`label`, `weight`, `gauge_size`, `description`, `direction`) are preserved by an in-place merge. **Do not hand-edit `target`/`yellow_floor`** — they will be overwritten next research run. `metrics.score_metric` returns `no_data` when target is missing.

**`src/threshold_researcher.py`** calls the Claude API (`RESEARCH_MODEL = "claude-sonnet-4-6"`) with the `web_search_20250305` tool to look up current Zillow Preferred program benchmarks, strips markdown fences, and parses JSON. Keep the response format aligned with the keys consumed by `update_thresholds_file`.

**`src/fub_client.py`** uses HTTP Basic auth with the API key as username. Tries `/reporting/zillow-preferred` first, falls back to `/reporting/agent` on 404. Best-guess endpoint paths and field names in `_normalize` may need adjustment when wired to a real FUB account. Per-agent fetch failures yield a `_null_record` so the report still generates with `no_data` rather than aborting. Retries use exponential backoff and honor `Retry-After` on 429.

**`src/csv_ingest.py`** parses admin uploads (`.csv` via `DictReader` with utf-8-sig BOM tolerance, `.json` accepting either a list at the top level or `{"agents": [...]}`/`{"rows": [...]}` envelopes). The required fixed columns are `agent_id, name, email, period`; the rest are validated against `metric_keys(load_thresholds())` so the upload schema follows whatever Zillow program is current.

**`src/storage.py`** owns SQLite. Tables (`agent_periods` (long-format, one row per metric), `agent_meta`, `runs`, `drafts`) are defined inline as `SCHEMA` and as the seed migration `src/migrations/001_initial.sql`. `connect()` runs `apply_pending_migrations()` on every open, so any caller that opens the DB will auto-migrate. Public API: `save_period`, `start_run`/`finish_run`/`get_active_run`/`latest_run` (so the dashboard can show "pull in progress"), `load_period`, `load_history`, `team_history`, `list_periods`, plus the draft queue: `queue_draft`, `list_drafts`, `get_draft`, `approve_draft`, `reject_draft`, `mark_sent`, `approve_all`. `normalize_period` accepts `"April 2026"` / `"2026-04"` / `"2026-04-15"` and emits canonical `"YYYY-MM"`.

**`src/migrations/_runner.py`** is a forward-only migration runner: each `NNN_*.sql` in `src/migrations/` runs once, recorded in the `schema_migrations` tracking table. Sets `journal_mode=WAL` and `synchronous=NORMAL`. Add a new migration by creating the next-numbered file — never edit a migration that has shipped.

**`src/metrics.py`** is the scoring engine. `METRIC_KEYS` is **not** hard-coded any more — `metric_keys(thresholds)` returns the live keyset sorted hero-first then by weight desc. `score_metric` honors `direction: "higher_is_better" | "lower_is_better"` (e.g. response-time metrics where fewer seconds = better). Per-metric status: green when at/above target (or at/below for lower-is-better), yellow when at/above the yellow floor (or at/below for lower-is-better), else red. `overall_status` is a weight-normalized average of `pct_of_target * weight`: `≥1.0 → "Preferred"`, `≥0.85 → "At Risk"`, else `"Needs Improvement"`; `"No Data"` only when every metric is missing. `operational_readiness` returns the same weighted score scaled to 0–100 (capped at 125), used by the dashboard leaderboard. `team_summary` ranks agents for the deck. `rolling_trend(agent_id, metric_key, window_months)` pulls history from `storage.load_history` and returns `{values, delta_pct, sparkline}` (inline SVG polyline).

**`src/gauges.py`** emits self-contained inline SVG semicircle arc gauges (no external deps, email-client safe). Two profiles in `SIZES`: `hero` (200×120) for the hero metric, `secondary` (130×80) for the rest — selected from each metric's `gauge_size`. Fill arc clamps fraction to 1.25 to permit slight overshoot. Status colors come from `BRAND["color_green/yellow/red"]`; `no_data` falls back to gray.

**`src/email_builder.py` / `src/deck_builder.py`** are thin Jinja2 renderers around `templates/email.html.j2` (table-based for Outlook/Gmail compatibility, autoescape on) and `templates/deck.html.j2` (Reveal.js 5.1.0 from CDN, autoescape **off** because the template embeds gauge SVG strings). Both pass the same `scored_agent` dict and pre-rendered gauge SVGs into templates — never render gauges inside the template.

**`src/review_mode.py`** writes `{slug}.html` per agent, `deck.html`, and an `index.html` overview to `output/review/`. The index is built inline as an f-string (not Jinja) and pulls colors directly from `BRAND`.

**`src/dashboard.py`** is the Flask admin app, created via `create_app()`. CSRF via Flask-WTF; brute-force protection via Flask-Limiter (5 attempts / 15 min / IP on `/login`); `ProxyFix` + secure cookies when `DEPLOYMENT_MODE=production`. Routes: `/` `/login` `/logout` `/healthz` (JSON: `ok`, `db_writable`, `last_heartbeat_age_hours`, `draft_queue_size`, `disk_used_pct`) `/home` (leaderboard + sparklines + manual-pull button) `/upload` `/pull-now` (POST starts a background-thread pipeline that calls `pull → research → draft` end-to-end) `/pull-status` (HTMX poll) `/review/<period>` `/draft/<id>` `/draft/<id>/approve` `/draft/<id>/reject` `/review/<period>/approve_all` `/send`. Manual-pull worker calls `notifier.notify_admin_failure` on any hard error. Uses `templates/admin/*.html` (not Jinja-2-suffix) with Tailwind + HTMX via CDN — no JS build step.

**`src/notifier.py`** sends plain-text admin alerts via the same SMTP creds. `notify_admin_failure(subject, body)` soft-fails when SMTP is unconfigured, and dedupes within `DEDUP_WINDOW_MINUTES = 30` via the `data/.last-alert` mtime marker. `scripts/heartbeat.sh` calls it on `pull` or `draft` failure (research failure is non-fatal).

**`preview.html`** at the repo root is a standalone static brand mockup (NeuChart-inspired) for design review — not part of the runtime pipeline.

## Production deployment

The system is designed for a Raspberry Pi at `/opt/Monthly-Metrics`:

- `scripts/install.sh` — idempotent bootstrap: apt installs, `.venv`, `requirements.txt`, scaffolds `.env` (auto-generates `ADMIN_PASSWORD` + `FLASK_SECRET_KEY`), runs `pytest`.
- `scripts/install_cron.sh` / `scripts/install_monthly_timer.sh` — installs the monthly trigger.
- `scripts/install_dashboard_service.sh` — installs `anchor-dashboard.service` (gunicorn).
- `scripts/install_tunnel.sh` — installs `cloudflared` and CNAMEs `anchor.joelycannoli.com → 127.0.0.1:5050`.
- `scripts/diagnose.sh` — green/red checklist for the full deploy chain (run this first when something is broken).
- `scripts/healthz_check.sh`, `scripts/disk_check.sh`, `scripts/backup_db.sh`, `scripts/harden_pi.sh`, `scripts/smoke.sh`, `scripts/deploy.sh` — operational helpers.

systemd units in `systemd/`:
- `anchor-monthly.{service,timer}` — runs `scripts/heartbeat.sh` at `*-*-01 09:00:00` with `Persistent=true`.
- `anchor-dashboard.service` — gunicorn under `Type=simple`, `Restart=on-failure`, `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome=read-only`, `ReadWritePaths=data logs`.
- `anchor-backup.{service,timer}` — daily DB backup at 02:00.

Operational runbooks: `HEARTBEAT.md` (scheduling), `PUBLISH.md` (Cloudflare Tunnel), `architecture/SOP.md`, `docs/runbooks/`, `etc/logrotate/`.

## Testing & CI

`pytest` is configured in `pyproject.toml` (testpaths=`tests`, strict markers/config, coverage `fail_under = 80`, source = `src config main`). `tests/conftest.py` provides `isolated_db` (tmp SQLite via `monkeypatch`) and `isolated_thresholds` (tmp `thresholds.json` copy) — use these to keep tests off the real `data/metrics.db`. Coverage spans ingest (`test_csv_ingest`), scoring (`test_metrics`), storage + drafts (`test_storage`), Flask routes (`test_dashboard`), the monthly pipeline (`test_pull`, `test_main_modes`), migrations (`test_migrations`), the notifier (`test_notifier`), the FUB client with `responses` (`test_fub_client`), the threshold researcher (`test_threshold_researcher`), and **`test_privacy.py`** which asserts no cross-agent data leaks into rendered emails — keep this passing whenever you touch templates or the email pipeline.

`.github/workflows/ci.yml` runs ruff lint, ruff format check, mypy (non-blocking), and `pytest --cov` on Python 3.11 + 3.12 against PRs and pushes to `main`.

`pyproject.toml` ruff config: `line-length = 100`, target `py311`, lints `E F W I B S UP`, `tests/**` ignores `S101 S105 S106`, `main.py` ignores `S105`. Format style `quote-style = "double"`.

## Conventions

- Metric values use natural units throughout: rates as decimals 0.0–1.0 (not percentages), seconds as raw seconds, scores as raw, counts as integers. Display formatting happens only in `gauges._format_value`, the `metric_value` Jinja filter on the dashboard, and templates.
- The metric set is **dynamic**. To add a metric: add a stanza to `config/thresholds.json` (with `weight`, `gauge_size`, `label`, `unit`, `description`, optional `direction: "lower_is_better"`), extend the research prompt in `threshold_researcher.py`, add normalization in `fub_client._normalize` if pulling live, and the rest of the pipeline (scoring, gauges, email, deck, dashboard, CSV validation) picks it up automatically. There is no `METRIC_KEYS` constant to update.
- Preserve the `metrics` (dict) and `metrics_list` (ordered, hero-first) parallel structures emitted by `score_agent` — templates iterate the list, code paths key into the dict.
- `agent_periods` is **long format** (one row per agent/period/metric_key). `save_period` upserts on the composite primary key. `load_period` re-pivots back to wide-format dicts shaped like `csv_ingest`/`fub_client` output, ready for `score_all_agents`.
- Periods normalize through `storage.normalize_period` (`"April 2026"` / `"2026-04"` / `"2026-04-15"` → `"2026-04"`). Display via `period_label` (`"April 2026"`).
- New schema changes are forward-only migrations — add `src/migrations/00N_<description>.sql`. Don't edit a shipped migration; don't drop tables outside a migration.
- Sending is always human-gated through `drafts.status='approved'`. Don't add a code path that calls SMTP from `--mode draft` or the heartbeat — `scripts/heartbeat.sh` deliberately stops at `draft`.
- Privacy: per-agent emails are rendered with that agent's record only. The Jinja context for `email.html.j2` contains a single `agent` object, never the team list. `tests/test_privacy.py` enforces this.
- `data/.last-alert` (notifier dedup), `data/metrics.db*`, and anything under `output/`/`logs/` are operational state — never commit them. `AGENTS` in `config/settings.py` is intentionally empty in source; populate via private deployment.
- Don't hand-edit `target`/`yellow_floor` in `thresholds.json` — they are overwritten by `--mode research`.
