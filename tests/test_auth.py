"""Tests for src/auth.py — magic-link issuance and session helpers."""

import pytest

from src import auth, storage
from src.mailer import SMTPCredentialsMissing


@pytest.fixture
def patched_db(tmp_db, monkeypatch):
    """Point the storage module at the isolated test database."""
    monkeypatch.setattr("src.storage.DATABASE_PATH", tmp_db)
    return tmp_db


@pytest.fixture
def alice(patched_db):
    storage.upsert_agents(
        [{"name": "Alice", "email": "alice@x", "fub_agent_id": "1"}],
        db_path=patched_db,
    )
    return storage.get_agent_by_email("alice@x", db_path=patched_db)


class TestIssueMagicLink:
    def test_unknown_email_returns_false_and_does_not_email(self, patched_db, mocker):
        send = mocker.patch("src.auth.send_html")
        assert auth.issue_magic_link("ghost@x") is False
        send.assert_not_called()

    def test_known_email_creates_link_and_sends(self, alice, mocker):
        send = mocker.patch("src.auth.send_html")
        ok = auth.issue_magic_link("alice@x")
        assert ok is True
        send.assert_called_once()
        # The mail body should contain the magic URL with a token.
        _, subject, body = send.call_args.args
        assert "Sign in" in subject
        assert "/verify?token=" in body

    def test_smtp_credentials_missing_propagates(self, alice, mocker):
        mocker.patch("src.auth.DEV_LOG_MAGIC_LINK", False)
        mocker.patch(
            "src.auth.send_html",
            side_effect=SMTPCredentialsMissing("nope"),
        )
        with pytest.raises(SMTPCredentialsMissing):
            auth.issue_magic_link("alice@x")

    def test_dev_log_swallows_smtp_error_and_logs_url(self, alice, mocker, caplog):
        # With the dev flag on, the URL should be logged instead of raising —
        # so localhost bring-up works without SMTP creds.
        mocker.patch("src.auth.DEV_LOG_MAGIC_LINK", True)
        mocker.patch(
            "src.auth.send_html",
            side_effect=SMTPCredentialsMissing("nope"),
        )
        with caplog.at_level("WARNING", logger="src.auth"):
            ok = auth.issue_magic_link("alice@x")
        assert ok is True
        assert any("/verify?token=" in r.message for r in caplog.records)


class TestVerifyToken:
    def test_valid_token_returns_agent(self, alice, mocker, patched_db):
        mocker.patch("src.auth.send_html")
        # Capture the issued token from the email URL.
        captured = {}
        def fake_send(to, subj, html):
            captured["html"] = html
        mocker.patch("src.auth.send_html", side_effect=fake_send)
        auth.issue_magic_link("alice@x")
        token = captured["html"].split("/verify?token=")[1].split('"')[0]
        agent = auth.verify_token(token)
        assert agent is not None
        assert agent["email"] == "alice@x"

    def test_invalid_token_returns_none(self, alice):
        assert auth.verify_token("not-real") is None

    def test_token_can_only_be_used_once(self, alice, mocker):
        captured = {}
        def fake_send(to, subj, html):
            captured["html"] = html
        mocker.patch("src.auth.send_html", side_effect=fake_send)
        auth.issue_magic_link("alice@x")
        token = captured["html"].split("/verify?token=")[1].split('"')[0]
        first = auth.verify_token(token)
        second = auth.verify_token(token)
        assert first is not None
        assert second is None


class TestSessionRoundTrip:
    def test_start_and_resolve(self, alice, patched_db):
        token = auth.start_session(alice["id"])

        class FakeRequest:
            cookies = {"anchor_session": token}
        agent = auth.current_agent(FakeRequest())
        assert agent is not None and agent["email"] == "alice@x"

    def test_end_session_clears(self, alice):
        token = auth.start_session(alice["id"])
        auth.end_session(token)

        class FakeRequest:
            cookies = {"anchor_session": token}
        assert auth.current_agent(FakeRequest()) is None

    def test_no_cookie_returns_none(self):
        class FakeRequest:
            cookies = {}
        assert auth.current_agent(FakeRequest()) is None
