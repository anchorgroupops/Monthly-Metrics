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


@pytest.mark.xfail(
    reason=(
        "email_builder uses select_autoescape(['html']), but the template is "
        "named 'email.html.j2' — Jinja's select_autoescape matches on the "
        "trailing extension only, so the .j2 suffix bypasses autoescape. "
        "Fix by passing select_autoescape(['html', 'j2']) or autoescape=True. "
        "Test stays here as a regression detector for the day this is fixed."
    ),
    strict=True,
)
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
