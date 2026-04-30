"""Tests for the dynamic metric scoring engine."""

from __future__ import annotations

from tests.conftest import write_thresholds


def test_higher_is_better_green(isolated_thresholds):
    write_thresholds(isolated_thresholds, {
        "csat": {
            "label": "CSAT", "unit": "percent",
            "target": 0.85, "yellow_floor": 0.75,
            "direction": "higher_is_better",
            "weight": 1.0, "gauge_size": "hero",
            "description": "x",
        },
    })
    from src.metrics import load_thresholds, score_metric

    cfg = load_thresholds()["metrics"]["csat"]
    assert score_metric("csat", 0.90, cfg)["status"] == "green"
    assert score_metric("csat", 0.80, cfg)["status"] == "yellow"
    assert score_metric("csat", 0.50, cfg)["status"] == "red"


def test_lower_is_better_speed_to_action(isolated_thresholds):
    write_thresholds(isolated_thresholds, {
        "speed_to_action": {
            "label": "Speed to Action", "unit": "seconds",
            "target": 300, "yellow_floor": 600,
            "direction": "lower_is_better",
            "weight": 1.0, "gauge_size": "hero",
            "description": "x",
        },
    })
    from src.metrics import load_thresholds, score_metric

    cfg = load_thresholds()["metrics"]["speed_to_action"]

    # Under target (faster) → green
    assert score_metric("speed_to_action", 180, cfg)["status"] == "green"
    # Between target and yellow_floor → yellow
    assert score_metric("speed_to_action", 450, cfg)["status"] == "yellow"
    # Above yellow_floor → red
    assert score_metric("speed_to_action", 900, cfg)["status"] == "red"

    # pct_of_target encoding: above-target value gives pct < 1
    assert score_metric("speed_to_action", 600, cfg)["pct_of_target"] == 0.5


def test_no_data_when_value_missing(isolated_thresholds):
    write_thresholds(isolated_thresholds, {
        "csat": {
            "label": "CSAT", "unit": "percent",
            "target": 0.85, "yellow_floor": 0.75,
            "direction": "higher_is_better",
            "weight": 1.0, "gauge_size": "hero",
            "description": "x",
        },
    })
    from src.metrics import load_thresholds, score_metric

    cfg = load_thresholds()["metrics"]["csat"]
    result = score_metric("csat", None, cfg)
    assert result["status"] == "no_data"
    assert result["pct_of_target"] is None


def test_overall_status_weighted(isolated_thresholds):
    write_thresholds(isolated_thresholds, {
        "speed_to_action": {
            "label": "Speed", "unit": "seconds", "target": 300, "yellow_floor": 600,
            "direction": "lower_is_better", "weight": 1.0, "gauge_size": "hero",
            "description": "x",
        },
        "csat": {
            "label": "CSAT", "unit": "percent", "target": 0.85, "yellow_floor": 0.75,
            "direction": "higher_is_better", "weight": 0.5, "gauge_size": "secondary",
            "description": "x",
        },
    })
    from src.metrics import score_agent

    from src.metrics import load_thresholds

    agent = {
        "agent_id": "a1", "name": "A", "email": "a@x.com", "period": "April 2026",
        "speed_to_action": 200,   # green (under target)
        "csat": 0.90,             # green
    }
    scored = score_agent(agent, load_thresholds())
    assert scored["overall_status"] == "Preferred"
    assert scored["operational_readiness"] is not None
    assert scored["operational_readiness"] >= 100


def test_operational_readiness_handles_no_data(isolated_thresholds):
    write_thresholds(isolated_thresholds, {
        "csat": {
            "label": "CSAT", "unit": "percent", "target": 0.85, "yellow_floor": 0.75,
            "direction": "higher_is_better", "weight": 1.0, "gauge_size": "hero",
            "description": "x",
        },
    })
    from src.metrics import load_thresholds, score_agent

    agent = {
        "agent_id": "a1", "name": "A", "email": "a@x.com", "period": "April 2026",
        "csat": None,
    }
    scored = score_agent(agent, load_thresholds())
    assert scored["overall_status"] == "No Data"
    assert scored["operational_readiness"] is None


def test_metric_keys_hero_first_then_weight(isolated_thresholds):
    write_thresholds(isolated_thresholds, {
        "low_weight": {
            "label": "L", "unit": "percent", "target": 1, "yellow_floor": 0.5,
            "direction": "higher_is_better", "weight": 0.2, "gauge_size": "secondary",
            "description": "x",
        },
        "hero_metric": {
            "label": "H", "unit": "percent", "target": 1, "yellow_floor": 0.5,
            "direction": "higher_is_better", "weight": 0.5, "gauge_size": "hero",
            "description": "x",
        },
        "high_weight": {
            "label": "HW", "unit": "percent", "target": 1, "yellow_floor": 0.5,
            "direction": "higher_is_better", "weight": 0.9, "gauge_size": "secondary",
            "description": "x",
        },
    })
    from src.metrics import metric_keys

    assert metric_keys()[0] == "hero_metric"
    # Among non-heros, higher weight first.
    assert metric_keys()[1] == "high_weight"
    assert metric_keys()[2] == "low_weight"
