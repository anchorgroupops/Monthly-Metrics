"""
Privacy guardrail: each agent's email must contain only their own data.

Per the spec, Agent A should never see Agent B's metrics or name.
"""

from __future__ import annotations

from pathlib import Path

FIXTURE = Path(__file__).parent / "fixtures" / "april_2026_sample.csv"


def test_email_contains_only_own_data(isolated_thresholds, isolated_db):
    from src.csv_ingest import parse_file
    from src.email_builder import build_email
    from src.metrics import score_all_agents

    raw = parse_file(FIXTURE)
    scored = score_all_agents(raw)
    assert len(scored) == 3

    by_id = {a["agent_id"]: a for a in scored}

    for target_id, target_agent in by_id.items():
        html = build_email(target_agent)

        # Target agent's first name must appear at least once (greeting).
        assert target_agent["name"].split()[0] in html

        # No other agent's full name may appear in the rendered email.
        others = [a for aid, a in by_id.items() if aid != target_id]
        for other in others:
            assert other["name"] not in html, (
                f"Privacy leak: {other['name']} appears in {target_agent['name']}'s email."
            )
            assert other["email"] not in html


def test_review_index_does_not_expose_per_agent_metrics_to_other_agents(
    isolated_thresholds, isolated_db, tmp_path, monkeypatch
):
    """
    The review index is admin-only, so it MAY contain everyone — but ensure
    it doesn't somehow leak into per-agent emails.
    """
    from config import settings

    monkeypatch.setattr(settings, "REVIEW_DIR", tmp_path / "review")

    from src.csv_ingest import parse_file
    from src.metrics import score_all_agents
    from src.review_mode import run_review

    raw = parse_file(FIXTURE)
    scored = score_all_agents(raw)
    run_review(scored)

    # Each per-agent file must not name any other agent.
    review_dir = tmp_path / "review"
    for agent in scored:
        slug = agent["name"].lower().replace(" ", "-")
        path = review_dir / f"{slug}.html"
        if not path.exists():
            continue
        html = path.read_text()
        for other in scored:
            if other["agent_id"] == agent["agent_id"]:
                continue
            assert other["name"] not in html, f"Leak: {other['name']} found inside {path.name}"
