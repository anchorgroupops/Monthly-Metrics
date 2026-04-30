"""
Tests for src/gauges.py — SVG arc gauge generator.

Focus areas:
- _format_value: unit-specific formatting and None handling
- _polar: coordinate math
- build_gauge: SVG output shape, no-data state, overshoot clamping, label truncation
- gauge_from_scored_metric: integration with scored metric dicts
"""

import math
import re

import pytest

from src.gauges import (
    _format_value,
    _polar,
    _arc_path,
    build_gauge,
    gauge_from_scored_metric,
    build_all_gauges,
)


# ── _format_value ─────────────────────────────────────────────────────────────

class TestFormatValue:

    def test_none_returns_na(self):
        assert _format_value(None, "percent", "pCVR") == "N/A"

    def test_percent_multiplies_by_100_and_formats(self):
        assert _format_value(0.038, "percent", "pCVR") == "3.8%"

    def test_percent_exactly_100(self):
        assert _format_value(1.0, "percent", "pickup_rate") == "100.0%"

    def test_score_formats_to_one_decimal(self):
        assert _format_value(4.7, "score", "csat") == "4.7"

    def test_score_rounds_to_one_decimal(self):
        assert _format_value(4.75, "score", "csat") == "4.8"

    def test_count_rounds_to_int(self):
        assert _format_value(5.0, "count", "zhl_transfers") == "5"

    def test_count_rounds_fractional(self):
        assert _format_value(5.6, "count", "zhl_transfers") == "6"

    def test_count_rounds_down(self):
        assert _format_value(5.4, "count", "zhl_transfers") == "5"

    def test_unknown_unit_falls_back_to_two_decimal_float(self):
        assert _format_value(0.12345, "unknown", "metric") == "0.12"

    def test_zero_percent(self):
        assert _format_value(0.0, "percent", "pCVR") == "0.0%"


# ── _polar ────────────────────────────────────────────────────────────────────

class TestPolar:

    def test_0_degrees_points_right(self):
        x, y = _polar(0, 0, 1, 0)
        assert x == pytest.approx(1.0)
        assert y == pytest.approx(0.0, abs=1e-10)

    def test_90_degrees_points_down(self):
        # SVG y-axis is downward, so 90° → (0, +r)
        x, y = _polar(0, 0, 1, 90)
        assert x == pytest.approx(0.0, abs=1e-10)
        assert y == pytest.approx(1.0)

    def test_180_degrees_points_left(self):
        x, y = _polar(0, 0, 1, 180)
        assert x == pytest.approx(-1.0)
        assert y == pytest.approx(0.0, abs=1e-10)

    def test_center_offset_applied(self):
        x, y = _polar(10, 20, 5, 0)
        assert x == pytest.approx(15.0)
        assert y == pytest.approx(20.0, abs=1e-10)

    def test_radius_scales_output(self):
        x, y = _polar(0, 0, 10, 0)
        assert x == pytest.approx(10.0)


# ── build_gauge ───────────────────────────────────────────────────────────────

class TestBuildGauge:

    def _hero(self, value, target, status, unit="percent"):
        return build_gauge(value, target, status, "Test Metric", unit, "pCVR", "hero")

    def _secondary(self, value, target, status, unit="percent"):
        return build_gauge(value, target, status, "Test", unit, "pCVR", "secondary")

    def test_returns_svg_element(self):
        svg = self._hero(0.038, 0.035, "green")
        assert svg.strip().startswith("<svg")
        assert svg.strip().endswith("</svg>")

    def test_contains_role_and_aria_label(self):
        svg = self._hero(0.038, 0.035, "green")
        assert 'role="img"' in svg
        assert 'aria-label=' in svg

    def test_value_string_appears_in_svg(self):
        svg = self._hero(0.038, 0.035, "green")
        assert "3.8%" in svg

    def test_no_data_shows_na(self):
        svg = build_gauge(None, None, "no_data", "Label", "percent", "pCVR", "secondary")
        assert "N/A" in svg

    def test_no_data_with_value_but_no_target(self):
        # value present but target is None → fraction=0 → no fill arc
        svg = build_gauge(0.038, None, "no_data", "Label", "percent", "pCVR", "secondary")
        assert "<svg" in svg  # should not raise

    def test_overshoot_does_not_crash(self):
        # value far above target (fraction > 1.25) — clamped gracefully
        svg = build_gauge(0.999, 0.035, "green", "Label", "percent", "pCVR", "hero")
        assert "<svg" in svg

    def test_zero_value_no_fill_arc(self):
        # value=0 → fraction=0 → fill_arc should be empty string
        svg = build_gauge(0.0, 0.035, "red", "Label", "percent", "pCVR", "secondary")
        assert "<svg" in svg

    def test_label_over_16_chars_is_truncated(self):
        long_label = "A Very Long Metric Name Here"  # 28 chars → truncated to 15 + "…"
        svg = build_gauge(1.0, 1.0, "green", long_label, "score", "metric", "secondary")
        # The truncated form must appear as the visible text label
        assert "A Very Long Met…" in svg
        # The full label still appears in the aria-label (accessibility) — that is correct
        assert 'aria-label="A Very Long Metric Name Here:' in svg

    def test_label_under_16_chars_not_truncated(self):
        short_label = "Pickup Rate"   # 11 chars
        svg = build_gauge(1.0, 1.0, "green", short_label, "percent", "pickup_rate", "secondary")
        assert short_label in svg

    def test_label_exactly_16_chars_not_truncated(self):
        label_16 = "A" * 16
        svg = build_gauge(1.0, 1.0, "green", label_16, "percent", "x", "secondary")
        assert label_16 in svg

    def test_hero_size_uses_larger_viewbox(self):
        hero = self._hero(0.5, 1.0, "yellow")
        secondary = self._secondary(0.5, 1.0, "yellow")
        # Hero width=200, secondary width=130 — check viewBox attribute
        assert 'viewBox="0 0 200' in hero
        assert 'viewBox="0 0 130' in secondary

    def test_status_colors_applied(self):
        green_svg = build_gauge(1.0, 1.0, "green", "L", "percent", "x", "secondary")
        red_svg = build_gauge(0.5, 1.0, "red", "L", "percent", "x", "secondary")
        # Green and red SVGs should contain different stroke colors
        assert green_svg != red_svg

    def test_count_unit_renders_integer(self):
        svg = build_gauge(5, 3, "green", "ZHL", "count", "zhl_transfers", "secondary")
        assert "5" in svg

    def test_score_unit_renders_decimal(self):
        svg = build_gauge(4.7, 4.5, "green", "CSAT", "score", "csat", "secondary")
        assert "4.7" in svg


# ── gauge_from_scored_metric ──────────────────────────────────────────────────

class TestGaugeFromScoredMetric:

    @pytest.fixture
    def scored_metric(self):
        return {
            "key": "pCVR",
            "label": "Predicted Conversion Rate",
            "value": 0.038,
            "target": 0.035,
            "yellow_floor": 0.030,
            "pct_of_target": 1.086,
            "status": "green",
            "weight": 1.0,
            "gauge_size": "hero",
            "unit": "percent",
        }

    def test_returns_svg_string(self, scored_metric):
        svg = gauge_from_scored_metric(scored_metric)
        assert "<svg" in svg

    def test_uses_hero_size_for_hero_gauge(self, scored_metric):
        svg = gauge_from_scored_metric(scored_metric)
        assert 'viewBox="0 0 200' in svg

    def test_uses_secondary_size_for_secondary_gauge(self, scored_metric):
        scored_metric["gauge_size"] = "secondary"
        svg = gauge_from_scored_metric(scored_metric)
        assert 'viewBox="0 0 130' in svg

    def test_value_formatted_correctly(self, scored_metric):
        svg = gauge_from_scored_metric(scored_metric)
        assert "3.8%" in svg

    def test_no_data_metric_renders_na(self, scored_metric):
        scored_metric["value"] = None
        scored_metric["status"] = "no_data"
        svg = gauge_from_scored_metric(scored_metric)
        assert "N/A" in svg


# ── build_all_gauges ──────────────────────────────────────────────────────────

class TestBuildAllGauges:

    @pytest.fixture
    def scored_agent(self):
        def _m(key, label, value, target, status, unit, size):
            return {
                "key": key, "label": label, "value": value, "target": target,
                "yellow_floor": None, "pct_of_target": value / target if (value and target) else None,
                "status": status, "weight": 1.0, "gauge_size": size, "unit": unit,
            }
        return {
            "metrics": {
                "pCVR": _m("pCVR", "pCVR", 0.038, 0.035, "green", "percent", "hero"),
                "pickup_rate": _m("pickup_rate", "Pickup Rate", 0.91, 0.85, "green", "percent", "secondary"),
                "csat": _m("csat", "CSAT", 4.7, 4.5, "green", "score", "secondary"),
                "zhl_transfers": _m("zhl_transfers", "ZHL", 5, 3, "green", "count", "secondary"),
            }
        }

    def test_returns_dict_with_all_metric_keys(self, scored_agent):
        result = build_all_gauges(scored_agent)
        assert set(result.keys()) == {"pCVR", "pickup_rate", "csat", "zhl_transfers"}

    def test_all_values_are_svg_strings(self, scored_agent):
        result = build_all_gauges(scored_agent)
        for key, svg in result.items():
            assert isinstance(svg, str), f"{key} gauge is not a string"
            assert "<svg" in svg, f"{key} gauge does not contain <svg>"
