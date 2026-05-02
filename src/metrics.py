"""
Metrics scoring engine.

Takes raw agent data from fub_client.py and thresholds from thresholds.json,
produces a fully scored agent report dict ready for gauge and template rendering.
"""

import json
import logging
from typing import Optional

from config.settings import THRESHOLDS_FILE

log = logging.getLogger(__name__)

# Status constants
GREEN  = "green"
YELLOW = "yellow"
RED    = "red"
NO_DATA = "no_data"

METRIC_KEYS = ["pCVR", "pickup_rate", "csat", "zhl_transfers"]


# ── Thresholds ────────────────────────────────────────────────────────────────

def load_thresholds() -> dict:
    """Load current thresholds from config/thresholds.json."""
    if not THRESHOLDS_FILE.exists():
        raise FileNotFoundError(
            f"thresholds.json not found at {THRESHOLDS_FILE}. "
            "Run: python main.py --mode research"
        )
    with open(THRESHOLDS_FILE) as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as e:
            raise ValueError(
                f"thresholds.json is not valid JSON ({e}). Re-run --mode research."
            ) from e

    if not isinstance(data, dict) or not isinstance(data.get("metrics"), dict):
        raise ValueError(
            "thresholds.json is malformed: expected an object with a 'metrics' "
            "object at the top level."
        )

    # Validate that thresholds have been populated
    metrics = data["metrics"]
    missing = [k for k in METRIC_KEYS if metrics.get(k, {}).get("target") is None]
    if missing:
        log.warning(
            "Thresholds not yet set for: %s. Run --mode research first.", missing
        )
    return data


# ── Single-metric scoring ─────────────────────────────────────────────────────

def score_metric(
    metric_key: str,
    value: Optional[float],
    threshold: dict,
) -> dict:
    """
    Score a single metric against its threshold.

    Returns:
    {
        "key":           str,
        "label":         str,
        "value":         float | None,
        "target":        float | None,
        "yellow_floor":  float | None,
        "pct_of_target": float | None,   # 0.0+ (>1.0 = above target)
        "status":        "green"|"yellow"|"red"|"no_data",
        "weight":        float,
        "gauge_size":    "hero"|"secondary",
        "unit":          str,
    }
    """
    target      = threshold.get("target")
    yellow_floor = threshold.get("yellow_floor")
    weight      = threshold.get("weight", 1.0)
    gauge_size  = threshold.get("gauge_size", "secondary")
    label       = threshold.get("label", metric_key)
    unit        = threshold.get("unit", "")

    if value is None:
        return {
            "key": metric_key, "label": label, "value": None,
            "target": target, "yellow_floor": yellow_floor,
            "pct_of_target": None, "status": NO_DATA,
            "weight": weight, "gauge_size": gauge_size, "unit": unit,
        }

    if target is None or target == 0:
        # Thresholds not yet researched — can't score
        pct = None
        status = NO_DATA
    else:
        pct = value / target
        if pct >= 1.0:
            status = GREEN
        elif yellow_floor is not None and value >= yellow_floor:
            status = YELLOW
        else:
            status = RED

    return {
        "key":           metric_key,
        "label":         label,
        "value":         value,
        "target":        target,
        "yellow_floor":  yellow_floor,
        "pct_of_target": pct,
        "status":        status,
        "weight":        weight,
        "gauge_size":    gauge_size,
        "unit":          unit,
    }


# ── Overall status ────────────────────────────────────────────────────────────

def overall_status(scored_metrics: list[dict]) -> str:
    """
    Weighted aggregate status across all metrics.

    Algorithm:
    - Each metric contributes (pct_of_target * weight) to a weighted sum.
    - Divide by total weight to get a normalized 0–1+ score.
    - Green ≥ 1.0, Yellow ≥ 0.85, Red < 0.85.
    - If any metric is NO_DATA, status is based on available metrics only;
      if ALL are NO_DATA, return "no_data".

    Returns: "Preferred" | "At Risk" | "Needs Improvement" | "No Data"
    """
    scoreable = [m for m in scored_metrics if m["status"] != NO_DATA]
    if not scoreable:
        return "No Data"

    weighted_sum = sum(m["pct_of_target"] * m["weight"] for m in scoreable)
    total_weight = sum(m["weight"] for m in scoreable)
    score = weighted_sum / total_weight if total_weight > 0 else 0

    if score >= 1.0:
        return "Preferred"
    elif score >= 0.85:
        return "At Risk"
    else:
        return "Needs Improvement"


def overall_status_color(status_label: str) -> str:
    return {
        "Preferred":           GREEN,
        "At Risk":             YELLOW,
        "Needs Improvement":   RED,
        "No Data":             NO_DATA,
    }.get(status_label, NO_DATA)


# ── Full agent scoring ────────────────────────────────────────────────────────

def score_agent(agent_data: dict, thresholds: dict) -> dict:
    """
    Produce a fully scored report for a single agent.

    Input:  raw agent dict from fub_client.py
    Output: scored dict ready for gauge rendering and template injection.
    """
    metrics_cfg = thresholds.get("metrics", {})

    scored = {
        key: score_metric(key, agent_data.get(key), metrics_cfg.get(key, {}))
        for key in METRIC_KEYS
    }

    # Ordered list for template iteration: hero first, then secondaries
    metrics_list = [scored["pCVR"]] + [scored[k] for k in METRIC_KEYS if k != "pCVR"]

    status_label = overall_status(metrics_list)

    return {
        # Identity
        "agent_id":       agent_data["agent_id"],
        "name":           agent_data["name"],
        "email":          agent_data["email"],
        "period":         agent_data["period"],
        # Per-metric scores
        "metrics":        scored,           # dict keyed by metric name
        "metrics_list":   metrics_list,     # ordered list for templates
        # Aggregate
        "overall_status": status_label,
        "overall_color":  overall_status_color(status_label),
        # Pass-through
        "_error":         agent_data.get("_error", False),
    }


def score_all_agents(agents_data: list[dict]) -> list[dict]:
    """Score all agents. Loads thresholds once and reuses."""
    thresholds = load_thresholds()
    return [score_agent(a, thresholds) for a in agents_data]


# ── Team summary ──────────────────────────────────────────────────────────────

def team_summary(scored_agents: list[dict]) -> dict:
    """
    Aggregate stats for the team overview slide.

    Returns counts by status, averages per metric, and top/bottom performers.
    """
    if not scored_agents:
        return {}

    status_counts = {"Preferred": 0, "At Risk": 0, "Needs Improvement": 0, "No Data": 0}
    for agent in scored_agents:
        status_counts[agent["overall_status"]] = (
            status_counts.get(agent["overall_status"], 0) + 1
        )

    # Per-metric team averages
    metric_avgs = {}
    for key in METRIC_KEYS:
        vals = [
            a["metrics"][key]["value"]
            for a in scored_agents
            if a["metrics"][key]["value"] is not None
        ]
        metric_avgs[key] = round(sum(vals) / len(vals), 4) if vals else None

    # Top performer by weighted score (reuse pct_of_target * weight logic)
    def agent_score(a):
        scoreable = [m for m in a["metrics_list"] if m["pct_of_target"] is not None]
        if not scoreable:
            return 0
        return sum(m["pct_of_target"] * m["weight"] for m in scoreable) / sum(
            m["weight"] for m in scoreable
        )

    sorted_agents = sorted(scored_agents, key=agent_score, reverse=True)

    return {
        "total_agents":    len(scored_agents),
        "status_counts":   status_counts,
        "metric_averages": metric_avgs,
        "top_performer":   sorted_agents[0]["name"] if sorted_agents else None,
        "agents_ranked":   [a["name"] for a in sorted_agents],
        "period":          scored_agents[0]["period"] if scored_agents else "",
    }
