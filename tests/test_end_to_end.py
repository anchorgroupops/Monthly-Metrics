"""End-to-end smoke tests for the full review pipeline.

Runs mock_agents → score_all_agents → build_all_emails + build_deck + run_review
and validates that the rendered output is well-formed HTML referencing every
agent. Catches data-shape drift between modules that unit tests miss.
"""

import json
from html.parser import HTMLParser

import pytest

from src.deck_builder import build_deck
from src.email_builder import build_all_emails
from src.fub_client import mock_agents
from src.metrics import score_all_agents
from src.review_mode import run_review


@pytest.fixture
def populated_thresholds_file(tmp_path, mocker, thresholds_full):
    """Write thresholds_full to a tmp file and point all consumers at it."""
    path = tmp_path / "thresholds.json"
    path.write_text(json.dumps(thresholds_full))
    mocker.patch("src.metrics.THRESHOLDS_FILE", path)
    mocker.patch("src.deck_builder.THRESHOLDS_FILE", path)
    return path


class _StrictHTMLParser(HTMLParser):
    """Tiny well-formedness check: parses without raising."""
    def error(self, message):
        raise AssertionError(message)


def _assert_parses(html: str):
    parser = _StrictHTMLParser()
    parser.feed(html)
    parser.close()


class TestPipelineReviewMode:
    def test_full_review_pipeline_produces_files_for_every_agent(
        self, tmp_path, mocker, populated_thresholds_file
    ):
        mocker.patch("src.review_mode.REVIEW_DIR", tmp_path / "review")
        agents = mock_agents()
        scored = score_all_agents(agents)
        run_review(scored)

        review_dir = tmp_path / "review"
        assert (review_dir / "index.html").exists()
        assert (review_dir / "deck.html").exists()
        # One file per mock agent.
        slugs = ["alex-rivera", "jordan-lee", "morgan-chen"]
        for slug in slugs:
            f = review_dir / f"{slug}.html"
            assert f.exists()
            _assert_parses(f.read_text())

    def test_index_lists_every_agent(
        self, tmp_path, mocker, populated_thresholds_file
    ):
        mocker.patch("src.review_mode.REVIEW_DIR", tmp_path / "review")
        scored = score_all_agents(mock_agents())
        run_review(scored)
        index = (tmp_path / "review" / "index.html").read_text()
        for name in ("Alex Rivera", "Jordan Lee", "Morgan Chen"):
            assert name in index


class TestPipelineSendMode:
    def test_each_email_shows_its_agents_status_badge(self, populated_thresholds_file):
        scored = score_all_agents(mock_agents())
        emails = build_all_emails(scored)
        assert len(emails) == 3
        for item in emails:
            html = item["html"]
            _assert_parses(html)
            assert item["agent"]["name"].split()[0] in html
            assert item["agent"]["period"] in html
            assert html.count("<svg") >= 4
            # Status badge from this agent's scored result must appear in the rendered email.
            assert item["agent"]["overall_status"] in html

    def test_pipeline_produces_both_extremes_with_mock_data(self, populated_thresholds_file):
        scored = score_all_agents(mock_agents())
        statuses = {a["overall_status"] for a in scored}
        # Mock dataset spans the high and low ends of the rubric.
        assert "Preferred" in statuses
        assert "Needs Improvement" in statuses


class TestPipelineDeck:
    def test_deck_includes_every_agent_and_parses(self, populated_thresholds_file):
        scored = score_all_agents(mock_agents())
        html = build_deck(scored)
        _assert_parses(html)
        for name in ("Alex Rivera", "Jordan Lee", "Morgan Chen"):
            assert name in html
        # Deck should embed gauges for every agent (4 metrics × 3 agents = 12 SVGs minimum).
        assert html.count("<svg") >= 12
