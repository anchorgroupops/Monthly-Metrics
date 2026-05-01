"""Tests for src/review_mode.py."""

import pytest

from src import review_mode
from src.metrics import score_agent
from src.review_mode import _build_index, _slugify, run_review


# ── _slugify ──────────────────────────────────────────────────────────────────

class TestSlugify:
    @pytest.mark.parametrize("name,expected", [
        ("Jane Smith", "jane-smith"),
        ("Jane  Smith", "jane-smith"),
        ("Jane_Smith", "jane-smith"),
        ("Jane-Smith", "jane-smith"),
        ("Jane O'Brien", "jane-o-brien"),
        ("  Jane  ", "jane"),
        ("Jane!@#$Smith", "jane-smith"),
    ])
    def test_basic_slugification(self, name, expected):
        assert _slugify(name) == expected

    def test_collapses_repeated_separators(self):
        assert _slugify("a---b___c   d") == "a-b-c-d"


# ── run_review ────────────────────────────────────────────────────────────────

class TestRunReview:
    def test_writes_per_agent_files_and_deck_and_index(
        self, tmp_path, agent_raw, thresholds_full, mocker, capsys
    ):
        mocker.patch("src.review_mode.REVIEW_DIR", tmp_path)
        scored = [
            score_agent({**agent_raw, "name": "Alice Smith", "agent_id": "1"}, thresholds_full),
            score_agent({**agent_raw, "name": "Bob Jones",   "agent_id": "2"}, thresholds_full),
        ]
        run_review(scored)
        assert (tmp_path / "alice-smith.html").exists()
        assert (tmp_path / "bob-jones.html").exists()
        assert (tmp_path / "deck.html").exists()
        assert (tmp_path / "index.html").exists()

    def test_index_contains_links_to_each_agent(
        self, tmp_path, agent_raw, thresholds_full, mocker
    ):
        mocker.patch("src.review_mode.REVIEW_DIR", tmp_path)
        scored = [
            score_agent({**agent_raw, "name": "Alice Smith", "agent_id": "1"}, thresholds_full),
        ]
        run_review(scored)
        index_html = (tmp_path / "index.html").read_text()
        assert 'href="alice-smith.html"' in index_html
        assert 'href="deck.html"' in index_html


# ── _build_index ──────────────────────────────────────────────────────────────

class TestBuildIndex:
    def test_renders_status_badge_per_agent(self, agent_raw, thresholds_full):
        from src.email_builder import build_all_emails
        scored = [score_agent(agent_raw, thresholds_full)]
        emails = build_all_emails(scored)
        html = _build_index(emails, scored)
        # Test agent is at-or-above target → Preferred badge with checkmark.
        assert "Preferred" in html
        assert "✓" in html

    def test_renders_pcvr_value_or_na(self, agent_raw, thresholds_full):
        from src.email_builder import build_all_emails
        no_data_agent = {**agent_raw, "pCVR": None}
        scored = [score_agent(no_data_agent, thresholds_full)]
        emails = build_all_emails(scored)
        html = _build_index(emails, scored)
        assert "N/A" in html
