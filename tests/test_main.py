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


# ── _send_emails ──────────────────────────────────────────────────────────────

class TestSendEmails:
    def test_dry_run_does_not_open_smtp_connection(self, mocker, capsys):
        smtp = mocker.patch("src.mailer.smtplib.SMTP")
        emails = [
            {"agent": {"name": "Alice", "email": "a@a", "period": "March 2026"},
             "html": "<html/>"},
        ]
        cli._send_emails(emails, dry_run=True)
        smtp.assert_not_called()
        out = capsys.readouterr().out
        assert "DRY RUN" in out
        assert "Alice" in out

    def test_missing_smtp_credentials_exits(self, mocker):
        mocker.patch("main.SMTP_USER", "")
        mocker.patch("main.SMTP_PASSWORD", "")
        emails = [
            {"agent": {"name": "Alice", "email": "a@a", "period": "March 2026"},
             "html": "<html/>"},
        ]
        with pytest.raises(SystemExit):
            cli._send_emails(emails, dry_run=False)

    def test_sends_via_smtp_when_credentials_present(self, mocker):
        mocker.patch("main.SMTP_USER", "user")
        mocker.patch("main.SMTP_PASSWORD", "pw")
        mocker.patch("src.mailer.SMTP_USER", "user")
        mocker.patch("src.mailer.SMTP_PASSWORD", "pw")
        mocker.patch("src.mailer.SMTP_HOST", "smtp.example.com")
        mocker.patch("src.mailer.SMTP_PORT", 587)
        smtp_class = mocker.patch("src.mailer.smtplib.SMTP")
        server = smtp_class.return_value.__enter__.return_value

        emails = [
            {"agent": {"name": "Alice", "email": "a@a", "period": "March 2026"},
             "html": "<html/>"},
            {"agent": {"name": "Bob",   "email": "b@b", "period": "March 2026"},
             "html": "<html/>"},
        ]
        cli._send_emails(emails, dry_run=False)
        server.starttls.assert_called_once()
        server.login.assert_called_once_with("user", "pw")
        assert server.sendmail.call_count == 2


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
