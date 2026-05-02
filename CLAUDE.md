# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Anchor Group Monthly Metrics — a Python CLI that pulls Zillow Preferred Performance Report data from Follow Up Boss (FUB), scores each agent against current Zillow Preferred thresholds, and generates per-agent HTML emails plus a Reveal.js team meeting deck. Designed to be triggered monthly (e.g. by n8n) on the prior calendar month.

## Commands

```bash
# Install
pip install -r requirements.txt

# Refresh thresholds via Claude web_search (writes config/thresholds.json)
python main.py --mode research

# Generate all emails + deck to output/review/ (gitignored) for local QA
python main.py --mode review              # live FUB data
python main.py --mode review --mock       # synthetic data, no API keys needed

# Single agent preview (defaults to review mode)
python main.py --agent "Jane Smith" --mock

# Send for real via SMTP (called by n8n on schedule)
python main.py --mode send
python main.py --mode send --dry-run      # print recipients without delivering

# Preview generated review files in browser
python -m http.server 8080 --directory output/review
```

Run the test suite with `pytest` (configured via `pyproject.toml`; runs with branch coverage and fails under 90%). Install dev deps with `pip install -r requirements-dev.txt`. There is no lint config or build step. `--verbose` enables debug logging. `OVERRIDE_REPORT_MONTH` in `config/settings.py` (format `"YYYY-MM"`) reruns a specific period; otherwise the prior calendar month is auto-detected.

## Required environment variables

- `FUB_API_KEY` — Follow Up Boss API key (HTTP Basic auth, key as username). Not needed with `--mock`.
- `ANTHROPIC_API_KEY` — only for `--mode research`.
- `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `EMAIL_FROM` — only for `--mode send` without `--dry-run`. Defaults to Gmail SMTP on 587 with STARTTLS.

`output/`, `.env`, and `config/secrets.py` are gitignored — never commit generated reports or credentials.

## Architecture

The pipeline is strictly linear and shared across `review` and `send` modes:

```
threshold_researcher → config/thresholds.json
                                ↓
fub_client.fetch_all_agents → metrics.score_all_agents → gauges.build_all_gauges
                                                                ↓
                                                    email_builder + deck_builder
                                                                ↓
                                              review_mode (write files) | main._send_emails (SMTP)
```

**`config/settings.py`** is the single source of truth for paths, FUB/Claude/SMTP config, the agent roster (`AGENTS`), reporting period override, and the `BRAND` dict (Anchor Team palette: Clear Water teal `#167272`, Pearl Aqua `#5DC8BE`, Sandy Shore cream `#F5EDE0`; Collier headings, Dax Pro body). Templates and the review index page all read from `BRAND`, so theme changes happen in one place.

**`config/thresholds.json`** is auto-managed by `threshold_researcher.py`. The researched fields (`target`, `yellow_floor`, `unit`, `last_updated`, `source`, `program_year`) are overwritten each run; the static fields (`label`, `weight`, `gauge_size`, `description`) are preserved by an in-place merge. **Do not hand-edit `target`/`yellow_floor`** — they will be overwritten next research run. Targets default to `null` until the first research call; `metrics.score_metric` returns `no_data` status when target is missing.

**`src/threshold_researcher.py`** calls the Claude API (`RESEARCH_MODEL = "claude-sonnet-4-6"`) with the `web_search_20250305` tool to look up current Zillow Preferred program benchmarks, then strips markdown fences and parses the JSON response. Keep the response format in `RESEARCH_PROMPT` aligned with the keys consumed by `update_thresholds_file`.

**`src/fub_client.py`** uses HTTP Basic auth with the API key as username (no password). It tries `/reporting/zillow-preferred` first and falls back to `/reporting/agent` on 404 — these endpoint paths and the field names in `_normalize` (e.g. `predictedConversionRate` vs `pCVR`) are best-guess and likely need adjustment when wired to a real FUB account. On per-agent fetch failure the client returns a `_null_record` so the report still generates with `no_data` statuses rather than aborting the whole run. Retries use exponential backoff and honor `Retry-After` on 429.

**`src/metrics.py`** is the scoring engine. The four canonical metric keys (`METRIC_KEYS`) are `pCVR`, `pickup_rate`, `csat`, `zhl_transfers`; pCVR is the hero. Per-metric status: green when `value/target ≥ 1.0`, yellow when `value ≥ yellow_floor`, else red. Overall status is a weight-normalized average of `pct_of_target * weight` across scoreable metrics: `≥1.0 → "Preferred"`, `≥0.85 → "At Risk"`, else `"Needs Improvement"`; `"No Data"` only when every metric is missing. `team_summary` ranks agents by the same weighted score for the deck's leaderboard slide.

**`src/gauges.py`** emits self-contained inline SVG semicircle arc gauges (no external deps, email-client safe). Two size profiles in `SIZES`: `hero` (200×120) for pCVR, `secondary` (130×80) for the rest — selected from each metric's `gauge_size` field in thresholds. The fill arc clamps fraction to 1.25 to permit slight overshoot rendering. Status colors come from `BRAND["color_green/yellow/red"]`; `no_data` falls back to gray.

**`src/email_builder.py` / `src/deck_builder.py`** are thin Jinja2 renderers around `templates/email.html.j2` (table-based layout for Outlook/Gmail compatibility, autoescape on) and `templates/deck.html.j2` (Reveal.js 5.1.0 from CDN, autoescape **off** because the template embeds gauge SVG strings). Both pass the same `scored_agent` dict and pre-rendered gauge SVGs into templates — never render gauges inside the template.

**`src/review_mode.py`** writes `{slug}.html` per agent, `deck.html`, and an `index.html` overview to `output/review/`. The index is built inline as an f-string (not Jinja) and pulls colors directly from `BRAND`.

**`main.py`** is the only entry point. The single-agent shortcut (`--agent NAME` with no `--mode`) defaults to review mode. Agent name matching is partial and case-insensitive via `_filter_agent`. SMTP delivery uses STARTTLS and one connection for all recipients.

**`preview.html`** at the repo root is a standalone static brand mockup (NeuChart-inspired) for design review — it is not part of the runtime pipeline and does not consume real data.

## Conventions

- Metric values use natural units throughout: rates as decimals 0.0–1.0 (not percentages), CSAT as raw score, ZHL as integer count. Display formatting happens only in `gauges._format_value` and templates.
- Preserve the `metrics` (dict) and `metrics_list` (ordered, hero-first) parallel structures emitted by `score_agent` — templates iterate the list, code paths key into the dict.
- When adding a new metric: add the key to `METRIC_KEYS`, add a stanza to `config/thresholds.json` (with `weight`, `gauge_size`, `label`, `unit`, `description`), extend the research prompt's expected JSON, add normalization in `fub_client._normalize`, and the rest of the pipeline picks it up automatically.
- Never commit anything under `output/`, real values in `AGENTS`, or populated `target`/`yellow_floor` numbers in `thresholds.json` (those should be regenerated by `--mode research`).
