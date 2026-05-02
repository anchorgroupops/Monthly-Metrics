"""Tests for main.py CLI behavior."""

import argparse

import pytest

import main as cli


def _args(**overrides):
    base = {"mock": False, "agent": None, "dry_run": False, "verbose": False, "mode": None}
    base.update(overrides)
    return argparse.Namespace(**base)


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
        smtp = mocker.patch("main.smtplib.SMTP")
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
        mocker.patch("main.SMTP_HOST", "smtp.example.com")
        mocker.patch("main.SMTP_PORT", 587)
        smtp_class = mocker.patch("main.smtplib.SMTP")
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


# ── _check_fub_key ────────────────────────────────────────────────────────────

class TestCheckFubKey:
    def test_exits_when_key_missing(self, mocker):
        mocker.patch("config.settings.FUB_API_KEY", "")
        with pytest.raises(SystemExit):
            cli._check_fub_key()

    def test_silent_when_key_present(self, mocker):
        mocker.patch("config.settings.FUB_API_KEY", "abc")
        cli._check_fub_key()  # no exception, no exit


# ── cmd_review ────────────────────────────────────────────────────────────────

class TestCmdReview:
    def test_mock_mode_skips_fub_key_check_and_runs_review(self, mocker):
        check = mocker.patch("main._check_fub_key")
        mock_data = [{"name": "A"}]
        mocker.patch("src.fub_client.mock_agents", return_value=mock_data)
        score = mocker.patch("src.metrics.score_all_agents", return_value=mock_data)
        run_review = mocker.patch("src.review_mode.run_review")
        rc = cli.cmd_review(_args(mock=True))
        assert rc == 0
        check.assert_not_called()
        score.assert_called_once_with(mock_data)
        run_review.assert_called_once()

    def test_live_mode_checks_fub_key_and_fetches(self, mocker):
        check = mocker.patch("main._check_fub_key")
        fetch = mocker.patch("src.fub_client.fetch_all_agents", return_value=[{"name": "A"}])
        mocker.patch("src.metrics.score_all_agents", return_value=[{"name": "A"}])
        mocker.patch("src.review_mode.run_review")
        rc = cli.cmd_review(_args())
        assert rc == 0
        check.assert_called_once()
        fetch.assert_called_once()

    def test_returns_1_when_no_agent_data(self, mocker, capsys):
        mocker.patch("src.fub_client.mock_agents", return_value=[])
        rc = cli.cmd_review(_args(mock=True))
        assert rc == 1
        assert "No agent data" in capsys.readouterr().out

    def test_filters_to_single_agent_when_provided(self, mocker):
        scored = [{"name": "Alice"}, {"name": "Bob"}]
        mocker.patch("src.fub_client.mock_agents", return_value=scored)
        mocker.patch("src.metrics.score_all_agents", return_value=scored)
        run_review = mocker.patch("src.review_mode.run_review")
        cli.cmd_review(_args(mock=True, agent="alice"))
        passed = run_review.call_args.args[0]
        assert [a["name"] for a in passed] == ["Alice"]

    def test_returns_1_when_filter_matches_nothing(self, mocker):
        scored = [{"name": "Alice"}]
        mocker.patch("src.fub_client.mock_agents", return_value=scored)
        mocker.patch("src.metrics.score_all_agents", return_value=scored)
        rc = cli.cmd_review(_args(mock=True, agent="nobody"))
        assert rc == 1


# ── cmd_send ──────────────────────────────────────────────────────────────────

class TestCmdSend:
    def test_runs_full_pipeline_in_dry_run(self, mocker):
        scored = [{"name": "Alice", "email": "a@a"}]
        emails = [{"agent": scored[0], "html": "<p/>", "slug": "alice"}]
        mocker.patch("src.fub_client.mock_agents", return_value=scored)
        mocker.patch("src.metrics.score_all_agents", return_value=scored)
        mocker.patch("src.email_builder.build_all_emails", return_value=emails)
        send = mocker.patch("main._send_emails")
        cli.cmd_send(_args(mock=True, dry_run=True))
        send.assert_called_once_with(emails, dry_run=True)

    def test_returns_1_when_no_agent_data(self, mocker):
        mocker.patch("src.fub_client.mock_agents", return_value=[])
        rc = cli.cmd_send(_args(mock=True))
        assert rc == 1


# ── cmd_research ──────────────────────────────────────────────────────────────

class TestCmdResearch:
    def test_calls_run_research(self, mocker):
        run = mocker.patch("src.threshold_researcher.run_research")
        rc = cli.cmd_research(_args())
        assert rc == 0
        run.assert_called_once()


# ── _send_emails — extra coverage ─────────────────────────────────────────────

class TestSendEmailsExtras:
    def test_subject_uses_template_with_period(self, mocker):
        import email
        mocker.patch("main.SMTP_USER", "u")
        mocker.patch("main.SMTP_PASSWORD", "p")
        smtp_class = mocker.patch("main.smtplib.SMTP")
        server = smtp_class.return_value.__enter__.return_value
        emails = [{
            "agent": {"name": "A", "email": "a@a", "period": "March 2026"},
            "html": "<html/>",
        }]
        cli._send_emails(emails, dry_run=False)
        sent_msg = server.sendmail.call_args.args[2]
        # Subject contains a non-ASCII em-dash so it's MIME-encoded — decode before asserting.
        parsed = email.message_from_string(sent_msg)
        decoded = str(email.header.make_header(email.header.decode_header(parsed["Subject"])))
        assert "Your March 2026 Performance Report" in decoded

    def test_from_header_includes_name_and_address(self, mocker):
        mocker.patch("main.SMTP_USER", "u")
        mocker.patch("main.SMTP_PASSWORD", "p")
        mocker.patch("main.EMAIL_FROM_NAME", "Anchor")
        mocker.patch("main.EMAIL_FROM_ADDRESS", "from@anchor.com")
        smtp_class = mocker.patch("main.smtplib.SMTP")
        server = smtp_class.return_value.__enter__.return_value
        emails = [{
            "agent": {"name": "A", "email": "a@a", "period": "March 2026"},
            "html": "<html/>",
        }]
        cli._send_emails(emails, dry_run=False)
        sent_msg = server.sendmail.call_args.args[2]
        assert "From: Anchor <from@anchor.com>" in sent_msg

    def test_html_part_is_text_html_utf8(self, mocker):
        mocker.patch("main.SMTP_USER", "u")
        mocker.patch("main.SMTP_PASSWORD", "p")
        smtp_class = mocker.patch("main.smtplib.SMTP")
        server = smtp_class.return_value.__enter__.return_value
        emails = [{
            "agent": {"name": "A", "email": "a@a", "period": "March 2026"},
            "html": "<html>safe</html>",
        }]
        cli._send_emails(emails, dry_run=False)
        sent_msg = server.sendmail.call_args.args[2]
        assert 'Content-Type: text/html; charset="utf-8"' in sent_msg

    def test_smtp_exception_exits_with_error(self, mocker):
        import smtplib
        mocker.patch("main.SMTP_USER", "u")
        mocker.patch("main.SMTP_PASSWORD", "p")
        smtp_class = mocker.patch("main.smtplib.SMTP")
        smtp_class.return_value.__enter__.side_effect = smtplib.SMTPException("boom")
        emails = [{
            "agent": {"name": "A", "email": "a@a", "period": "March 2026"},
            "html": "<html/>",
        }]
        with pytest.raises(SystemExit):
            cli._send_emails(emails, dry_run=False)
