"""
Renders per-agent HTML email strings using the Jinja2 email template.
"""

import logging
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from config.settings import BRAND, TEMPLATES_DIR
from src.gauges import build_all_gauges

log = logging.getLogger(__name__)

_env: Environment | None = None


def _get_env() -> Environment:
    global _env
    if _env is None:
        _env = Environment(
            loader=FileSystemLoader(str(TEMPLATES_DIR)),
            autoescape=select_autoescape(["html", "j2"]),
        )
    return _env


def build_email(scored_agent: dict) -> str:
    """
    Render a complete HTML email string for a single scored agent.

    Args:
        scored_agent: Output of metrics.score_agent()

    Returns:
        Complete HTML string ready to write to file or send via SMTP.
    """
    gauges = build_all_gauges(scored_agent)
    env = _get_env()
    template = env.get_template("email.html.j2")

    html = template.render(
        agent=scored_agent,
        gauges=gauges,
        brand=BRAND,
    )
    log.debug("Built email for %s (%d bytes)", scored_agent["name"], len(html))
    return html


def build_all_emails(scored_agents: list[dict]) -> list[dict]:
    """
    Build emails for all agents.

    Returns list of dicts:
    [{"agent": scored_agent, "html": "<full html>", "slug": "jane-smith"}, …]
    """
    results = []
    for agent in scored_agents:
        html = build_email(agent)
        slug = agent["name"].lower().replace(" ", "-")
        results.append({"agent": agent, "html": html, "slug": slug})
    return results
