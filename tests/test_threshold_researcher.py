"""Tests for src/threshold_researcher.py — Anthropic-driven KPI discovery."""

import json
from types import SimpleNamespace

import pytest


def _claude_response(text: str):
    """Build a fake anthropic Messages.create response shaped like the real one."""
    block = SimpleNamespace(text=text, type="text")
    return SimpleNamespace(content=[block])


VALID_RESEARCH = {
    "source_notes": "From zillow.com/agent-resources",
    "metrics": {
        "speed_to_action": {
            "label": "Speed to Action",
            "unit": "seconds",
            "target": 120,
            "yellow_floor": 180,
            "direction": "lower_is_better",
            "weight": 1.0,
            "gauge_size": "hero",
            "description": "Time to first contact.",
        }
    },
}
VALID_RESEARCH_JSON = json.dumps(VALID_RESEARCH)


# ── research_thresholds ──────────────────────────────────────────────────────


class TestResearchThresholds:
    def test_raises_oserror_when_api_key_missing(self, monkeypatch):
        from src import threshold_researcher as r

        monkeypatch.setattr(r, "ANTHROPIC_API_KEY", "")

        with pytest.raises(OSError, match="ANTHROPIC_API_KEY"):
            r.research_thresholds()

    def test_happy_path_returns_parsed_dict(self, mocker, monkeypatch):
        from src import threshold_researcher as r

        monkeypatch.setattr(r, "ANTHROPIC_API_KEY", "test-key")
        mock_client = mocker.MagicMock()
        mock_client.messages.create.return_value = _claude_response(VALID_RESEARCH_JSON)
        mocker.patch("anthropic.Anthropic", return_value=mock_client)

        result = r.research_thresholds(year="2026")

        assert result == VALID_RESEARCH
        mock_client.messages.create.assert_called_once()

    def test_strips_markdown_code_fences(self, mocker, monkeypatch):
        """If Claude wraps JSON in ```json ... ``` fences, the parser still works."""
        from src import threshold_researcher as r

        monkeypatch.setattr(r, "ANTHROPIC_API_KEY", "test-key")
        fenced = f"```json\n{VALID_RESEARCH_JSON}\n```"
        mock_client = mocker.MagicMock()
        mock_client.messages.create.return_value = _claude_response(fenced)
        mocker.patch("anthropic.Anthropic", return_value=mock_client)

        result = r.research_thresholds()

        assert result == VALID_RESEARCH

    def test_raises_valueerror_on_non_json_response(self, mocker, monkeypatch):
        from src import threshold_researcher as r

        monkeypatch.setattr(r, "ANTHROPIC_API_KEY", "test-key")
        mock_client = mocker.MagicMock()
        mock_client.messages.create.return_value = _claude_response("This is not JSON.")
        mocker.patch("anthropic.Anthropic", return_value=mock_client)

        with pytest.raises(ValueError, match="non-JSON"):
            r.research_thresholds()

    def test_raises_valueerror_when_response_has_no_text(self, mocker, monkeypatch):
        """If the response has no text-bearing block (e.g. only tool-use), raise."""
        from src import threshold_researcher as r

        monkeypatch.setattr(r, "ANTHROPIC_API_KEY", "test-key")

        # A block without `.text` attribute simulates a non-text content block.
        non_text_block = SimpleNamespace(type="tool_use")
        empty_response = SimpleNamespace(content=[non_text_block])
        mock_client = mocker.MagicMock()
        mock_client.messages.create.return_value = empty_response
        mocker.patch("anthropic.Anthropic", return_value=mock_client)

        with pytest.raises(ValueError, match="no text content"):
            r.research_thresholds()

    def test_uses_current_year_when_year_arg_none(self, mocker, monkeypatch):
        """If year is None, the prompt is formatted with today's year."""
        from datetime import date

        from src import threshold_researcher as r

        monkeypatch.setattr(r, "ANTHROPIC_API_KEY", "test-key")
        mock_client = mocker.MagicMock()
        mock_client.messages.create.return_value = _claude_response(VALID_RESEARCH_JSON)
        mocker.patch("anthropic.Anthropic", return_value=mock_client)

        r.research_thresholds()  # year=None

        call_args = mock_client.messages.create.call_args
        prompt = call_args.kwargs["messages"][0]["content"]
        assert str(date.today().year) in prompt


# ── update_thresholds_file ────────────────────────────────────────────────────


class TestUpdateThresholdsFile:
    def test_writes_new_json(self, tmp_path, monkeypatch):
        from src import threshold_researcher as r

        target = tmp_path / "thresholds.json"
        monkeypatch.setattr(r, "THRESHOLDS_FILE", target)

        r.update_thresholds_file(VALID_RESEARCH, year="2026")

        assert target.exists()
        loaded = json.loads(target.read_text())
        assert loaded["program_year"] == "2026"
        assert loaded["metrics"] == VALID_RESEARCH["metrics"]
        assert loaded["source"] == "From zillow.com/agent-resources"
        assert "last_updated" in loaded

    def test_backs_up_existing_file(self, tmp_path, monkeypatch):
        from src import threshold_researcher as r

        target = tmp_path / "thresholds.json"
        target.write_text(json.dumps({"metrics": {"original": "kept_in_backup"}}))

        monkeypatch.setattr(r, "THRESHOLDS_FILE", target)

        r.update_thresholds_file(VALID_RESEARCH, year="2026")

        backup = target.with_suffix(".json.bak")
        assert backup.exists()
        backup_data = json.loads(backup.read_text())
        assert backup_data["metrics"] == {"original": "kept_in_backup"}

    def test_raises_when_research_has_no_metrics(self, tmp_path, monkeypatch):
        from src import threshold_researcher as r

        target = tmp_path / "thresholds.json"
        monkeypatch.setattr(r, "THRESHOLDS_FILE", target)

        with pytest.raises(ValueError, match="no metrics"):
            r.update_thresholds_file({"source_notes": "x", "metrics": {}})

        assert not target.exists()

    def test_uses_current_year_when_year_arg_none(self, tmp_path, monkeypatch):
        from datetime import date

        from src import threshold_researcher as r

        target = tmp_path / "thresholds.json"
        monkeypatch.setattr(r, "THRESHOLDS_FILE", target)

        r.update_thresholds_file(VALID_RESEARCH)  # year=None

        loaded = json.loads(target.read_text())
        assert loaded["program_year"] == str(date.today().year)


# ── run_research (end-to-end) ─────────────────────────────────────────────────


class TestRunResearch:
    def test_end_to_end_with_mock_claude(self, tmp_path, mocker, monkeypatch, capsys):
        from src import threshold_researcher as r

        target = tmp_path / "thresholds.json"
        monkeypatch.setattr(r, "ANTHROPIC_API_KEY", "test-key")
        monkeypatch.setattr(r, "THRESHOLDS_FILE", target)

        mock_client = mocker.MagicMock()
        mock_client.messages.create.return_value = _claude_response(VALID_RESEARCH_JSON)
        mocker.patch("anthropic.Anthropic", return_value=mock_client)

        r.run_research(year="2026")

        assert target.exists()
        loaded = json.loads(target.read_text())
        assert loaded["program_year"] == "2026"

        out = capsys.readouterr().out
        assert "Thresholds updated" in out
        assert "speed_to_action" in out
