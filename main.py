#!/usr/bin/env python3
"""
Anchor Group Monthly Metrics — CLI Entry Point

Usage:
  python main.py --mode research              # Update thresholds via AI web research
  python main.py --mode review                # Generate all output locally for preview
  python main.py --mode review --mock         # Review using mock data (no FUB API needed)
  python main.py --mode send                  # Generate + send emails (called by n8n)
  python main.py --agent "Jane Smith"         # Preview a single agent (--mock optional)
"""

import argparse
import logging
import smtplib
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from config.settings import (
    AGENTS,
    BRAND,
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


# ── Mode: research ────────────────────────────────────────────────────────────

def cmd_research(args) -> int:
    from src.threshold_researcher import run_research
    print("\n── Researching Zillow Preferred thresholds… ──")
    run_research()
    return 0


# ── Mode: review ──────────────────────────────────────────────────────────────

def cmd_review(args) -> int:
    from src.fub_client import fetch_all_agents, mock_agents
    from src.metrics import score_all_agents
    from src.review_mode import run_review

    print("\n── Review Mode ──────────────────────────────────────────────────────")
    if args.mock:
        print("  Using MOCK data (no FUB API key required)")
        agents_data = mock_agents()
    else:
        _check_fub_key()
        print("  Fetching agent data from Follow Up Boss…")
        agents_data = fetch_all_agents()

    if not agents_data:
        print("  No agent data available. Check config/settings.py AGENTS list.")
        return 1

    print(f"  Scoring {len(agents_data)} agent(s)…")
    scored = score_all_agents(agents_data)

    if args.agent:
        scored = _filter_agent(scored, args.agent)
        if not scored:
            return 1

    print(f"  Generating review output…")
    run_review(scored)
    return 0


# ── Mode: send ────────────────────────────────────────────────────────────────

def cmd_send(args) -> int:
    from src.fub_client import fetch_all_agents, mock_agents
    from src.metrics import score_all_agents
    from src.email_builder import build_all_emails

    print("\n── Send Mode ────────────────────────────────────────────────────────")

    if args.mock:
        print("  Using MOCK data")
        agents_data = mock_agents()
    else:
        _check_fub_key()
        print("  Fetching agent data from Follow Up Boss…")
        agents_data = fetch_all_agents()

    if not agents_data:
        print("  No agent data. Aborting.")
        return 1

    print(f"  Scoring {len(agents_data)} agent(s)…")
    scored = score_all_agents(agents_data)

    if args.agent:
        scored = _filter_agent(scored, args.agent)
        if not scored:
            return 1

    emails = build_all_emails(scored)
    _send_emails(emails, dry_run=args.dry_run)
    return 0


# ── Mode: single agent ────────────────────────────────────────────────────────

def cmd_agent(args) -> int:
    """Preview a single agent — writes only their email to review/ and prints path."""
    from src.fub_client import mock_agents, fetch_all_agents
    from src.metrics import score_all_agents
    from src.review_mode import run_review

    print(f"\n── Single Agent Preview: {args.agent} ──")
    if args.mock:
        agents_data = mock_agents()
    else:
        _check_fub_key()
        agents_data = fetch_all_agents()

    scored = score_all_agents(agents_data)
    filtered = _filter_agent(scored, args.agent)
    if not filtered:
        return 1

    run_review(filtered)
    return 0


# ── Helpers ───────────────────────────────────────────────────────────────────

def _check_fub_key() -> None:
    from config.settings import FUB_API_KEY
    if not FUB_API_KEY:
        print(
            "  ERROR: FUB_API_KEY environment variable not set.\n"
            "  Set it with: export FUB_API_KEY=your_key\n"
            "  Or use --mock for local testing without a live key."
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


def _send_emails(emails: list[dict], dry_run: bool = False) -> None:
    """Send emails via SMTP. Use --dry-run to skip actual delivery."""
    if dry_run:
        print(f"  DRY RUN — would send {len(emails)} email(s):")
        for item in emails:
            print(f"    → {item['agent']['name']} <{item['agent']['email']}>")
        return

    if not SMTP_USER or not SMTP_PASSWORD:
        print(
            "  ERROR: SMTP credentials not set. Set SMTP_USER and SMTP_PASSWORD env vars.\n"
            "  Use --dry-run to skip sending."
        )
        sys.exit(1)

    print(f"  Connecting to {SMTP_HOST}:{SMTP_PORT}…")
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)

            for item in emails:
                agent = item["agent"]
                msg = MIMEMultipart("alternative")
                msg["Subject"] = EMAIL_SUBJECT_TEMPLATE.format(month=agent["period"])
                msg["From"]    = f"{EMAIL_FROM_NAME} <{EMAIL_FROM_ADDRESS}>"
                msg["To"]      = agent["email"]
                msg.attach(MIMEText(item["html"], "html", "utf-8"))

                server.sendmail(EMAIL_FROM_ADDRESS, agent["email"], msg.as_string())
                print(f"  ✓ Sent to {agent['name']} <{agent['email']}>")

    except smtplib.SMTPException as e:
        print(f"  SMTP error: {e}")
        sys.exit(1)

    print(f"\n  {len(emails)} email(s) sent successfully.\n")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Anchor Group Monthly Metrics — report generator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--mode",
        choices=["research", "review", "send"],
        help="Execution mode",
    )
    parser.add_argument(
        "--agent",
        metavar="NAME",
        help="Process a single agent by name (partial match ok)",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Use mock data instead of live FUB API",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="(send mode) Print recipients without actually sending emails",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug logging",
    )

    args = parser.parse_args()
    setup_logging(args.verbose)

    # Single-agent shortcut: defaults to review mode
    if args.agent and not args.mode:
        args.mode = "review"

    if args.mode == "research":
        return cmd_research(args)
    elif args.mode == "review":
        return cmd_review(args)
    elif args.mode == "send":
        return cmd_send(args)
    else:
        parser.print_help()
        return 0


if __name__ == "__main__":
    sys.exit(main())
