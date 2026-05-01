"""Tests for src/gauges.py — value formatting and SVG generation."""

import xml.etree.ElementTree as ET

import pytest

from src.gauges import (
    SIZES,
    _format_value,
    build_all_gauges,
    build_gauge,
    gauge_from_scored_metric,
)


# ── _format_value ─────────────────────────────────────────────────────────────

class TestFormatValue:
    def test_none_renders_na(self):
        assert _format_value(None, "percent", "pCVR") == "N/A"

    def test_percent_unit(self):
        assert _format_value(0.038, "percent", "pCVR") == "3.8%"
        assert _format_value(1.0, "percent", "pickup_rate") == "100.0%"

    def test_score_unit_one_decimal(self):
        assert _format_value(4.7, "score", "csat") == "4.7"
        assert _format_value(4.0, "score", "csat") == "4.0"

    def test_count_unit_rounds_to_int(self):
        assert _format_value(3.0, "count", "zhl_transfers") == "3"
        assert _format_value(3.4, "count", "zhl_transfers") == "3"
        assert _format_value(3.6, "count", "zhl_transfers") == "4"

    def test_unknown_unit_falls_back_to_two_decimals(self):
        assert _format_value(1.234, "weird_unit", "x") == "1.23"


# ── build_gauge ───────────────────────────────────────────────────────────────

class TestBuildGauge:
    def _parse(self, svg: str) -> ET.Element:
        return ET.fromstring(svg)

    def test_returns_well_formed_svg(self):
        svg = build_gauge(0.04, 0.035, "green", "pCVR", "percent", "pCVR", size="hero")
        root = self._parse(svg)
        assert root.tag.endswith("svg")

    def test_aria_label_contains_metric_name_and_value(self):
        svg = build_gauge(0.04, 0.035, "green", "pCVR", "percent", "pCVR", size="hero")
        root = self._parse(svg)
        assert "pCVR" in root.get("aria-label")
        assert "4.0%" in root.get("aria-label")

    def test_hero_dimensions(self):
        svg = build_gauge(0.04, 0.035, "green", "pCVR", "percent", "pCVR", size="hero")
        root = self._parse(svg)
        assert root.get("width") == str(SIZES["hero"]["width"])
        assert root.get("height") == str(SIZES["hero"]["height"])

    def test_secondary_dimensions(self):
        svg = build_gauge(4.7, 4.5, "green", "CSAT", "score", "csat", size="secondary")
        root = self._parse(svg)
        assert root.get("width") == str(SIZES["secondary"]["width"])
        assert root.get("height") == str(SIZES["secondary"]["height"])

    def test_unknown_size_falls_back_to_secondary(self):
        svg = build_gauge(4.7, 4.5, "green", "CSAT", "score", "csat", size="enormous")
        root = self._parse(svg)
        assert root.get("width") == str(SIZES["secondary"]["width"])

    def test_no_data_skips_fill_arc_and_uses_text_color(self):
        # When status is no_data, fraction is 0 (no fill arc drawn) and the
        # center value text uses TEXT_COLOR instead of a status color.
        from config.settings import BRAND
        svg = build_gauge(None, 0.035, "no_data", "pCVR", "percent", "pCVR")
        root = self._parse(svg)
        ns = "{http://www.w3.org/2000/svg}"
        paths = root.findall(f"{ns}path")
        assert len(paths) == 1  # only the background track, no fill arc
        # Value text should be in body text color, not a status color.
        assert BRAND["color_text"] in svg

    def test_value_none_renders_na_label(self):
        svg = build_gauge(None, 0.035, "no_data", "pCVR", "percent", "pCVR")
        assert "N/A" in svg

    def test_overshoot_does_not_explode(self):
        # value way above target — internal clamp at 1.25× allows slight overshoot
        # but should not produce malformed SVG.
        svg = build_gauge(0.50, 0.035, "green", "pCVR", "percent", "pCVR", size="hero")
        self._parse(svg)  # parses cleanly

    def test_zero_target_skips_fill_arc(self):
        # When target is 0, fraction is 0 → no <path> for fill arc, only the track.
        svg = build_gauge(0.04, 0, "no_data", "pCVR", "percent", "pCVR")
        root = self._parse(svg)
        ns = "{http://www.w3.org/2000/svg}"
        paths = root.findall(f"{ns}path")
        assert len(paths) == 1  # only the background track

    def test_nonzero_target_has_two_arcs(self):
        svg = build_gauge(0.04, 0.035, "green", "pCVR", "percent", "pCVR")
        root = self._parse(svg)
        ns = "{http://www.w3.org/2000/svg}"
        paths = root.findall(f"{ns}path")
        assert len(paths) == 2  # track + fill

    def test_long_label_is_truncated(self):
        long = "Predicted Conversion Rate Long Suffix"
        svg = build_gauge(0.04, 0.035, "green", long, "percent", "pCVR")
        # The truncated form (first 15 chars + ellipsis) appears as the
        # gauge label below the arc; the full label still appears in
        # aria-label for accessibility.
        assert long[:15] + "…" in svg

    def test_short_label_is_not_truncated(self):
        svg = build_gauge(0.04, 0.035, "green", "pCVR", "percent", "pCVR")
        assert "…" not in svg


# ── gauge_from_scored_metric / build_all_gauges ──────────────────────────────

class TestGaugeFromScoredMetric:
    def test_picks_size_from_scored_metric(self):
        scored = {
            "value": 0.04, "target": 0.035, "status": "green",
            "label": "pCVR", "unit": "percent", "key": "pCVR",
            "gauge_size": "hero",
        }
        svg = gauge_from_scored_metric(scored)
        root = ET.fromstring(svg)
        assert root.get("width") == str(SIZES["hero"]["width"])

    def test_defaults_to_secondary_when_size_missing(self):
        scored = {
            "value": 4.7, "target": 4.5, "status": "green",
            "label": "CSAT", "unit": "score", "key": "csat",
        }
        svg = gauge_from_scored_metric(scored)
        root = ET.fromstring(svg)
        assert root.get("width") == str(SIZES["secondary"]["width"])


class TestBuildAllGauges:
    def test_returns_one_svg_per_metric(self, agent_raw, thresholds_full):
        from src.metrics import score_agent
        scored = score_agent(agent_raw, thresholds_full)
        gauges = build_all_gauges(scored)
        assert set(gauges.keys()) == {"pCVR", "pickup_rate", "csat", "zhl_transfers"}
        for svg in gauges.values():
            ET.fromstring(svg)
