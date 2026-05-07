"""
Daily sync job.

Triggered by `monthly-metrics-sync.timer` on the Pi (and exposed as
`python main.py --mode sync` for ad-hoc runs):

    1. Load roster from config/agents.csv
    2. Fetch each agent's metrics from FUB (or use mock data with --mock)
    3. Score against the current thresholds
    4. Upsert one snapshot row per agent for today

Idempotent — running the job twice on the same day overwrites the day's row
rather than producing duplicates.
"""

from __future__ import annotations

import logging

from src import storage
from src.metrics import score_all_agents
from src.roster import load_agents

log = logging.getLogger(__name__)


def run(mock: bool = False) -> dict:
    """
    Execute one sync. Returns summary stats useful for logging / tests.
    """
    agents = load_agents()
    if not agents:
        log.warning(
            "Roster is empty — populate config/agents.csv before scheduling syncs."
        )
        return {"agents": 0, "snapshots": 0}

    storage.upsert_agents(agents)

    if mock:
        from src.fub_client import mock_agents
        raw = mock_agents()
    else:
        from src.fub_client import fetch_all_agents
        raw = fetch_all_agents()

    scored = score_all_agents(raw)
    written = 0
    for s in scored:
        storage.write_snapshot(s)
        written += 1

    log.info("Daily sync complete: %d agents, %d snapshots", len(agents), written)
    return {"agents": len(agents), "snapshots": written}


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run the daily metrics sync.")
    parser.add_argument("--mock", action="store_true", help="Use mock FUB data")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )
    storage.init_schema()
    summary = run(mock=args.mock)
    print(f"  {summary['agents']} agents, {summary['snapshots']} snapshots written")
