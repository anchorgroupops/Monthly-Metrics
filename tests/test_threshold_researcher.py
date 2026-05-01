"""Tests for src/threshold_researcher.py — focuses on the JSON parsing and merge."""

import json
from types import SimpleNamespace

import pytest

from src import threshold_researcher
from src.threshold_researcher import (
    research_thresholds,
    update_thresholds_file,
)


def _claude_response(text: str):
    """Build a fake anthropic Messages.create response shaped like the real one."""
    block = SimpleNamespace(text=text, type="text")
    return SimpleNamespace(content=[block])


# ── research_thresholds ───────────────────────────────────────────────────────

class TestResearchThresholds:
    VALID_JSON = json.dumps({
        "source_notes": "From zillow.com/agent-resources",
        "metrics": {
            "pCVR": {"target": 0.035, "yellow_floor": 0.030, "unit": "percent"},
            "pickup_rate": {"target": 0.85, "yellow_floor": 0.75, "unit": "percent"},
            "csat": {"target": 4.5, "yellow_floor": 4.0, "unit": "score"},
            "zhl_transfers": {"target": 3, "yellow_floor": 2, "unit": "count"},
        },
    })

    def test_raises_when_api_key_missing(self, mocker):
        mocker.patch("src.threshold_researcher.ANTHROPIC_API_KEY", "")
        with pytest.raises(EnvironmentError, match="ANTHROPIC_API_KEY"):
            research_thresholds()

    def test_parses_plain_json_response(self, mocker):
        mocker.patch("src.threshold_researcher.ANTHROPIC_API_KEY", "key")
        fake_client = mocker.MagicMock()
        fake_client.messages.create.return_value = _claude_response(self.VALID_JSON)
        mocker.patch(
            "src.threshold_researcher.anthropic.Anthropic",
            return_value=fake_client,
        )
        result = research_thresholds()
        assert result["metrics"]["pCVR"]["target"] == 0.035

    def test_strips_json_code_fences(self, mocker):
        mocker.patch("src.threshold_researcher.ANTHROPIC_API_KEY", "key")
        fenced = f"```json\n{self.VALID_JSON}\n```"
        fake_client = mocker.MagicMock()
        fake_client.messages.create.return_value = _claude_response(fenced)
        mocker.patch(
            "src.threshold_researcher.anthropic.Anthropic",
            return_value=fake_client,
        )
        result = research_thresholds()
        assert result["metrics"]["pCVR"]["target"] == 0.035

    def test_strips_unlabelled_code_fences(self, mocker):
        mocker.patch("src.threshold_researcher.ANTHROPIC_API_KEY", "key")
        fenced = f"```\n{self.VALID_JSON}\n```"
        fake_client = mocker.MagicMock()
        fake_client.messages.create.return_value = _claude_response(fenced)
        mocker.patch(
            "src.threshold_researcher.anthropic.Anthropic",
            return_value=fake_client,
        )
        result = research_thresholds()
        assert result["metrics"]["pCVR"]["target"] == 0.035

    def test_raises_on_unparseable_json(self, mocker):
        mocker.patch("src.threshold_researcher.ANTHROPIC_API_KEY", "key")
        fake_client = mocker.MagicMock()
        fake_client.messages.create.return_value = _claude_response("not json at all")
        mocker.patch(
            "src.threshold_researcher.anthropic.Anthropic",
            return_value=fake_client,
        )
        with pytest.raises(ValueError, match="non-JSON"):
            research_thresholds()

    def test_raises_when_response_has_no_text_block(self, mocker):
        mocker.patch("src.threshold_researcher.ANTHROPIC_API_KEY", "key")
        # A web_search tool_use block without any text content.
        no_text = SimpleNamespace(content=[SimpleNamespace(type="tool_use")])
        fake_client = mocker.MagicMock()
        fake_client.messages.create.return_value = no_text
        mocker.patch(
            "src.threshold_researcher.anthropic.Anthropic",
            return_value=fake_client,
        )
        with pytest.raises(ValueError, match="no text content"):
            research_thresholds()

    def test_uses_configured_research_model_and_web_search_tool(self, mocker):
        mocker.patch("src.threshold_researcher.ANTHROPIC_API_KEY", "key")
        mocker.patch("src.threshold_researcher.RESEARCH_MODEL", "claude-test-model")
        fake_client = mocker.MagicMock()
        fake_client.messages.create.return_value = _claude_response(self.VALID_JSON)
        mocker.patch(
            "src.threshold_researcher.anthropic.Anthropic",
            return_value=fake_client,
        )
        research_thresholds()
        call = fake_client.messages.create.call_args
        assert call.kwargs["model"] == "claude-test-model"
        tools = call.kwargs["tools"]
        assert tools[0]["type"] == "web_search_20250305"


# ── update_thresholds_file ────────────────────────────────────────────────────

class TestUpdateThresholdsFile:
    def test_creates_file_when_missing(self, tmp_path, mocker):
        path = tmp_path / "thresholds.json"
        mocker.patch("src.threshold_researcher.THRESHOLDS_FILE", path)
        update_thresholds_file({
            "source_notes": "test",
            "metrics": {
                "pCVR": {"target": 0.035, "yellow_floor": 0.030, "unit": "percent"},
            },
        })
        loaded = json.loads(path.read_text())
        assert loaded["metrics"]["pCVR"]["target"] == 0.035

    def test_preserves_static_fields_on_merge(self, tmp_path, mocker):
        """The whole point of the merge — static metadata must survive a research run."""
        path = tmp_path / "thresholds.json"
        path.write_text(json.dumps({
            "metrics": {
                "pCVR": {
                    "target": 0.030,           # will be overwritten
                    "yellow_floor": 0.025,     # will be overwritten
                    "unit": "percent",
                    "weight": 2.0,             # MUST survive
                    "gauge_size": "hero",      # MUST survive
                    "label": "pCVR",           # MUST survive
                    "description": "Predicted Conversion Rate",  # MUST survive
                },
            },
        }))
        mocker.patch("src.threshold_researcher.THRESHOLDS_FILE", path)
        update_thresholds_file({
            "source_notes": "Updated 2026",
            "metrics": {
                "pCVR": {"target": 0.040, "yellow_floor": 0.035, "unit": "percent"},
            },
        })
        loaded = json.loads(path.read_text())
        m = loaded["metrics"]["pCVR"]
        # Researched fields updated.
        assert m["target"] == 0.040
        assert m["yellow_floor"] == 0.035
        # Static fields untouched.
        assert m["weight"] == 2.0
        assert m["gauge_size"] == "hero"
        assert m["label"] == "pCVR"
        assert m["description"] == "Predicted Conversion Rate"

    def test_adds_new_metric_key_not_in_existing_file(self, tmp_path, mocker):
        path = tmp_path / "thresholds.json"
        path.write_text(json.dumps({"metrics": {}}))
        mocker.patch("src.threshold_researcher.THRESHOLDS_FILE", path)
        update_thresholds_file({
            "source_notes": "x",
            "metrics": {
                "new_metric": {"target": 1.0, "yellow_floor": 0.8, "unit": "percent"},
            },
        })
        loaded = json.loads(path.read_text())
        assert loaded["metrics"]["new_metric"]["target"] == 1.0

    def test_writes_top_level_metadata(self, tmp_path, mocker):
        path = tmp_path / "thresholds.json"
        mocker.patch("src.threshold_researcher.THRESHOLDS_FILE", path)
        update_thresholds_file(
            {"source_notes": "src", "metrics": {}},
            year="2026",
        )
        loaded = json.loads(path.read_text())
        assert loaded["program_year"] == "2026"
        assert loaded["source"] == "src"
        assert "last_updated" in loaded
