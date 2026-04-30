"""
Metrics scoring engine.

Takes raw agent data and the current dynamic metric registry from
config/thresholds.json, produces a fully scored agent report dict ready for
gauge and template rendering.

The metric set is NOT hard-coded — whatever metrics are in thresholds.json
this run, get scored. This lets the threshold researcher swap KPIs as Zillow
changes the program.
"""

import json
import logging
from typing import Optional

from config.settings import THRESHOLDS_FILE

log = logging.getLogger(__name__)

# Status constants
GREEN = "green"
YELLOW = "yellow"
RED = "red"
NO_DATA = "no_data"


# ── Thresholds ────────────────────────────────────────────────────────────────

def load_thresholds() -> dict:
    """Load current thresholds from config/thresholds.json."""
    if not THRESHOLDS_FILE.exists():
        raise FileNotFoundError(
            f"thresholds.json not found at {THRESHOLDS_FILE}. "
            "Run: python main.py --mode research"
        )
    with open(THRESHOLDS_FILE) as f:
        data = json.load(f)

    metrics = data.get("metrics", {})
    if not metrics:
        raise ValueError(
            "thresholds.json has no metrics defined. "
            "Run: python main.py --mode research"
        )

    missing = [k for k, m in metrics.items() if m.get("target") is None]
    if missing:
        log.warning(
            "Thresholds not yet set for: %s. Run --mode research first.", missing
        )
    return data


def metric_keys(thresholds: Optional[dict] = None) -> list[str]:
    """Return the current metric keys, hero first then by weight desc."""
    if thresholds is None:
        thresholds = load_thresholds()
    metrics = thresholds.get("metrics", {})

    def sort_key(key: str) -> tuple:
        m = metrics[key]
        is_hero = 0 if m.get("gauge_size") == "hero" else 1
        weight = -float(m.get("weight", 0.0))
        return (is_hero, weight, key)

    return sorted(metrics.keys(), key=sort_key)


# ── Single-metric scoring ─────────────────────────────────────────────────────

def score_metric(
    metric_key: str,
    value: Optional[float],
    threshold: dict,
) -> dict:
    """
    Score a single metric against its threshold.

    Handles both higher-is-better (most metrics) and lower-is-better
    (e.g. Speed to Action — fewer seconds = better).
    """
    target = threshold.get("target")
    yellow_floor = threshold.get("yellow_floor")
    weight = threshold.get("weight", 1.0)
    gauge_size = threshold.get("gauge_size", "secondary")
    label = threshold.get("label", metric_key)
    unit = threshold.get("unit", "")
    direction = threshold.get("direction", "higher_is_better")

    base = {
        "key": metric_key,
        "label": label,
        "value": value,
        "target": target,
        "yellow_floor": yellow_floor,
        "weight": weight,
        "gauge_size": gauge_size,
        "unit": unit,
        "direction": direction,
    }

    if value is None or target is None or target == 0:
        return {**base, "pct_of_target": None, "status": NO_DATA}

    if direction == "lower_is_better":
        # Good when value <= target. pct_of_target encoded so 1.0 = at target,
        # >1.0 = beating target (faster), <1.0 = slower than target.
        pct = target / value if value > 0 else 0.0
        if value <= target:
            status = GREEN
        elif yellow_floor is not None and value <= yellow_floor:
            status = YELLOW
        else:
            status = RED
    else:
        pct = value / target
        if pct >= 1.0:
            status = GREEN
        elif yellow_floor is not None and value >= yellow_floor:
            status = YELLOW
        else:
            status = RED

    return {**base, "pct_of_target": pct, "status": status}


# ── Overall status ────────────────────────────────────────────────────────────

def overall_status(scored_metrics: list[dict]) -> str:
    """
    Weighted aggregate status across all metrics.

    Each scoreable metric contributes pct_of_target * weight; divide by total
    weight for a normalized score. Green ≥ 1.0, Yellow ≥ 0.85, Red < 0.85.
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
        "Preferred": GREEN,
        "At Risk": YELLOW,
        "Needs Improvement": RED,
        "No Data": NO_DATA,
    }.get(status_label, NO_DATA)


def operational_readiness(scored_metrics: list[dict]) -> Optional[float]:
    """
    Composite 0–100 readiness score across all available metrics.

    Same weighted-pct logic as overall_status, scaled to 0–100 and capped.
    Returns None if no metrics are scoreable.
    """
    scoreable = [m for m in scored_metrics if m["status"] != NO_DATA]
    if not scoreable:
        return None
    weighted_sum = sum(m["pct_of_target"] * m["weight"] for m in scoreable)
    total_weight = sum(m["weight"] for m in scoreable)
    score = weighted_sum / total_weight if total_weight > 0 else 0
    return round(min(score * 100, 125), 1)


# ── Full agent scoring ────────────────────────────────────────────────────────

def score_agent(agent_data: dict, thresholds: dict) -> dict:
    """Produce a fully scored report for a single agent."""
    metrics_cfg = thresholds.get("metrics", {})
    keys = metric_keys(thresholds)

    scored = {
        key: score_metric(key, agent_data.get(key), metrics_cfg.get(key, {}))
        for key in keys
    }

    metrics_list = [scored[k] for k in keys]
    status_label = overall_status(metrics_list)

    return {
        "agent_id": agent_data["agent_id"],
        "name": agent_data["name"],
        "email": agent_data["email"],
        "period": agent_data["period"],
        "metrics": scored,
        "metrics_list": metrics_list,
        "overall_status": status_label,
        "overall_color": overall_status_color(status_label),
        "operational_readiness": operational_readiness(metrics_list),
        "_error": agent_data.get("_error", False),
    }


def score_all_agents(agents_data: list[dict]) -> list[dict]:
    """Score all agents. Loads thresholds once and reuses."""
    thresholds = load_thresholds()
    return [score_agent(a, thresholds) for a in agents_data]


# ── Team summary + history ────────────────────────────────────────────────────

def team_summary(scored_agents: list[dict]) -> dict:
    """Aggregate stats for the team overview slide and dashboard."""
    if not scored_agents:
        return {}

    status_counts = {"Preferred": 0, "At Risk": 0, "Needs Improvement": 0, "No Data": 0}
    for agent in scored_agents:
        status_counts[agent["overall_status"]] = (
            status_counts.get(agent["overall_status"], 0) + 1
        )

    keys = list(scored_agents[0]["metrics"].keys())
    metric_avgs = {}
    for key in keys:
        vals = [
            a["metrics"][key]["value"]
            for a in scored_agents
            if a["metrics"][key]["value"] is not None
        ]
        metric_avgs[key] = round(sum(vals) / len(vals), 4) if vals else None

    def agent_score(a: dict) -> float:
        scoreable = [m for m in a["metrics_list"] if m["pct_of_target"] is not None]
        if not scoreable:
            return 0.0
        return sum(m["pct_of_target"] * m["weight"] for m in scoreable) / sum(
            m["weight"] for m in scoreable
        )

    sorted_agents = sorted(scored_agents, key=agent_score, reverse=True)

    return {
        "total_agents": len(scored_agents),
        "status_counts": status_counts,
        "metric_averages": metric_avgs,
        "top_performer": sorted_agents[0]["name"] if sorted_agents else None,
        "agents_ranked": [a["name"] for a in sorted_agents],
        "period": scored_agents[0]["period"] if scored_agents else "",
    }


def rolling_trend(
    agent_id: str,
    metric_key: str,
    window_months: int = 3,
) -> dict:
    """
    Pull the agent's last N months of values for a metric from SQLite history.

    Returns:
      {
        "values": [(period, value), …]   # oldest → newest
        "delta_pct": float | None        # newest vs oldest
        "sparkline": "<svg>"             # mini inline chart
      }

    Empty values list and None delta if no history found.
    """
    from src.storage import load_history  # local import avoids circular dep

    rows = load_history(agent_id, metric_key, window_months)
    rows.sort(key=lambda r: r[0])  # period ascending

    if not rows:
        return {"values": [], "delta_pct": None, "sparkline": ""}

    nums = [v for _, v in rows if v is not None]
    delta_pct = None
    if len(nums) >= 2 and nums[0] not in (0, None):
        delta_pct = round((nums[-1] - nums[0]) / abs(nums[0]) * 100, 1)

    return {
        "values": rows,
        "delta_pct": delta_pct,
        "sparkline": _sparkline_svg(nums),
    }


def _sparkline_svg(values: list[float], width: int = 80, height: int = 24) -> str:
    """Tiny inline SVG sparkline. Returns empty string if too few points."""
    if not values or len(values) < 2:
        return ""
    lo, hi = min(values), max(values)
    span = hi - lo if hi != lo else 1.0
    step = width / (len(values) - 1)
    points = " ".join(
        f"{i * step:.1f},{height - ((v - lo) / span) * height:.1f}"
        for i, v in enumerate(values)
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">'
        f'<polyline fill="none" stroke="currentColor" stroke-width="1.5" '
        f'stroke-linecap="round" stroke-linejoin="round" points="{points}" />'
        f"</svg>"
    )
