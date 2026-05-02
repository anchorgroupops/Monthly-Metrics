"""Smoke + safety tests for src/email_builder.py."""

import pytest

from src.email_builder import build_all_emails, build_email
from src.metrics import score_agent


def test_build_email_renders_without_error(agent_raw, thresholds_full):
    scored = score_agent(agent_raw, thresholds_full)
    html = build_email(scored)
    assert isinstance(html, str) and len(html) > 0


def test_build_email_contains_agent_identity(agent_raw, thresholds_full):
    scored = score_agent(agent_raw, thresholds_full)
    html = build_email(scored)
    # The template greets the agent by first name (agent.name.split()[0]).
    first_name = agent_raw["name"].split()[0]
    assert first_name in html
    assert agent_raw["period"] in html


def test_build_email_embeds_inline_svg_gauges(agent_raw, thresholds_full):
    scored = score_agent(agent_raw, thresholds_full)
    html = build_email(scored)
    # All four metric gauges should appear inline.
    assert html.count("<svg") >= 4


def test_build_email_autoescapes_agent_name(thresholds_full):
    """If autoescape were active, an agent name with HTML tags would be escaped."""
    raw = {
        "agent_id": "x",
        "name": "<script>alert(1)</script>",
        "email": "x@x",
        "period": "March 2026",
        "pCVR": 0.04, "pickup_rate": 0.9, "csat": 4.7, "zhl_transfers": 4,
    }
    scored = score_agent(raw, thresholds_full)
    html = build_email(scored)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_build_email_uses_table_based_layout(agent_raw, thresholds_full):
    """Email clients (Outlook, older Gmail) require table-based layout, not flex/grid."""
    scored = score_agent(agent_raw, thresholds_full)
    html = build_email(scored)
    assert html.count("<table") >= 3
    # Avoid flexbox/grid in body styles — they don't render in major email clients.
    assert "display:flex" not in html.replace(" ", "")
    assert "display:grid" not in html.replace(" ", "")


def test_build_email_renders_status_badge_label(agent_raw, thresholds_full):
    scored = score_agent(agent_raw, thresholds_full)
    html = build_email(scored)
    assert scored["overall_status"] in html


def test_build_email_renders_at_risk_badge(thresholds_full):
    """Yellow band: weighted score in [0.85, 1.0). A simple way: scale all metrics down ~10%."""
    raw = {
        "agent_id": "x", "name": "Test", "email": "t@t", "period": "March 2026",
        "pCVR": 0.0315, "pickup_rate": 0.77, "csat": 4.05, "zhl_transfers": 2,
    }
    scored = score_agent(raw, thresholds_full)
    assert scored["overall_status"] == "At Risk"
    html = build_email(scored)
    assert "At Risk" in html


def test_build_email_uses_brand_palette(agent_raw, thresholds_full):
    from config.settings import BRAND
    scored = score_agent(agent_raw, thresholds_full)
    html = build_email(scored)
    # Page background color from BRAND must appear inline.
    assert BRAND["color_bg"] in html
    # Footer message appears (autoescape converts apostrophes — compare on a
    # stable unescaped fragment instead of the full sentence).
    assert "Keep showing up with integrity" in html
    assert "sets great agents apart" in html


def test_build_all_emails_returns_one_per_agent(agent_raw, thresholds_full):
    scored = [
        score_agent({**agent_raw, "name": "A", "agent_id": "1"}, thresholds_full),
        score_agent({**agent_raw, "name": "B", "agent_id": "2"}, thresholds_full),
    ]
    results = build_all_emails(scored)
    assert len(results) == 2
    assert {r["agent"]["name"] for r in results} == {"A", "B"}
    for r in results:
        assert "html" in r
        assert "slug" in r
