"""
Read-only diagnostic for the monthly FUB pull.

Probes /v1/people for each agent in the roster (or a single agent matched by
name) and prints — without writing to SQLite — how many raw leads came back,
how many matched the Zillow Preferred filter, and a histogram of the
``sourceId`` and ``source`` values across the raw list. Surfaces the
information that ``fetch_all_agents`` only logs at INFO level, so the user
can confirm whether an agent who shows "No Data" on the leaderboard truly
had zero Zillow leads in the period or whether the filter / agent-id
matching is dropping real leads.
"""

from __future__ import annotations

import logging
from collections import Counter

from src.fub_client import _fetch_people_raw, _report_period, fetch_users
from src.fub_daily_metrics import is_zillow_preferred

log = logging.getLogger(__name__)


def _roster() -> list[dict]:
    from config.settings import AGENTS

    roster = list(AGENTS)
    if not roster:
        roster = fetch_users()
    return roster


def _match_agent(roster: list[dict], name: str) -> list[dict]:
    normalized = name.lower().strip()
    return [a for a in roster if normalized in a["name"].lower()]


def diagnose_agent(agent_cfg: dict, start_date: str, end_date: str) -> dict:
    """Fetch raw /people for one agent and tally what came back."""
    agent_id = agent_cfg["fub_agent_id"]
    name = agent_cfg["name"]

    try:
        people = _fetch_people_raw(agent_id, start_date, end_date)
    except Exception as exc:
        log.error("diagnose: %s id=%s fetch failed: %s", name, agent_id, exc)
        return {
            "name": name,
            "agent_id": agent_id,
            "raw": 0,
            "zillow": 0,
            "source_ids": Counter(),
            "source_names": Counter(),
            "error": str(exc),
        }

    zillow_count = sum(1 for p in people if is_zillow_preferred(p))
    source_ids = Counter(repr(p.get("sourceId")) for p in people)
    source_names = Counter((p.get("source") or "(none)") for p in people)

    return {
        "name": name,
        "agent_id": agent_id,
        "raw": len(people),
        "zillow": zillow_count,
        "source_ids": source_ids,
        "source_names": source_names,
        "error": None,
    }


def _format_row(row: dict) -> str:
    name = row["name"][:22]
    if row["error"]:
        return f"  {name:<22} {row['raw']:>4}  {row['zillow']:>6}  ERROR: {row['error'][:60]}"
    top_ids = ", ".join(f"{sid}:{n}" for sid, n in row["source_ids"].most_common(4))
    return f"  {name:<22} {row['raw']:>4}  {row['zillow']:>6}  {top_ids or '(no leads)'}"


def _format_source_names(row: dict) -> str:
    if row["error"] or not row["source_names"]:
        return ""
    top = ", ".join(f"{name!r}:{n}" for name, n in row["source_names"].most_common(4))
    return f"                                 source labels: {top}"


def run_diagnose(agent_name: str | None = None) -> int:
    """Run the diagnostic and print a table. Returns shell exit code."""
    from config.settings import FUB_API_KEY

    if not FUB_API_KEY:
        print(
            "  ERROR: FUB_API_KEY environment variable not set.\n"
            "  Set it in /opt/Monthly-Metrics/.env (production) or your shell."
        )
        return 1

    roster = _roster()
    if not roster:
        print("  FUB returned no agents.")
        return 1

    if agent_name:
        roster = _match_agent(roster, agent_name)
        if not roster:
            print(f"  No agent matched '{agent_name}'.")
            return 1

    start_date, end_date = _report_period()
    print("\n-- Diagnose Mode ----------------------------------------------------")
    print(f"  Period: {start_date} → {end_date}")
    print(f"  Agents: {len(roster)}\n")
    print(f"  {'Agent':<22} {'raw':>4}  {'zillow':>6}  top sourceId counts")
    print(f"  {'-' * 22} {'-' * 4}  {'-' * 6}  {'-' * 40}")

    summary = {"with_leads": 0, "empty": 0, "errored": 0, "total_zillow": 0}
    for agent_cfg in roster:
        row = diagnose_agent(agent_cfg, start_date, end_date)
        print(_format_row(row))
        name_line = _format_source_names(row)
        if name_line:
            print(name_line)
        if row["error"]:
            summary["errored"] += 1
        elif row["zillow"] > 0:
            summary["with_leads"] += 1
            summary["total_zillow"] += row["zillow"]
        else:
            summary["empty"] += 1

    print(
        f"\n  Summary: {summary['with_leads']} agent(s) with Zillow leads · "
        f"{summary['empty']} empty · {summary['errored']} errored · "
        f"{summary['total_zillow']} total Zillow leads.\n"
    )
    return 0
