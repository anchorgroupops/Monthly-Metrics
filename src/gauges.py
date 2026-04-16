"""
SVG arc gauge generator.

Produces inline SVG strings (no external dependencies) suitable for
embedding directly in HTML emails and Reveal.js slides.

Gauge layout:
  - Semicircle arc (180° sweep), drawn left→right
  - Background track arc (light gray)
  - Filled arc in status color (green / yellow / red)
  - Center label: formatted value
  - Bottom label: metric name

Sizing classes:
  - "hero"      → 200×120px  (pCVR — primary metric)
  - "secondary" → 130×80px   (Pickup Rate, CSAT, ZHL)
"""

import math
from typing import Optional

from config.settings import BRAND

# ── Color map ─────────────────────────────────────────────────────────────────

STATUS_COLORS = {
    "green":   BRAND["color_green"],
    "yellow":  BRAND["color_yellow"],
    "red":     BRAND["color_red"],
    "no_data": "#CCCCCC",
}

TRACK_COLOR  = "#E8ECEF"
TEXT_COLOR   = BRAND["color_text"]

# ── Size profiles ─────────────────────────────────────────────────────────────

SIZES = {
    "hero": {
        "width":        200,
        "height":       120,
        "cx":           100,     # arc center x
        "cy":           108,     # arc center y (below midpoint for semicircle)
        "radius":        82,
        "stroke_width":  18,
        "font_value":    26,
        "font_label":    11,
        "label_y_offset": 22,    # below cy
    },
    "secondary": {
        "width":        130,
        "height":        80,
        "cx":            65,
        "cy":            72,
        "radius":        53,
        "stroke_width":  13,
        "font_value":    18,
        "font_label":     9,
        "label_y_offset": 16,
    },
}

# ── Arc math ──────────────────────────────────────────────────────────────────

def _polar(cx: float, cy: float, r: float, angle_deg: float) -> tuple[float, float]:
    """Convert polar coordinates (angle in degrees from 3 o'clock) to Cartesian."""
    rad = math.radians(angle_deg)
    return cx + r * math.cos(rad), cy + r * math.sin(rad)


def _arc_path(cx: float, cy: float, r: float, start_deg: float, end_deg: float) -> str:
    """
    Build an SVG arc path string for a circular arc.
    Angles measured clockwise from right (standard SVG).
    For our semicircle: start=180° (left), end=0° (right).
    """
    x1, y1 = _polar(cx, cy, r, start_deg)
    x2, y2 = _polar(cx, cy, r, end_deg)
    # large-arc-flag: 1 if sweep > 180°
    sweep = (end_deg - start_deg) % 360
    large = 1 if sweep > 180 else 0
    return (
        f"M {x1:.3f} {y1:.3f} "
        f"A {r:.3f} {r:.3f} 0 {large} 1 {x2:.3f} {y2:.3f}"
    )


# ── Value formatting ──────────────────────────────────────────────────────────

def _format_value(value: Optional[float], unit: str, metric_key: str) -> str:
    """Human-readable value string for the gauge center label."""
    if value is None:
        return "N/A"
    if unit == "percent":
        return f"{value * 100:.1f}%"
    if unit == "score":
        return f"{value:.1f}"
    if unit == "count":
        return str(int(round(value)))
    # Fallback
    return f"{value:.2f}"


# ── Main gauge builder ────────────────────────────────────────────────────────

def build_gauge(
    value: Optional[float],
    target: Optional[float],
    status: str,
    label: str,
    unit: str,
    metric_key: str,
    size: str = "secondary",
) -> str:
    """
    Build and return an inline SVG arc gauge string.

    Args:
        value:      Raw metric value (e.g. 0.038 for pCVR, 4.7 for CSAT)
        target:     Threshold target for the metric
        status:     "green" | "yellow" | "red" | "no_data"
        label:      Display label (e.g. "Pickup Rate")
        unit:       "percent" | "score" | "count"
        metric_key: Internal key (e.g. "pCVR")
        size:       "hero" | "secondary"

    Returns:
        Inline SVG string.
    """
    s = SIZES.get(size, SIZES["secondary"])
    cx, cy, r = s["cx"], s["cy"], s["radius"]
    sw = s["stroke_width"]

    fill_color = STATUS_COLORS.get(status, STATUS_COLORS["no_data"])

    # Arc spans from 180° (left) to 0° (right) — a bottom-up semicircle
    start_deg = 180.0
    end_deg   = 0.0    # equivalent to 360°

    # Fraction filled: clamp 0–1
    if value is not None and target is not None and target > 0:
        fraction = min(max(value / target, 0.0), 1.25)  # allow slight overshoot
    elif value is not None and target is None:
        fraction = 0.0
    else:
        fraction = 0.0

    # Fill arc end angle: 180° + fraction * 180° (left→right)
    fill_end_deg = 180.0 + fraction * 180.0

    # Paths
    track_path = _arc_path(cx, cy, r, start_deg, end_deg)

    if fraction > 0:
        fill_path = _arc_path(cx, cy, r, start_deg, fill_end_deg)
        fill_arc = (
            f'<path d="{fill_path}" fill="none" '
            f'stroke="{fill_color}" stroke-width="{sw}" '
            f'stroke-linecap="round" />'
        )
    else:
        fill_arc = ""

    value_str = _format_value(value, unit, metric_key)

    # Truncate long labels to prevent overflow in smaller gauges
    display_label = label if len(label) <= 16 else label[:15] + "…"

    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{s["width"]}" height="{s["height"]}" '
        f'viewBox="0 0 {s["width"]} {s["height"]}" '
        f'role="img" aria-label="{label}: {value_str}">'

        # Background track
        f'<path d="{track_path}" fill="none" '
        f'stroke="{TRACK_COLOR}" stroke-width="{sw}" stroke-linecap="round" />'

        # Filled arc
        f'{fill_arc}'

        # Center value text
        f'<text x="{cx}" y="{cy - 4}" '
        f'text-anchor="middle" dominant-baseline="auto" '
        f'font-family="{BRAND["font_body"]}" '
        f'font-size="{s["font_value"]}" font-weight="700" '
        f'fill="{fill_color if status != "no_data" else TEXT_COLOR}">'
        f'{value_str}</text>'

        # Metric label below arc
        f'<text x="{cx}" y="{cy + s["label_y_offset"]}" '
        f'text-anchor="middle" dominant-baseline="hanging" '
        f'font-family="{BRAND["font_body"]}" '
        f'font-size="{s["font_label"]}" '
        f'fill="{TEXT_COLOR}" opacity="0.7">'
        f'{display_label}</text>'

        f'</svg>'
    )
    return svg


# ── Convenience wrappers ──────────────────────────────────────────────────────

def gauge_from_scored_metric(scored: dict) -> str:
    """
    Build a gauge directly from a scored metric dict (output of metrics.score_metric).
    Automatically selects hero/secondary size based on gauge_size field.
    """
    return build_gauge(
        value=scored["value"],
        target=scored["target"],
        status=scored["status"],
        label=scored["label"],
        unit=scored["unit"],
        metric_key=scored["key"],
        size=scored.get("gauge_size", "secondary"),
    )


def build_all_gauges(scored_agent: dict) -> dict:
    """
    Build all gauges for an agent and return a dict of SVG strings keyed by metric.

    Returns:
    {
        "pCVR":          "<svg>…</svg>",
        "pickup_rate":   "<svg>…</svg>",
        "csat":          "<svg>…</svg>",
        "zhl_transfers": "<svg>…</svg>",
    }
    """
    return {
        key: gauge_from_scored_metric(metric)
        for key, metric in scored_agent["metrics"].items()
    }
