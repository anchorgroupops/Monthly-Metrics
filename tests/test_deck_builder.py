"""Smoke tests for src/deck_builder.py."""

import pytest

from src.deck_builder import build_deck
from src.metrics import score_agent


def test_build_deck_raises_on_empty_agents():
    with pytest.raises(ValueError, match="no agent data"):
        build_deck([])


def test_build_deck_renders_with_one_agent(agent_raw, thresholds_full):
    scored = [score_agent(agent_raw, thresholds_full)]
    html = build_deck(scored)
    assert "<html" in html.lower()
    assert "reveal" in html.lower()  # Reveal.js framework reference
    assert agent_raw["name"] in html


def test_build_deck_does_not_escape_gauge_svgs(agent_raw, thresholds_full):
    """deck_builder.py disables autoescape so gauge SVG strings render as markup,
    not as escaped text. Verify SVG opening tags appear unescaped."""
    scored = [score_agent(agent_raw, thresholds_full)]
    html = build_deck(scored)
    assert "<svg" in html
    assert "&lt;svg" not in html


def test_build_deck_includes_all_agents(agent_raw, thresholds_full):
    scored = [
        score_agent({**agent_raw, "name": "Alice", "agent_id": "1"}, thresholds_full),
        score_agent({**agent_raw, "name": "Bob",   "agent_id": "2"}, thresholds_full),
    ]
    html = build_deck(scored)
    assert "Alice" in html
    assert "Bob" in html
