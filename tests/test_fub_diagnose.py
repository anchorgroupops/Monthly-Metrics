"""Tests for src/fub_diagnose.py — read-only diagnostic mode."""

import responses


class TestRunDiagnose:
    def test_returns_1_when_api_key_missing(self, monkeypatch, capsys):
        from src import fub_diagnose

        monkeypatch.setattr("config.settings.FUB_API_KEY", "")
        # The module imports FUB_API_KEY lazily inside run_diagnose, so set both.
        monkeypatch.setenv("FUB_API_KEY", "")

        rc = fub_diagnose.run_diagnose()

        assert rc == 1
        assert "FUB_API_KEY" in capsys.readouterr().out

    @responses.activate
    def test_prints_table_and_summary(self, monkeypatch, capsys):
        from config import settings
        from src import fub_client, fub_diagnose

        monkeypatch.setattr("config.settings.FUB_API_KEY", "test-key")
        monkeypatch.setattr(fub_client, "FUB_API_KEY", "test-key")
        monkeypatch.setattr(fub_client, "FUB_BASE_URL", "https://api.example.com")
        monkeypatch.setattr(fub_client, "OVERRIDE_REPORT_MONTH", "2026-04")
        monkeypatch.setattr(settings, "AGENTS", [])

        # fetch_users called for roster
        responses.get(
            "https://api.example.com/users",
            json={
                "users": [
                    {"id": 100, "name": "Alice", "email": "a@x.com", "role": "Agent"},
                    {"id": 200, "name": "Bob", "email": "b@x.com", "role": "Agent"},
                ],
                "_metadata": {},
            },
            status=200,
        )

        # /people for both agents — Alice has 1 Zillow lead, Bob has 2 non-Zillow.
        def people_callback(request):
            url = request.url
            if "assignedUserId=100" in url:
                return (
                    200,
                    {},
                    '{"_metadata": {"total": 1}, "people": ['
                    '{"id": 1, "sourceId": 14, "source": "Zillow Preferred"}]}',
                )
            return (
                200,
                {},
                '{"_metadata": {"total": 2}, "people": ['
                '{"id": 2, "sourceId": 7, "source": "Web Form"},'
                '{"id": 3, "sourceId": 7, "source": "Web Form"}]}',
            )

        responses.add_callback(
            responses.GET,
            "https://api.example.com/people",
            callback=people_callback,
        )

        rc = fub_diagnose.run_diagnose()

        assert rc == 0
        out = capsys.readouterr().out
        assert "Alice" in out
        assert "Bob" in out
        assert "1 agent(s) with Zillow leads" in out
        assert "1 empty" in out

    @responses.activate
    def test_filters_to_single_agent_when_name_provided(self, monkeypatch, capsys):
        from config import settings
        from src import fub_client, fub_diagnose

        monkeypatch.setattr("config.settings.FUB_API_KEY", "test-key")
        monkeypatch.setattr(fub_client, "FUB_API_KEY", "test-key")
        monkeypatch.setattr(fub_client, "FUB_BASE_URL", "https://api.example.com")
        monkeypatch.setattr(fub_client, "OVERRIDE_REPORT_MONTH", "2026-04")
        monkeypatch.setattr(
            settings,
            "AGENTS",
            [
                {"name": "Tom Oreste", "email": "t@x.com", "fub_agent_id": "100"},
                {"name": "Other Agent", "email": "o@x.com", "fub_agent_id": "200"},
            ],
        )

        responses.get(
            "https://api.example.com/people",
            json={"_metadata": {"total": 0}, "people": []},
            status=200,
        )

        rc = fub_diagnose.run_diagnose(agent_name="tom")

        assert rc == 0
        out = capsys.readouterr().out
        assert "Tom Oreste" in out
        assert "Other Agent" not in out

    def test_returns_1_when_no_agent_matches(self, monkeypatch, capsys):
        from config import settings
        from src import fub_client, fub_diagnose

        monkeypatch.setattr("config.settings.FUB_API_KEY", "test-key")
        monkeypatch.setattr(fub_client, "FUB_API_KEY", "test-key")
        monkeypatch.setattr(
            settings,
            "AGENTS",
            [{"name": "Alice", "email": "a@x.com", "fub_agent_id": "100"}],
        )

        rc = fub_diagnose.run_diagnose(agent_name="nonexistent")

        assert rc == 1
        assert "No agent matched" in capsys.readouterr().out


class TestDiagnoseAgent:
    @responses.activate
    def test_counts_raw_and_zillow_and_sources(self, monkeypatch):
        from src import fub_client, fub_diagnose

        monkeypatch.setattr(fub_client, "FUB_API_KEY", "test-key")
        monkeypatch.setattr(fub_client, "FUB_BASE_URL", "https://api.example.com")

        responses.get(
            "https://api.example.com/people",
            json={
                "_metadata": {"total": 3},
                "people": [
                    {"id": 1, "sourceId": 14, "source": "Zillow Preferred"},
                    {"id": 2, "sourceId": "14", "source": "Zillow Flex"},
                    {"id": 3, "sourceId": 7, "source": "Web Form"},
                ],
            },
            status=200,
        )

        row = fub_diagnose.diagnose_agent(
            {"fub_agent_id": "100", "name": "Alice", "email": "a@x.com"},
            "2026-04-01",
            "2026-04-30",
        )

        assert row["raw"] == 3
        assert row["zillow"] == 2  # string and int 14 both match after hardening
        assert row["source_ids"]["14"] == 1
        assert row["source_ids"]["'14'"] == 1
        assert row["error"] is None

    def test_returns_error_row_on_fetch_failure(self, monkeypatch):
        from src import fub_diagnose

        def boom(*_a, **_k):
            raise RuntimeError("network down")

        monkeypatch.setattr(fub_diagnose, "_fetch_people_raw", boom)

        row = fub_diagnose.diagnose_agent(
            {"fub_agent_id": "100", "name": "Alice", "email": "a@x.com"},
            "2026-04-01",
            "2026-04-30",
        )

        assert row["raw"] == 0
        assert row["zillow"] == 0
        assert "network down" in row["error"]
