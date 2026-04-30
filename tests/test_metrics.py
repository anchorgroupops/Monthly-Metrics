"""
Tests for src/metrics.py — the core scoring engine.

This is the highest-priority test module: score_metric and overall_status
are pure functions that determine which agents are "Preferred" vs
"Needs Improvement", making correctness here business-critical.
"""

import json
import pytest
from unittest.mock import patch

from src.metrics import (
    score_metric,
    overall_status,
    overall_status_color,
    team_summary,
    score_agent,
    score_all_agents,
    GREEN, YELLOW, RED, NO_DATA,
)


# ── score_metric ──────────────────────────────────────────────────────────────

class TestScoreMetric:

    def test_green_when_value_meets_target(self):
        result = score_metric("pCVR", 0.040, {"target": 0.035, "yellow_floor": 0.030})
        assert result["status"] == GREEN
        assert result["pct_of_target"] == pytest.approx(0.040 / 0.035)

    def test_green_when_value_equals_target(self):
        result = score_metric("pCVR", 0.035, {"target": 0.035, "yellow_floor": 0.030})
        assert result["status"] == GREEN
        assert result["pct_of_target"] == pytest.approx(1.0)

    def test_yellow_when_value_between_floor_and_target(self):
        result = score_metric("pCVR", 0.032, {"target": 0.035, "yellow_floor": 0.030})
        assert result["status"] == YELLOW

    def test_yellow_when_value_equals_yellow_floor(self):
        result = score_metric("pCVR", 0.030, {"target": 0.035, "yellow_floor": 0.030})
        assert result["status"] == YELLOW

    def test_red_when_value_below_yellow_floor(self):
        result = score_metric("pCVR", 0.025, {"target": 0.035, "yellow_floor": 0.030})
        assert result["status"] == RED

    def test_red_when_no_yellow_floor_and_below_target(self):
        # Without a yellow_floor, anything below target should be RED
        result = score_metric("pCVR", 0.030, {"target": 0.035, "yellow_floor": None})
        assert result["status"] == RED

    def test_no_data_when_value_is_none(self):
        result = score_metric("pCVR", None, {"target": 0.035, "yellow_floor": 0.030})
        assert result["status"] == NO_DATA
        assert result["value"] is None
        assert result["pct_of_target"] is None

    def test_no_data_when_target_is_none(self):
        result = score_metric("pCVR", 0.038, {"target": None, "yellow_floor": None})
        assert result["status"] == NO_DATA
        assert result["pct_of_target"] is None

    def test_no_data_when_target_is_zero(self):
        result = score_metric("pCVR", 0.038, {"target": 0, "yellow_floor": 0})
        assert result["status"] == NO_DATA

    def test_default_weight_and_gauge_size(self):
        result = score_metric("pCVR", None, {})
        assert result["weight"] == 1.0
        assert result["gauge_size"] == "secondary"

    def test_label_falls_back_to_key(self):
        result = score_metric("pCVR", None, {})
        assert result["label"] == "pCVR"

    def test_custom_label_and_weight_are_passed_through(self):
        threshold = {
            "target": 0.035, "yellow_floor": 0.030,
            "label": "Conversion Rate", "weight": 0.65,
            "gauge_size": "hero", "unit": "percent",
        }
        result = score_metric("pCVR", 0.038, threshold)
        assert result["label"] == "Conversion Rate"
        assert result["weight"] == pytest.approx(0.65)
        assert result["gauge_size"] == "hero"
        assert result["unit"] == "percent"

    def test_output_shape_with_value(self):
        result = score_metric("csat", 4.7, {"target": 4.5, "yellow_floor": 4.0})
        for key in ("key", "label", "value", "target", "yellow_floor",
                    "pct_of_target", "status", "weight", "gauge_size", "unit"):
            assert key in result

    def test_output_shape_without_value(self):
        result = score_metric("csat", None, {})
        for key in ("key", "label", "value", "target", "yellow_floor",
                    "pct_of_target", "status", "weight", "gauge_size", "unit"):
            assert key in result


# ── overall_status ────────────────────────────────────────────────────────────

def _make_scored(pct, weight=1.0):
    """Minimal scored-metric dict for overall_status tests."""
    if pct is None:
        return {"pct_of_target": None, "weight": weight, "status": NO_DATA}
    return {"pct_of_target": pct, "weight": weight, "status": GREEN if pct >= 1.0 else RED}


class TestOverallStatus:

    def test_preferred_when_all_at_or_above_target(self):
        metrics = [_make_scored(1.0), _make_scored(1.1), _make_scored(1.0)]
        assert overall_status(metrics) == "Preferred"

    def test_at_risk_when_average_between_85_and_100(self):
        # score = (0.90 + 0.88) / 2 = 0.89 → "At Risk"
        metrics = [_make_scored(0.90), _make_scored(0.88)]
        assert overall_status(metrics) == "At Risk"

    def test_needs_improvement_when_average_below_85(self):
        metrics = [_make_scored(0.50), _make_scored(0.60)]
        assert overall_status(metrics) == "Needs Improvement"

    def test_no_data_when_all_metrics_are_no_data(self):
        metrics = [_make_scored(None), _make_scored(None)]
        assert overall_status(metrics) == "No Data"

    def test_no_data_with_empty_list(self):
        assert overall_status([]) == "No Data"

    def test_no_data_metrics_are_excluded_from_calculation(self):
        # One scoreable metric at 1.0 → "Preferred"; the NO_DATA one is ignored
        metrics = [_make_scored(1.0), _make_scored(None)]
        assert overall_status(metrics) == "Preferred"

    def test_weights_affect_outcome(self):
        # weight=2 metric at 0.80 drags a weight=1 metric at 1.0:
        # weighted_sum = 1.0*1 + 0.80*2 = 2.6; total_weight = 3; score = 0.867 → At Risk
        metrics = [
            {"pct_of_target": 1.0, "weight": 1.0, "status": GREEN},
            {"pct_of_target": 0.80, "weight": 2.0, "status": RED},
        ]
        assert overall_status(metrics) == "At Risk"

    def test_boundary_at_exactly_1_0(self):
        # Exactly 1.0 → Preferred
        metrics = [_make_scored(1.0)]
        assert overall_status(metrics) == "Preferred"

    def test_boundary_at_exactly_0_85(self):
        # Exactly 0.85 → At Risk (score >= 0.85)
        metrics = [_make_scored(0.85)]
        assert overall_status(metrics) == "At Risk"

    def test_just_below_0_85_is_needs_improvement(self):
        metrics = [_make_scored(0.849)]
        assert overall_status(metrics) == "Needs Improvement"


# ── overall_status_color ──────────────────────────────────────────────────────

class TestOverallStatusColor:

    def test_preferred_maps_to_green(self):
        assert overall_status_color("Preferred") == GREEN

    def test_at_risk_maps_to_yellow(self):
        assert overall_status_color("At Risk") == YELLOW

    def test_needs_improvement_maps_to_red(self):
        assert overall_status_color("Needs Improvement") == RED

    def test_no_data_maps_to_no_data(self):
        assert overall_status_color("No Data") == NO_DATA

    def test_unknown_string_maps_to_no_data(self):
        assert overall_status_color("something else") == NO_DATA


# ── score_agent ───────────────────────────────────────────────────────────────

class TestScoreAgent:

    @pytest.fixture
    def raw_agent(self):
        return {
            "agent_id": "mock-001",
            "name": "Alex Rivera",
            "email": "alex@example.com",
            "period": "March 2026",
            "pCVR": 0.038,
            "pickup_rate": 0.91,
            "csat": 4.7,
            "zhl_transfers": 5,
            "_raw": {},
        }

    @pytest.fixture
    def thresholds(self, sample_thresholds):
        return sample_thresholds

    def test_output_contains_identity_fields(self, raw_agent, thresholds):
        result = score_agent(raw_agent, thresholds)
        assert result["agent_id"] == "mock-001"
        assert result["name"] == "Alex Rivera"
        assert result["email"] == "alex@example.com"
        assert result["period"] == "March 2026"

    def test_output_contains_scored_metrics_dict(self, raw_agent, thresholds):
        result = score_agent(raw_agent, thresholds)
        assert "metrics" in result
        for key in ("pCVR", "pickup_rate", "csat", "zhl_transfers"):
            assert key in result["metrics"]

    def test_metrics_list_starts_with_pCVR(self, raw_agent, thresholds):
        result = score_agent(raw_agent, thresholds)
        assert result["metrics_list"][0]["key"] == "pCVR"

    def test_overall_status_and_color_present(self, raw_agent, thresholds):
        result = score_agent(raw_agent, thresholds)
        assert result["overall_status"] in ("Preferred", "At Risk", "Needs Improvement", "No Data")
        assert result["overall_color"] in (GREEN, YELLOW, RED, NO_DATA)

    def test_error_flag_defaults_to_false(self, raw_agent, thresholds):
        result = score_agent(raw_agent, thresholds)
        assert result["_error"] is False

    def test_error_flag_propagated(self, raw_agent, thresholds):
        raw_agent["_error"] = True
        result = score_agent(raw_agent, thresholds)
        assert result["_error"] is True

    def test_none_values_produce_no_data_status(self, raw_agent, thresholds):
        raw_agent["pCVR"] = None
        result = score_agent(raw_agent, thresholds)
        assert result["metrics"]["pCVR"]["status"] == NO_DATA


# ── team_summary ──────────────────────────────────────────────────────────────

def _make_agent(name, status, pcvr_val, pcvr_pct):
    """
    Build a minimal scored agent dict for team_summary tests.
    team_summary iterates over all METRIC_KEYS, so all four must be present.
    """
    def _null_metric(key):
        return {"key": key, "value": None, "pct_of_target": None, "weight": 0.65, "status": NO_DATA}

    pCVR_m = {
        "key": "pCVR", "value": pcvr_val, "pct_of_target": pcvr_pct,
        "weight": 1.0,
        "status": NO_DATA if pcvr_pct is None else (GREEN if pcvr_pct >= 1.0 else RED),
    }
    metrics = {
        "pCVR": pCVR_m,
        "pickup_rate": _null_metric("pickup_rate"),
        "csat": _null_metric("csat"),
        "zhl_transfers": _null_metric("zhl_transfers"),
    }
    return {
        "name": name,
        "overall_status": status,
        "metrics": metrics,
        "metrics_list": [pCVR_m],
        "period": "March 2026",
    }


class TestTeamSummary:

    def test_empty_list_returns_empty_dict(self):
        assert team_summary([]) == {}

    def test_total_agents_count(self):
        agents = [
            _make_agent("A", "Preferred", 0.04, 1.14),
            _make_agent("B", "At Risk", 0.03, 0.86),
        ]
        assert team_summary(agents)["total_agents"] == 2

    def test_status_counts(self):
        agents = [
            _make_agent("A", "Preferred", 0.04, 1.14),
            _make_agent("B", "At Risk", 0.03, 0.86),
            _make_agent("C", "Needs Improvement", 0.02, 0.57),
        ]
        counts = team_summary(agents)["status_counts"]
        assert counts["Preferred"] == 1
        assert counts["At Risk"] == 1
        assert counts["Needs Improvement"] == 1

    def test_metric_average_calculated_correctly(self):
        agents = [
            _make_agent("A", "Preferred", 0.040, 1.14),
            _make_agent("B", "Preferred", 0.020, 0.57),
        ]
        result = team_summary(agents)
        assert result["metric_averages"]["pCVR"] == pytest.approx(0.030, abs=1e-4)

    def test_metric_average_excludes_none_values(self):
        agents = [
            _make_agent("A", "Preferred", 0.040, 1.14),
            _make_agent("B", "No Data", None, None),
        ]
        result = team_summary(agents)
        assert result["metric_averages"]["pCVR"] == pytest.approx(0.040, abs=1e-4)

    def test_metric_average_all_none_returns_none(self):
        agents = [
            _make_agent("A", "No Data", None, None),
            _make_agent("B", "No Data", None, None),
        ]
        result = team_summary(agents)
        assert result["metric_averages"]["pCVR"] is None

    def test_top_performer_is_highest_scoring(self):
        agents = [
            _make_agent("Low", "Needs Improvement", 0.01, 0.29),
            _make_agent("High", "Preferred", 0.05, 1.43),
        ]
        assert team_summary(agents)["top_performer"] == "High"

    def test_agents_ranked_in_descending_score_order(self):
        agents = [
            _make_agent("Mid", "At Risk", 0.030, 0.86),
            _make_agent("Low", "Needs Improvement", 0.010, 0.29),
            _make_agent("High", "Preferred", 0.050, 1.43),
        ]
        ranked = team_summary(agents)["agents_ranked"]
        assert ranked == ["High", "Mid", "Low"]

    def test_period_taken_from_first_agent(self):
        agents = [_make_agent("A", "Preferred", 0.04, 1.14)]
        assert team_summary(agents)["period"] == "March 2026"
