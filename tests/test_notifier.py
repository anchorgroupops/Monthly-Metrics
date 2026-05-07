"""Tests for src/notifier.py — operational alert delivery via SMTP."""

import smtplib

import pytest

# notifier imports SMTP_*, ADMIN_EMAIL at module load — tests patch the notifier
# module's names, not config.settings's, so the function sees the patched values.


@pytest.fixture
def configured_notifier(monkeypatch):
    """Patch notifier's SMTP credentials so notify_admin_failure proceeds to send."""
    from src import notifier

    monkeypatch.setattr(notifier, "SMTP_USER", "user@example.com")
    monkeypatch.setattr(notifier, "SMTP_PASSWORD", "pw")
    monkeypatch.setattr(notifier, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(notifier, "SMTP_PORT", 587)
    monkeypatch.setattr(notifier, "ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setattr(notifier, "EMAIL_FROM_ADDRESS", "reports@example.com")
    monkeypatch.setattr(notifier, "EMAIL_FROM_NAME", "Anchor Reports")
    return notifier


# ── Soft-fail when not configured ─────────────────────────────────────────────


class TestSoftFailOnMissingConfig:
    def test_returns_false_when_smtp_user_missing(self, mocker, monkeypatch):
        from src import notifier

        monkeypatch.setattr(notifier, "SMTP_USER", "")
        monkeypatch.setattr(notifier, "SMTP_PASSWORD", "pw")
        monkeypatch.setattr(notifier, "ADMIN_EMAIL", "admin@x.com")
        smtp = mocker.patch("smtplib.SMTP")

        result = notifier.notify_admin_failure("subject", "body")

        assert result is False
        smtp.assert_not_called()

    def test_returns_false_when_smtp_password_missing(self, mocker, monkeypatch):
        from src import notifier

        monkeypatch.setattr(notifier, "SMTP_USER", "user@x.com")
        monkeypatch.setattr(notifier, "SMTP_PASSWORD", "")
        monkeypatch.setattr(notifier, "ADMIN_EMAIL", "admin@x.com")
        smtp = mocker.patch("smtplib.SMTP")

        result = notifier.notify_admin_failure("subject", "body")

        assert result is False
        smtp.assert_not_called()

    def test_returns_false_when_admin_email_missing(self, mocker, monkeypatch):
        from src import notifier

        monkeypatch.setattr(notifier, "SMTP_USER", "user@x.com")
        monkeypatch.setattr(notifier, "SMTP_PASSWORD", "pw")
        monkeypatch.setattr(notifier, "ADMIN_EMAIL", "")
        smtp = mocker.patch("smtplib.SMTP")

        result = notifier.notify_admin_failure("subject", "body")

        assert result is False
        smtp.assert_not_called()


# ── Happy path ───────────────────────────────────────────────────────────────


class TestSuccessfulSend:
    def test_returns_true_on_send(self, mocker, configured_notifier):
        smtp_class = mocker.patch("smtplib.SMTP")
        server = smtp_class.return_value.__enter__.return_value

        result = configured_notifier.notify_admin_failure("subject", "body")

        assert result is True
        smtp_class.assert_called_once_with("smtp.example.com", 587)
        server.starttls.assert_called_once()
        server.login.assert_called_once_with("user@example.com", "pw")
        assert server.sendmail.call_count == 1

    def test_sends_to_admin_email_only(self, mocker, configured_notifier):
        """Operational alerts must not BCC the agent roster — single recipient."""
        smtp_class = mocker.patch("smtplib.SMTP")
        server = smtp_class.return_value.__enter__.return_value

        configured_notifier.notify_admin_failure("subject", "body")

        from_addr, to_addrs, _msg = server.sendmail.call_args.args
        assert from_addr == "reports@example.com"
        assert to_addrs == ["admin@example.com"]
        assert len(to_addrs) == 1

    def test_subject_and_body_in_message(self, mocker, configured_notifier):
        from email import message_from_string

        smtp_class = mocker.patch("smtplib.SMTP")
        server = smtp_class.return_value.__enter__.return_value

        configured_notifier.notify_admin_failure("Pipeline failed: run #42", "Stack trace here.")

        msg_str = server.sendmail.call_args.args[2]
        assert "Pipeline failed: run #42" in msg_str

        # Body is base64-encoded by MIMEText default; parse and decode to verify.
        parsed = message_from_string(msg_str)
        body = parsed.get_payload(decode=True).decode("utf-8")
        assert body == "Stack trace here."

    def test_from_header_uses_configured_name(self, mocker, configured_notifier):
        smtp_class = mocker.patch("smtplib.SMTP")
        server = smtp_class.return_value.__enter__.return_value

        configured_notifier.notify_admin_failure("subject", "body")

        msg_str = server.sendmail.call_args.args[2]
        assert "Anchor Reports" in msg_str
        assert "reports@example.com" in msg_str


# ── Failure path ──────────────────────────────────────────────────────────────


class TestSMTPError:
    def test_returns_false_on_smtp_exception(self, mocker, configured_notifier, caplog):
        """Soft-fail: SMTP errors are logged, function returns False, doesn't raise."""
        import logging

        smtp_class = mocker.patch("smtplib.SMTP")
        smtp_class.return_value.__enter__.side_effect = smtplib.SMTPException("boom")

        with caplog.at_level(logging.ERROR, logger="src.notifier"):
            result = configured_notifier.notify_admin_failure("subject", "body")

        assert result is False
        assert any("SMTP error" in rec.message for rec in caplog.records)

    def test_returns_false_on_authentication_error(self, mocker, configured_notifier):
        smtp_class = mocker.patch("smtplib.SMTP")
        server = smtp_class.return_value.__enter__.return_value
        server.login.side_effect = smtplib.SMTPAuthenticationError(535, b"Bad creds")

        result = configured_notifier.notify_admin_failure("subject", "body")

        assert result is False
