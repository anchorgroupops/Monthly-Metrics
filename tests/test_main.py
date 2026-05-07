"""Tests for main.py CLI behavior."""

import pytest

import main as cli


# ── _filter_agent ─────────────────────────────────────────────────────────────

class TestFilterAgent:
    def _agents(self):
        return [
            {"name": "Alice Smith"},
            {"name": "Bob Jones"},
            {"name": "Charlie Brown"},
        ]

    def test_exact_match(self):
        out = cli._filter_agent(self._agents(), "Alice Smith")
        assert len(out) == 1
        assert out[0]["name"] == "Alice Smith"

    def test_case_insensitive(self):
        out = cli._filter_agent(self._agents(), "ALICE")
        assert len(out) == 1
        assert out[0]["name"] == "Alice Smith"

    def test_partial_match(self):
        out = cli._filter_agent(self._agents(), "smith")
        assert len(out) == 1
        assert out[0]["name"] == "Alice Smith"

    def test_no_match_returns_empty_list(self, capsys):
        out = cli._filter_agent(self._agents(), "Zelda")
        assert out == []
        captured = capsys.readouterr()
        assert "No agent found" in captured.out
        # Lists available names for the user
        assert "Alice Smith" in captured.out

    def test_whitespace_is_stripped(self):
        out = cli._filter_agent(self._agents(), "  alice  ")
        assert len(out) == 1


# NOTE: TestSendEmails was dropped during cherry-pick from
# claude/notebooklm-mcp-access-js94b — that branch's main.py exposed a
# `_send_emails` helper, but our main.py inlines SMTP delivery in cmd_send().
# Re-add equivalent tests against cmd_send() in P1.


# ── argparse entry point ──────────────────────────────────────────────────────

class TestMainEntryPoint:
    def test_single_agent_shortcut_defaults_to_review(self, mocker):
        """`--agent NAME` with no `--mode` should run the review pipeline."""
        cmd_review = mocker.patch("main.cmd_review", return_value=0)
        mocker.patch("sys.argv", ["main.py", "--agent", "Alice", "--mock"])
        rc = cli.main()
        assert rc == 0
        cmd_review.assert_called_once()

    def test_no_args_prints_help_and_returns_zero(self, mocker, capsys):
        mocker.patch("sys.argv", ["main.py"])
        rc = cli.main()
        assert rc == 0
        out = capsys.readouterr().out
        assert "Anchor Group Monthly Metrics" in out

    def test_research_mode_dispatches_to_cmd_research(self, mocker):
        cmd_research = mocker.patch("main.cmd_research", return_value=0)
        mocker.patch("sys.argv", ["main.py", "--mode", "research"])
        cli.main()
        cmd_research.assert_called_once()

    def test_send_mode_dispatches_to_cmd_send(self, mocker):
        cmd_send = mocker.patch("main.cmd_send", return_value=0)
        mocker.patch("sys.argv", ["main.py", "--mode", "send", "--dry-run"])
        cli.main()
        cmd_send.assert_called_once()
