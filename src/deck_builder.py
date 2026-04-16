"""
Renders the Reveal.js team slide deck using the Jinja2 deck template.
"""

import json
import logging

from jinja2 import Environment, FileSystemLoader, select_autoescape

from config.settings import BRAND, TEMPLATES_DIR, THRESHOLDS_FILE
from src.gauges import build_all_gauges
from src.metrics import team_summary

log = logging.getLogger(__name__)

_env = None


def _get_env():
    global _env
    if _env is None:
        _env = Environment(
            loader=FileSystemLoader(str(TEMPLATES_DIR)),
            autoescape=False,   # SVG + HTML in template — disable auto-escaping
        )
    return _env


def build_deck(scored_agents: list[dict]) -> str:
    """
    Render the complete Reveal.js HTML deck for the team meeting.

    Args:
        scored_agents: List of outputs from metrics.score_agent()

    Returns:
        Complete HTML string for the deck.
    """
    if not scored_agents:
        raise ValueError("Cannot build deck with no agent data.")

    summary = team_summary(scored_agents)

    # Build gauges for every agent (dict keyed by agent_id → {metric: svg})
    gauges_by_agent = {
        agent["agent_id"]: build_all_gauges(agent)
        for agent in scored_agents
    }

    # Load thresholds for the team averages slide
    thresholds = {}
    if THRESHOLDS_FILE.exists():
        with open(THRESHOLDS_FILE) as f:
            thresholds = json.load(f)

    env = _get_env()
    template = env.get_template("deck.html.j2")

    html = template.render(
        agents=scored_agents,
        summary=summary,
        gauges_by_agent=gauges_by_agent,
        thresholds=thresholds,
        brand=BRAND,
    )
    log.debug("Built deck (%d slides, %d bytes)", len(scored_agents) + 3, len(html))
    return html
