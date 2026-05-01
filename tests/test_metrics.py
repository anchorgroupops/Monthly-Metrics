"""Tests for the scoring engine in src/metrics.py."""

import json
from unittest.mock import patch

import pytest

from src import metrics
from src.metrics import (
    GREEN, NO_DATA, RED, YELLOW,
    METRIC_KEYS,
    load_thresholds,
    overall_status,
    overall_status_color,
    score_agent,
    score_all_agents,
    score_metric,
    team_summary,
)


# ── score_metric ──────────────────────────────────────────────────────────────

class TestScoreMetric:
    def _threshold(self, **overrides):
        base = {
            "target": 0.035, "yellow_floor": 0.030, "weight": 2.0,
            "gauge_size": "hero", "label": "pCVR", "unit": "percent",
        }
        base.update(overrides)
        return base

    def test_green_when_value_meets_target_exactly(self):
        result = score_metric("pCVR", 0.035, self._threshold())
        assert result["status"] == GREEN
        assert result["pct_of_target"] == pytest.approx(1.0)

    def test_green_when_value_above_target(self):
        result = score_metric("pCVR", 0.050, self._threshold())
        assert result["status"] == GREEN
        assert result["pct_of_target"] == pytest.approx(0.050 / 0.035)

    def test_yellow_when_value_at_yellow_floor(self):
        result = score_metric("pCVR", 0.030, self._threshold())
        assert result["status"] == YELLOW

    def test_yellow_between_floor_and_target(self):
        result = score_metric("pCVR", 0.032, self._threshold())
        assert result["status"] == YELLOW

    def test_red_below_yellow_floor(self):
        result = score_metric("pCVR", 0.020, self._threshold())
        assert result["status"] == RED

    def test_no_data_when_value_is_none(self):
        result = score_metric("pCVR", None, self._threshold())
        assert result["status"] == NO_DATA
        assert result["value"] is None
        assert result["pct_of_target"] is None
        # Target metadata should still pass through
        assert result["target"] == 0.035

    def test_no_data_when_target_is_none(self):
        result = score_metric("pCVR", 0.040, self._threshold(target=None))
        assert result["status"] == NO_DATA
        assert result["pct_of_target"] is None

    def test_no_data_when_target_is_zero(self):
        result = score_metric("pCVR", 0.040, self._threshold(target=0))
        assert result["status"] == NO_DATA
        assert result["pct_of_target"] is None

    def test_red_when_yellow_floor_missing_and_below_target(self):
        result = score_metric("pCVR", 0.020, self._threshold(yellow_floor=None))
        assert result["status"] == RED

    def test_defaults_applied_for_optional_threshold_keys(self):
        # Bare minimum threshold dict — should not crash and should fall back.
        result = score_metric("pCVR", 0.040, {"target": 0.035})
        assert result["status"] == GREEN
        assert result["weight"] == 1.0
        assert result["gauge_size"] == "secondary"
        assert result["label"] == "pCVR"        # falls back to metric_key
        assert result["unit"] == ""

    def test_passes_through_label_and_unit(self):
        result = score_metric("csat", 4.7, self._threshold(label="CSAT", unit="score"))
        assert result["label"] == "CSAT"
        assert result["unit"] == "score"

    def test_returned_dict_has_expected_keys(self):
        result = score_metric("pCVR", 0.04, self._threshold())
        assert set(result.keys()) == {
            "key", "label", "value", "target", "yellow_floor",
            "pct_of_target", "status", "weight", "gauge_size", "unit",
        }


# ── overall_status ────────────────────────────────────────────────────────────

class TestOverallStatus:
    def _scored(self, pct, weight=1.0, status=GREEN):
        return {"pct_of_target": pct, "weight": weight, "status": status}

    def test_preferred_when_weighted_score_at_one(self):
        metrics_list = [self._scored(1.0), self._scored(1.0)]
        assert overall_status(metrics_list) == "Preferred"

    def test_at_risk_at_exactly_point_eight_five(self):
        metrics_list = [self._scored(0.85, status=YELLOW)]
        assert overall_status(metrics_list) == "At Risk"

    def test_needs_improvement_just_below_point_eight_five(self):
        metrics_list = [self._scored(0.84, status=RED)]
        assert overall_status(metrics_list) == "Needs Improvement"

    def test_no_data_only_when_every_metric_missing(self):
        metrics_list = [
            self._scored(None, status=NO_DATA),
            self._scored(None, status=NO_DATA),
        ]
        assert overall_status(metrics_list) == "No Data"

    def test_no_data_metrics_excluded_from_weighting(self):
        # One scoreable metric at 1.0, two NO_DATA — should round to Preferred.
        metrics_list = [
            self._scored(1.2, weight=1.0, status=GREEN),
            self._scored(None, weight=2.0, status=NO_DATA),
            self._scored(None, weight=1.0, status=NO_DATA),
        ]
        assert overall_status(metrics_list) == "Preferred"

    def test_weighting_actually_weights(self):
        # pCVR pct=0.5 weight=2, others pct=1.0 weight=1 → (0.5*2 + 1.0 + 1.0) / 4 = 0.75
        metrics_list = [
            self._scored(0.5, weight=2.0),
            self._scored(1.0, weight=1.0),
            self._scored(1.0, weight=1.0),
        ]
        assert overall_status(metrics_list) == "Needs Improvement"


# ── overall_status_color ──────────────────────────────────────────────────────

class TestOverallStatusColor:
    @pytest.mark.parametrize("label,expected", [
        ("Preferred", GREEN),
        ("At Risk", YELLOW),
        ("Needs Improvement", RED),
        ("No Data", NO_DATA),
        ("Garbage", NO_DATA),  # unknown label falls through to no_data
    ])
    def test_label_to_color_mapping(self, label, expected):
        assert overall_status_color(label) == expected


# ── score_agent / score_all_agents ────────────────────────────────────────────

class TestScoreAgent:
    def test_produces_metrics_dict_and_ordered_list(self, agent_raw, thresholds_full):
        scored = score_agent(agent_raw, thresholds_full)
        assert set(scored["metrics"].keys()) == set(METRIC_KEYS)
        # metrics_list is hero-first
        assert scored["metrics_list"][0]["key"] == "pCVR"
        assert [m["key"] for m in scored["metrics_list"]] == [
            "pCVR", "pickup_rate", "csat", "zhl_transfers"
        ]

    def test_passes_through_identity_fields(self, agent_raw, thresholds_full):
        scored = score_agent(agent_raw, thresholds_full)
        assert scored["agent_id"] == "test-001"
        assert scored["name"] == "Test Agent"
        assert scored["email"] == "test@example.com"
        assert scored["period"] == "March 2026"

    def test_overall_status_and_color_present(self, agent_raw, thresholds_full):
        scored = score_agent(agent_raw, thresholds_full)
        assert scored["overall_status"] in {"Preferred", "At Risk", "Needs Improvement", "No Data"}
        assert scored["overall_color"] in {GREEN, YELLOW, RED, NO_DATA}

    def test_all_green_agent_is_preferred(self, agent_raw, thresholds_full):
        scored = score_agent(agent_raw, thresholds_full)
        assert scored["overall_status"] == "Preferred"

    def test_all_null_agent_is_no_data(self, thresholds_full):
        agent = {
            "agent_id": "x", "name": "X", "email": "x@x", "period": "p",
            "pCVR": None, "pickup_rate": None, "csat": None, "zhl_transfers": None,
        }
        scored = score_agent(agent, thresholds_full)
        assert scored["overall_status"] == "No Data"
        assert scored["overall_color"] == NO_DATA

    def test_error_flag_propagates(self, agent_raw, thresholds_full):
        agent_raw["_error"] = True
        scored = score_agent(agent_raw, thresholds_full)
        assert scored["_error"] is True

    def test_error_flag_defaults_false(self, agent_raw, thresholds_full):
        scored = score_agent(agent_raw, thresholds_full)
        assert scored["_error"] is False

    def test_missing_metric_in_thresholds_does_not_crash(self, agent_raw):
        # Empty thresholds → every metric should land in NO_DATA.
        scored = score_agent(agent_raw, {"metrics": {}})
        for key in METRIC_KEYS:
            assert scored["metrics"][key]["status"] == NO_DATA
        assert scored["overall_status"] == "No Data"


class TestScoreAllAgents:
    def test_loads_thresholds_once(self, agent_raw, thresholds_full, mocker):
        spy = mocker.patch("src.metrics.load_thresholds", return_value=thresholds_full)
        score_all_agents([agent_raw, agent_raw, agent_raw])
        assert spy.call_count == 1


# ── load_thresholds ───────────────────────────────────────────────────────────

class TestLoadThresholds:
    def test_raises_when_file_missing(self, tmp_path, mocker):
        missing = tmp_path / "nope.json"
        mocker.patch("src.metrics.THRESHOLDS_FILE", missing)
        with pytest.raises(FileNotFoundError, match="thresholds.json"):
            load_thresholds()

    def test_warns_but_returns_when_targets_unpopulated(self, tmp_path, caplog, mocker):
        path = tmp_path / "thresholds.json"
        path.write_text(json.dumps({
            "metrics": {k: {"target": None} for k in METRIC_KEYS}
        }))
        mocker.patch("src.metrics.THRESHOLDS_FILE", path)
        with caplog.at_level("WARNING"):
            data = load_thresholds()
        assert "Thresholds not yet set" in caplog.text
        assert data["metrics"]["pCVR"]["target"] is None

    def test_returns_data_when_fully_populated(self, tmp_path, thresholds_full, mocker):
        path = tmp_path / "thresholds.json"
        path.write_text(json.dumps(thresholds_full))
        mocker.patch("src.metrics.THRESHOLDS_FILE", path)
        data = load_thresholds()
        assert data["metrics"]["pCVR"]["target"] == 0.035


# ── team_summary ──────────────────────────────────────────────────────────────

class TestTeamSummary:
    def test_empty_input_returns_empty_dict(self):
        assert team_summary([]) == {}

    def test_status_counts_and_total(self, agent_raw, thresholds_full):
        scored = [score_agent(agent_raw, thresholds_full) for _ in range(3)]
        summary = team_summary(scored)
        assert summary["total_agents"] == 3
        assert summary["status_counts"]["Preferred"] == 3
        assert summary["status_counts"]["At Risk"] == 0
        assert summary["status_counts"]["Needs Improvement"] == 0

    def test_metric_averages_skip_none(self, thresholds_full):
        agents = [
            {"agent_id": "a", "name": "A", "email": "a@a", "period": "p",
             "pCVR": 0.04, "pickup_rate": None, "csat": 4.5, "zhl_transfers": 3},
            {"agent_id": "b", "name": "B", "email": "b@b", "period": "p",
             "pCVR": 0.02, "pickup_rate": 0.80, "csat": None, "zhl_transfers": 1},
        ]
        scored = [score_agent(a, thresholds_full) for a in agents]
        summary = team_summary(scored)
        # pCVR average uses both
        assert summary["metric_averages"]["pCVR"] == pytest.approx(0.03)
        # pickup_rate skips the None
        assert summary["metric_averages"]["pickup_rate"] == pytest.approx(0.80)
        # csat skips the None
        assert summary["metric_averages"]["csat"] == pytest.approx(4.5)

    def test_top_performer_ranks_by_weighted_score(self, thresholds_full):
        weak = {
            "agent_id": "weak", "name": "Weak", "email": "w@w", "period": "p",
            "pCVR": 0.010, "pickup_rate": 0.50, "csat": 3.0, "zhl_transfers": 0,
        }
        strong = {
            "agent_id": "strong", "name": "Strong", "email": "s@s", "period": "p",
            "pCVR": 0.060, "pickup_rate": 0.95, "csat": 5.0, "zhl_transfers": 6,
        }
        scored = [score_agent(weak, thresholds_full), score_agent(strong, thresholds_full)]
        summary = team_summary(scored)
        assert summary["top_performer"] == "Strong"
        assert summary["agents_ranked"] == ["Strong", "Weak"]

    def test_top_performer_when_all_no_data(self, thresholds_full):
        agent = {"agent_id": "x", "name": "X", "email": "x@x", "period": "p",
                 "pCVR": None, "pickup_rate": None, "csat": None, "zhl_transfers": None}
        scored = [score_agent(agent, thresholds_full)]
        summary = team_summary(scored)
        # Should not crash — returns a top performer but their score is 0.
        assert summary["top_performer"] == "X"

    def test_period_pulled_from_first_agent(self, agent_raw, thresholds_full):
        scored = [score_agent(agent_raw, thresholds_full)]
        summary = team_summary(scored)
        assert summary["period"] == "March 2026"
