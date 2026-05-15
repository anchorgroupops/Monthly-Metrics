#!/usr/bin/env python3
"""
Anchor Group Monthly Metrics — CLI Entry Point

Usage:
  python main.py --mode research                       # Refresh KPIs + thresholds
  python main.py --mode pull                           # Fetch FUB metrics → SQLite
  python main.py --mode daily                          # Daily MTD activity snapshot → SQLite
  python main.py --mode daily --mock                   # Same, with synthetic data
  python main.py --mode upload <file.csv|file.json>    # Ingest admin upload
  python main.py --mode review                         # Generate output locally
  python main.py --mode review --mock                  # Review with mock data
  python main.py --mode draft                          # Queue drafts for approval
  python main.py --mode dashboard                      # Start Flask admin UI
  python main.py --mode send                           # Send approved drafts
  python main.py --agent "Jane Smith"                  # Preview a single agent
"""

import argparse
import logging
import smtplib
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from config.settings import (
    EMAIL_FROM_ADDRESS,
    EMAIL_FROM_NAME,
    EMAIL_SUBJECT_TEMPLATE,
    SMTP_HOST,
    SMTP_PASSWORD,
    SMTP_PORT,
    SMTP_USER,
)


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )


# -- Mode: research ------------------------------------------------------------


def cmd_research(args) -> int:
    from src.threshold_researcher import run_research

    print("\n-- Researching Zillow Preferred KPIs… --")
    run_research()
    return 0


# -- Mode: pull ----------------------------------------------------------------


def cmd_pull(args) -> int:
    """
    Fetch the prior-month metrics from FUB and persist to SQLite. Idempotent:
    re-running for the same period upserts on (agent_id, period, metric_key).
    Used by the cron pipeline and the dashboard's manual-pull button.
    """
    from config.settings import FUB_API_KEY
    from src.fub_client import fetch_all_agents
    from src.storage import finish_run, save_period, start_run

    print("\n-- Pull Mode --------------------------------------------------------")

    if not FUB_API_KEY:
        print(
            "  ERROR: FUB_API_KEY environment variable not set.\n"
            "  Set it in /opt/Monthly-Metrics/.env (production) or your shell."
        )
        return 1

    run_id = start_run(source="fub")
    try:
        agents = fetch_all_agents()
        if not agents:
            finish_run(run_id, "ok", "no agents returned")
            print("  FUB returned 0 agents — nothing to save.")
            return 0
        save_period(agents, source="fub", run_id=run_id)

        errored = sum(1 for a in agents if a.get("_error"))
        all_nulls = all(
            a.get("pCVR") is None
            and a.get("pickup_rate") is None
            and a.get("csat") is None
            and a.get("zhl_transfers") is None
            for a in agents
        )

        if errored == len(agents) or all_nulls:
            msg = (
                f"Pulled {len(agents)} agents from FUB but every record was empty or errored "
                f"({errored}/{len(agents)} explicit fetch failures). "
                "The Zillow Preferred Performance Report is UI-only in FUB — "
                "admin must upload the monthly CSV via the dashboard."
            )
            finish_run(run_id, "error", msg)
            print(f"  ERROR: {msg}")
            return 1

        finish_run(run_id, "ok", f"{len(agents)} agents, {errored} errored")
        print(f"  Pulled {len(agents)} agent record(s) from FUB ({errored} with errors).")
        print("  Next: python main.py --mode draft\n")
        return 0
    except Exception as exc:
        finish_run(run_id, "error", str(exc))
        log = logging.getLogger(__name__)
        log.exception("FUB pull failed")
        print(f"  ERROR: {exc}")
        return 1


# ── Mode: daily ───────────────────────────────────────────────────────────────


def cmd_daily(args) -> int:
    """
    Daily operational-activity pull. Hits /v1/people for each agent, computes
    MTD metrics from raw lead activity, and upserts a snapshot row per agent
    keyed on today's date. Idempotent: re-running on the same day overwrites.

    Use --mock to populate the DB with synthetic data for local dashboard work.
    """
    from src.fub_daily_metrics import mock_daily_results, pull_daily_metrics, save_results

    print("\n── Daily Mode ───────────────────────────────────────────────────────")

    if args.mock:
        print("  Source: mock data")
        results = mock_daily_results()
    else:
        from config.settings import FUB_API_KEY

        if not FUB_API_KEY:
            print(
                "  ERROR: FUB_API_KEY environment variable not set.\n"
                "  Set it in /opt/Monthly-Metrics/.env (production) or your shell.\n"
                "  Or use --mock for local testing without a live key."
            )
            return 1
        print("  Source: live FUB API (/v1/people, MTD)")
        try:
            results = pull_daily_metrics()
        except Exception as exc:
            log = logging.getLogger(__name__)
            log.exception("Daily pull failed")
            print(f"  ERROR: {exc}")
            return 1

    if not results:
        print("  No agents to process.")
        return 0

    saved = save_results(results)
    errored = sum(1 for r in results if r.get("_error"))
    print(f"  Saved daily snapshot for {saved} agent(s) ({errored} with errors).")
    if errored:
        for r in results:
            if r.get("_error"):
                print(f"    ! {r['name']}: {r['_error'][:120]}")
    print("  View at: python main.py --mode dashboard  →  /daily\n")
    return 0


# ── Mode: upload ──────────────────────────────────────────────────────────────


def cmd_upload(args) -> int:
    from src.csv_ingest import parse_file
    from src.storage import save_period

    if not args.file:
        print("  ERROR: --mode upload requires a file path. Example:")
        print("    python main.py --mode upload data/april_2026.csv")
        return 1

    print("\n-- Upload Mode ------------------------------------------------------")
    print(f"  File: {args.file}")

    try:
        agents = parse_file(args.file)
    except (FileNotFoundError, ValueError) as e:
        print(f"  ERROR: {e}")
        return 1

    suffix = args.file.rsplit(".", 1)[-1].lower()
    source = "csv" if suffix == "csv" else "json"
    run_id = save_period(agents, source=source, file_path=args.file)

    print(f"  Ingested {len(agents)} agent record(s) — run #{run_id}.")
    print("  Next: python main.py --mode draft   to queue draft emails for approval.\n")
    return 0


# -- Mode: review --------------------------------------------------------------


def cmd_review(args) -> int:
    from src.metrics import score_all_agents
    from src.review_mode import run_review

    print("\n-- Review Mode ------------------------------------------------------")
    agents_data = _load_source_agents(args)
    if not agents_data:
        return 1

    print(f"  Scoring {len(agents_data)} agent(s)…")
    scored = score_all_agents(agents_data)

    if args.agent:
        scored = _filter_agent(scored, args.agent)
        if not scored:
            return 1

    print("  Generating review output…")
    run_review(scored)
    return 0


# -- Mode: draft ---------------------------------------------------------------


def cmd_draft(args) -> int:
    """Queue draft emails for admin approval. Does NOT send."""
    from src.email_builder import build_email
    from src.metrics import score_all_agents
    from src.storage import queue_draft

    print("\n-- Draft Mode -------------------------------------------------------")
    agents_data = _load_source_agents(args)
    if not agents_data:
        return 1

    scored = score_all_agents(agents_data)
    if args.agent:
        scored = _filter_agent(scored, args.agent)
        if not scored:
            return 1

    queued = 0
    for agent in scored:
        html = build_email(agent)
        queue_draft(agent["agent_id"], agent["period"], html)
        queued += 1
        print(f"  Queued draft for {agent['name']}")

    print(f"\n  {queued} draft(s) queued. Review at:")
    print("    python main.py --mode dashboard\n")
    return 0


# -- Mode: dashboard -----------------------------------------------------------


def cmd_dashboard(args) -> int:
    from src.dashboard import create_app

    print("\n-- Dashboard --------------------------------------------------------")
    print("  Starting Flask on http://127.0.0.1:5050")
    print("  Set ADMIN_PASSWORD env var to enable login (default: 'anchor').\n")
    app = create_app()
    app.run(host="127.0.0.1", port=5050, debug=args.verbose)
    return 0


# -- Mode: send ----------------------------------------------------------------


def cmd_send(args) -> int:
    """Send only drafts in the approval queue with status='approved'."""
    from src.storage import get_draft, list_drafts, mark_sent

    print("\n-- Send Mode --------------------------------------------------------")
    approved = list_drafts(status="approved")
    if not approved:
        print("  No approved drafts in queue. Approve some via the dashboard first.")
        return 1

    if args.dry_run:
        print(f"  DRY RUN — would send {len(approved)} email(s):")
        for d in approved:
            print(f"    → {d['name']} <{d['email']}>")
        return 0

    if not SMTP_USER or not SMTP_PASSWORD:
        print(
            "  ERROR: SMTP credentials not set. Set SMTP_USER and SMTP_PASSWORD env vars.\n"
            "  Use --dry-run to skip sending."
        )
        return 1

    print(f"  Connecting to {SMTP_HOST}:{SMTP_PORT}…")
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)

            from src.storage import period_label

            for d in approved:
                full = get_draft(d["id"])
                msg = MIMEMultipart("alternative")
                msg["Subject"] = EMAIL_SUBJECT_TEMPLATE.format(month=period_label(full["period"]))
                msg["From"] = f"{EMAIL_FROM_NAME} <{EMAIL_FROM_ADDRESS}>"
                msg["To"] = full["email"]
                msg.attach(MIMEText(full["html"], "html", "utf-8"))

                server.sendmail(EMAIL_FROM_ADDRESS, full["email"], msg.as_string())
                mark_sent(full["id"])
                print(f"  ✓ Sent to {full['name']} <{full['email']}>")

    except smtplib.SMTPException as e:
        print(f"  SMTP error: {e}")
        return 1

    print(f"\n  {len(approved)} email(s) sent successfully.\n")
    return 0


# -- Mode: single agent shortcut -----------------------------------------------


def cmd_agent(args) -> int:
    return cmd_review(args)


def cmd_migrate(args) -> int:
    """Run pending schema migrations against data/metrics.db."""
    from src.migrations._runner import apply_pending_migrations
    from src.storage import DB_PATH

    print("\n-- Migrate ----------------------------------------------------------")
    print(f"  DB: {DB_PATH}")
    applied = apply_pending_migrations(DB_PATH)
    if applied:
        print(f"  Applied {len(applied)} migration(s):")
        for name in applied:
            print(f"    - {name}")
    else:
        print("  No pending migrations.")
    return 0


# -- Helpers -------------------------------------------------------------------


def _load_source_agents(args) -> list[dict]:
    """
    Resolve the data source for review/draft modes.

    Precedence:
      1. --mock                → mock_agents()
      2. --period <YYYY-MM>    → SQLite (admin uploaded earlier)
      3. --source fub          → live FUB pull (preserved behavior)
      4. default (no flags)    → most-recent SQLite period if any, else FUB
    """
    from src.fub_client import fetch_all_agents, mock_agents
    from src.storage import list_periods, load_period

    if args.mock:
        print("  Source: mock data")
        return mock_agents()

    if args.period:
        print(f"  Source: SQLite ({args.period})")
        agents = load_period(args.period)
        if not agents:
            print(f"  No data found for period {args.period}. Did you run --mode upload?")
        return agents

    if args.source == "fub":
        _check_fub_key()
        print("  Source: live FUB API")
        return fetch_all_agents()

    # Auto: prefer most recent SQLite period, fall back to FUB
    periods = list_periods()
    if periods:
        latest = periods[0]
        print(f"  Source: SQLite ({latest})  [auto — pass --source fub to override]")
        return load_period(latest)

    _check_fub_key()
    print("  Source: live FUB API  [no SQLite history yet]")
    return fetch_all_agents()


def _check_fub_key() -> None:
    from config.settings import FUB_API_KEY

    if not FUB_API_KEY:
        print(
            "  ERROR: FUB_API_KEY environment variable not set.\n"
            "  Set it with: export FUB_API_KEY=your_key\n"
            "  Or use --mock for local testing without a live key.\n"
            "  Or use --mode upload to load CSV/JSON instead."
        )
        sys.exit(1)


def _filter_agent(scored: list[dict], name: str) -> list[dict]:
    normalized = name.lower().strip()
    matched = [a for a in scored if normalized in a["name"].lower()]
    if not matched:
        print(f"  No agent found matching '{name}'. Available:")
        for a in scored:
            print(f"    - {a['name']}")
        return []
    return matched


# -- Entry point ---------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Anchor Group Monthly Metrics — report generator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--mode",
        choices=[
            "research",
            "pull",
            "daily",
            "upload",
            "review",
            "draft",
            "dashboard",
            "send",
            "migrate",
        ],
        help="Execution mode",
    )
    parser.add_argument(
        "--file",
        metavar="PATH",
        help="(upload mode) Path to CSV or JSON file",
    )
    parser.add_argument(
        "--source",
        choices=["fub", "sqlite"],
        help="Override default data source for review/draft modes",
    )
    parser.add_argument(
        "--period",
        metavar="YYYY-MM",
        help="Load a specific period from SQLite (e.g. 2026-04)",
    )
    parser.add_argument(
        "--agent",
        metavar="NAME",
        help="Process a single agent by name (partial match ok)",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Use mock data instead of live FUB API or SQLite",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="(send mode) Print recipients without actually sending emails",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable debug logging",
    )

    # Allow `python main.py --mode upload <path>` positional shortcut
    args, extras = parser.parse_known_args()
    if args.mode == "upload" and not args.file and extras:
        args.file = extras[0]

    setup_logging(args.verbose)

    if args.agent and not args.mode:
        args.mode = "review"

    if args.mode == "research":
        return cmd_research(args)
    if args.mode == "pull":
        return cmd_pull(args)
    if args.mode == "daily":
        return cmd_daily(args)
    if args.mode == "upload":
        return cmd_upload(args)
    if args.mode == "review":
        return cmd_review(args)
    if args.mode == "draft":
        return cmd_draft(args)
    if args.mode == "dashboard":
        return cmd_dashboard(args)
    if args.mode == "send":
        return cmd_send(args)
    if args.mode == "migrate":
        return cmd_migrate(args)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
