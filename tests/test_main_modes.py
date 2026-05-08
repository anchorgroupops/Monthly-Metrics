"""Tests for main.py cmd_* mode handlers + helpers."""

import argparse

import pytest


def _args(**kwargs):
    """Build a Namespace with sensible defaults for cmd_* handlers."""
    defaults = {
        "file": None,
        "source": None,
        "period": None,
        "agent": None,
        "mock": False,
        "dry_run": False,
        "verbose": False,
        "mode": None,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


# ── cmd_research ──────────────────────────────────────────────────────────────


class TestCmdResearch:
    def test_dispatches_to_run_research(self, mocker):
        run_research = mocker.patch("src.threshold_researcher.run_research")
        from main import cmd_research

        rc = cmd_research(_args())

        assert rc == 0
        run_research.assert_called_once()


# ── cmd_upload ────────────────────────────────────────────────────────────────


class TestCmdUpload:
    def test_missing_file_returns_1(self, capsys):
        from main import cmd_upload

        rc = cmd_upload(_args(file=None))

        assert rc == 1
        assert "ERROR" in capsys.readouterr().out

    def test_valid_csv_persists(self, isolated_db, tmp_path):
        from main import cmd_upload

        csv = tmp_path / "april.csv"
        csv.write_text(
            "agent_id,name,email,period,speed_to_action,work_with_rate,csat,appt_set_rate,appt_met_rate\n"
            "100,Alice,alice@x.com,April 2026,120,0.62,0.91,0.71,0.78\n",
            encoding="utf-8",
        )

        rc = cmd_upload(_args(file=str(csv)))

        assert rc == 0

        from src import storage

        assert len(storage.load_period("2026-04")) == 1

    def test_json_extension_uses_json_source(self, isolated_db, tmp_path):
        from main import cmd_upload

        json_file = tmp_path / "april.json"
        json_file.write_text(
            '[{"agent_id":"100","name":"A","email":"a@x.com","period":"April 2026",'
            '"speed_to_action":120,"work_with_rate":0.6,"csat":0.9,'
            '"appt_set_rate":0.7,"appt_met_rate":0.7}]',
            encoding="utf-8",
        )

        rc = cmd_upload(_args(file=str(json_file)))
        assert rc == 0

    def test_missing_file_path_raises_filenotfound(self, isolated_db, capsys):
        from main import cmd_upload

        rc = cmd_upload(_args(file="/does/not/exist.csv"))

        assert rc == 1
        assert "ERROR" in capsys.readouterr().out

    def test_invalid_csv_raises_valueerror(self, isolated_db, tmp_path, capsys):
        from main import cmd_upload

        bad = tmp_path / "bad.csv"
        bad.write_text("nonsense\n")

        rc = cmd_upload(_args(file=str(bad)))
        assert rc == 1


# ── cmd_review / cmd_draft (use mock data path) ──────────────────────────────


class TestCmdReview:
    def test_mock_data_path(self, monkeypatch, tmp_path, isolated_db):
        from src import review_mode

        monkeypatch.setattr(review_mode, "REVIEW_DIR", tmp_path)

        from main import cmd_review

        rc = cmd_review(_args(mock=True))
        assert rc == 0
        # Output files should exist
        assert (tmp_path / "deck.html").exists()
        assert (tmp_path / "index.html").exists()

    def test_no_data_returns_1(self, isolated_db, monkeypatch):
        """Empty DB + no mock + no fub key → _check_fub_key exits."""
        from config import settings
        from main import cmd_review

        monkeypatch.setattr(settings, "FUB_API_KEY", "")

        with pytest.raises(SystemExit) as exc:
            cmd_review(_args(mock=False))
        assert exc.value.code == 1

    def test_agent_filter_no_match_returns_1(self, monkeypatch, tmp_path, isolated_db):
        from src import review_mode

        monkeypatch.setattr(review_mode, "REVIEW_DIR", tmp_path)

        from main import cmd_review

        rc = cmd_review(_args(mock=True, agent="ZZZ-NoSuchAgent-ZZZ"))
        assert rc == 1


class TestCmdDraft:
    def test_mock_data_queues_drafts(self, isolated_db):
        from main import cmd_draft
        from src import storage

        rc = cmd_draft(_args(mock=True))
        assert rc == 0

        drafts = storage.list_drafts()
        assert len(drafts) == 3  # mock_agents() returns 3

    def test_agent_filter_narrows_drafts(self, isolated_db):
        from main import cmd_draft
        from src import storage

        rc = cmd_draft(_args(mock=True, agent="Alex"))
        assert rc == 0

        drafts = storage.list_drafts()
        assert len(drafts) == 1
        # cmd_draft queues without populating agent_meta, so list_drafts'
        # LEFT JOIN returns None for name. Verify by agent_id instead.
        assert drafts[0]["agent_id"] == "mock-001"

    def test_no_data_returns_1(self, isolated_db, monkeypatch):
        from config import settings
        from main import cmd_draft

        monkeypatch.setattr(settings, "FUB_API_KEY", "")

        with pytest.raises(SystemExit):
            cmd_draft(_args(mock=False))

    def test_agent_filter_no_match_returns_1(self, isolated_db):
        from main import cmd_draft

        rc = cmd_draft(_args(mock=True, agent="ZZZ"))
        assert rc == 1


# ── cmd_dashboard ─────────────────────────────────────────────────────────────


class TestCmdDashboard:
    def test_calls_app_run(self, mocker):
        from main import cmd_dashboard

        # Mock create_app + .run() so we don't actually start a server
        mock_app = mocker.MagicMock()
        mocker.patch("src.dashboard.create_app", return_value=mock_app)

        rc = cmd_dashboard(_args())

        assert rc == 0
        mock_app.run.assert_called_once_with(host="127.0.0.1", port=5050, debug=False)


# ── cmd_send ──────────────────────────────────────────────────────────────────


class TestCmdSend:
    def test_no_approved_returns_1(self, isolated_db):
        from main import cmd_send

        rc = cmd_send(_args())
        assert rc == 1

    def test_dry_run_does_not_open_smtp(self, isolated_db, mocker):
        from main import cmd_send
        from src import storage

        storage.save_period(
            [
                {
                    "agent_id": "100",
                    "name": "A",
                    "email": "a@x",
                    "period": "April 2026",
                    "csat": 0.85,
                    "_raw": {},
                }
            ],
            source="csv",
        )
        d = storage.queue_draft("100", "April 2026", "<html/>")
        storage.approve_draft(d)

        smtp = mocker.patch("smtplib.SMTP")

        rc = cmd_send(_args(dry_run=True))

        assert rc == 0
        smtp.assert_not_called()

    def test_no_smtp_creds_returns_1(self, isolated_db, monkeypatch):
        import main

        monkeypatch.setattr(main, "SMTP_USER", "")
        monkeypatch.setattr(main, "SMTP_PASSWORD", "")

        from src import storage

        storage.save_period(
            [
                {
                    "agent_id": "100",
                    "name": "A",
                    "email": "a@x",
                    "period": "April 2026",
                    "csat": 0.85,
                    "_raw": {},
                }
            ],
            source="csv",
        )
        d = storage.queue_draft("100", "April 2026", "<html/>")
        storage.approve_draft(d)

        rc = main.cmd_send(_args(dry_run=False))
        assert rc == 1

    def test_sends_via_smtp(self, isolated_db, monkeypatch, mocker):
        import main

        monkeypatch.setattr(main, "SMTP_USER", "user@x.com")
        monkeypatch.setattr(main, "SMTP_PASSWORD", "pw")
        monkeypatch.setattr(main, "SMTP_HOST", "smtp.example.com")
        monkeypatch.setattr(main, "SMTP_PORT", 587)

        from src import storage

        storage.save_period(
            [
                {
                    "agent_id": "100",
                    "name": "A",
                    "email": "a@x.com",
                    "period": "April 2026",
                    "csat": 0.85,
                    "_raw": {},
                }
            ],
            source="csv",
        )
        d = storage.queue_draft("100", "April 2026", "<html/>")
        storage.approve_draft(d)

        smtp_class = mocker.patch("main.smtplib.SMTP")
        server = smtp_class.return_value.__enter__.return_value

        rc = main.cmd_send(_args(dry_run=False))

        assert rc == 0
        server.starttls.assert_called_once()
        server.login.assert_called_once_with("user@x.com", "pw")
        assert server.sendmail.call_count == 1

        sent = storage.list_drafts(status="sent")
        assert len(sent) == 1

    def test_smtp_error_returns_1(self, isolated_db, monkeypatch, mocker):
        import smtplib

        import main

        monkeypatch.setattr(main, "SMTP_USER", "user@x.com")
        monkeypatch.setattr(main, "SMTP_PASSWORD", "pw")

        smtp_class = mocker.patch("main.smtplib.SMTP")
        smtp_class.return_value.__enter__.side_effect = smtplib.SMTPException("boom")

        from src import storage

        storage.save_period(
            [
                {
                    "agent_id": "100",
                    "name": "A",
                    "email": "a@x",
                    "period": "April 2026",
                    "csat": 0.85,
                    "_raw": {},
                }
            ],
            source="csv",
        )
        d = storage.queue_draft("100", "April 2026", "<html/>")
        storage.approve_draft(d)

        rc = main.cmd_send(_args(dry_run=False))
        assert rc == 1


# ── cmd_agent ─────────────────────────────────────────────────────────────────


class TestCmdAgent:
    def test_aliases_to_cmd_review(self, mocker):
        from main import cmd_agent

        cmd_review = mocker.patch("main.cmd_review", return_value=0)
        rc = cmd_agent(_args())

        assert rc == 0
        cmd_review.assert_called_once()


# ── _load_source_agents ───────────────────────────────────────────────────────


class TestLoadSourceAgents:
    def test_mock_returns_mock_agents(self, isolated_db):
        from main import _load_source_agents

        agents = _load_source_agents(_args(mock=True))
        assert len(agents) == 3
        assert agents[0]["name"] == "Alex Rivera"

    def test_period_loads_from_storage(self, isolated_db):
        from main import _load_source_agents
        from src import storage

        storage.save_period(
            [
                {
                    "agent_id": "100",
                    "name": "A",
                    "email": "a@x",
                    "period": "April 2026",
                    "csat": 0.85,
                    "_raw": {},
                }
            ],
            source="csv",
        )

        agents = _load_source_agents(_args(period="2026-04"))
        assert len(agents) == 1

    def test_period_returns_empty_when_missing(self, isolated_db, capsys):
        from main import _load_source_agents

        agents = _load_source_agents(_args(period="1999-01"))
        assert agents == []
        assert "No data found" in capsys.readouterr().out

    def test_source_fub_calls_fub_client(self, isolated_db, monkeypatch, mocker):
        from config import settings
        from main import _load_source_agents

        monkeypatch.setattr(settings, "FUB_API_KEY", "test-key")

        fetch = mocker.patch(
            "src.fub_client.fetch_all_agents",
            return_value=[
                {
                    "agent_id": "100",
                    "name": "A",
                    "email": "a@x",
                    "period": "April 2026",
                    "csat": 0.85,
                    "_raw": {},
                }
            ],
        )

        agents = _load_source_agents(_args(source="fub"))

        fetch.assert_called_once()
        assert len(agents) == 1

    def test_default_prefers_storage_when_present(self, isolated_db, mocker):
        from main import _load_source_agents
        from src import storage

        storage.save_period(
            [
                {
                    "agent_id": "100",
                    "name": "A",
                    "email": "a@x",
                    "period": "April 2026",
                    "csat": 0.85,
                    "_raw": {},
                }
            ],
            source="csv",
        )
        # FUB should NOT be called
        fetch = mocker.patch("src.fub_client.fetch_all_agents")

        agents = _load_source_agents(_args())
        assert len(agents) == 1
        fetch.assert_not_called()

    def test_default_falls_back_to_fub_when_storage_empty(self, isolated_db, monkeypatch, mocker):
        from config import settings
        from main import _load_source_agents

        monkeypatch.setattr(settings, "FUB_API_KEY", "test-key")

        fetch = mocker.patch(
            "src.fub_client.fetch_all_agents",
            return_value=[
                {
                    "agent_id": "100",
                    "name": "A",
                    "email": "a@x",
                    "period": "April 2026",
                    "csat": 0.85,
                    "_raw": {},
                }
            ],
        )

        agents = _load_source_agents(_args())
        fetch.assert_called_once()
        assert len(agents) == 1


# ── _check_fub_key ────────────────────────────────────────────────────────────


class TestCheckFubKey:
    def test_passes_when_key_present(self, monkeypatch):
        from config import settings
        from main import _check_fub_key

        monkeypatch.setattr(settings, "FUB_API_KEY", "test-key")

        _check_fub_key()  # should not exit

    def test_exits_when_key_missing(self, monkeypatch, capsys):
        from config import settings
        from main import _check_fub_key

        monkeypatch.setattr(settings, "FUB_API_KEY", "")

        with pytest.raises(SystemExit) as exc:
            _check_fub_key()
        assert exc.value.code == 1
        assert "FUB_API_KEY" in capsys.readouterr().out


# ── cmd_pull (additional coverage beyond test_pull.py) ───────────────────────


class TestCmdPullBranches:
    def test_pull_with_no_agents_falls_back_to_discovery(
        self, isolated_db, monkeypatch, mocker
    ):
        from config import settings
        from main import cmd_pull

        monkeypatch.setattr(settings, "AGENTS", [])
        monkeypatch.setattr(settings, "FUB_API_KEY", "test-key")
        # fetch_all_agents returns [] when discovery yields no Agents/Brokers;
        # cmd_pull should treat that as "ok, nothing to save" not as "no roster".
        mocker.patch("src.fub_client.fetch_all_agents", return_value=[])

        rc = cmd_pull(_args())
        assert rc == 0

    def test_pull_with_no_api_key_returns_1(self, isolated_db, monkeypatch):
        from config import settings
        from main import cmd_pull

        monkeypatch.setattr(settings, "FUB_API_KEY", "")

        rc = cmd_pull(_args())
        assert rc == 1


# ── main() argparse dispatcher (gaps from test_main.py) ──────────────────────


class TestMainEntryPointGaps:
    def test_upload_mode_dispatches(self, mocker):
        from main import main as main_entry

        cmd_upload = mocker.patch("main.cmd_upload", return_value=0)
        mocker.patch("sys.argv", ["main.py", "--mode", "upload", "--file", "foo.csv"])

        rc = main_entry()
        assert rc == 0
        cmd_upload.assert_called_once()

    def test_pull_mode_dispatches(self, mocker):
        from main import main as main_entry

        cmd_pull = mocker.patch("main.cmd_pull", return_value=0)
        mocker.patch("sys.argv", ["main.py", "--mode", "pull"])

        rc = main_entry()
        assert rc == 0
        cmd_pull.assert_called_once()

    def test_review_mode_dispatches(self, mocker):
        from main import main as main_entry

        cmd_review = mocker.patch("main.cmd_review", return_value=0)
        mocker.patch("sys.argv", ["main.py", "--mode", "review", "--mock"])

        rc = main_entry()
        assert rc == 0
        cmd_review.assert_called_once()

    def test_draft_mode_dispatches(self, mocker):
        from main import main as main_entry

        cmd_draft = mocker.patch("main.cmd_draft", return_value=0)
        mocker.patch("sys.argv", ["main.py", "--mode", "draft", "--mock"])

        rc = main_entry()
        assert rc == 0
        cmd_draft.assert_called_once()

    def test_dashboard_mode_dispatches(self, mocker):
        from main import main as main_entry

        cmd_dashboard = mocker.patch("main.cmd_dashboard", return_value=0)
        mocker.patch("sys.argv", ["main.py", "--mode", "dashboard"])

        rc = main_entry()
        assert rc == 0
        cmd_dashboard.assert_called_once()

    def test_upload_mode_with_positional_path(self, mocker, tmp_path):
        """The positional `python main.py --mode upload <path>` shortcut populates args.file."""
        from main import main as main_entry

        cmd_upload = mocker.patch("main.cmd_upload", return_value=0)
        mocker.patch("sys.argv", ["main.py", "--mode", "upload", str(tmp_path / "x.csv")])

        rc = main_entry()
        assert rc == 0
        # cmd_upload was called with args; check args.file was populated from extras
        called_args = cmd_upload.call_args.args[0]
        assert called_args.file == str(tmp_path / "x.csv")


# ── cmd_migrate ───────────────────────────────────────────────────────────────


class TestCmdMigrate:
    def test_runs_pending_migrations(self, isolated_db, capsys):
        from main import cmd_migrate

        rc = cmd_migrate(_args())

        assert rc == 0
        out = capsys.readouterr().out
        assert "Migrate" in out

    def test_second_run_is_noop(self, isolated_db, capsys):
        from main import cmd_migrate

        cmd_migrate(_args())  # first run applies
        capsys.readouterr()  # discard first output
        rc = cmd_migrate(_args())

        assert rc == 0
        assert "No pending migrations" in capsys.readouterr().out

    def test_migrate_mode_dispatches(self, mocker):
        from main import main as main_entry

        cmd_migrate = mocker.patch("main.cmd_migrate", return_value=0)
        mocker.patch("sys.argv", ["main.py", "--mode", "migrate"])

        rc = main_entry()
        assert rc == 0
        cmd_migrate.assert_called_once()
