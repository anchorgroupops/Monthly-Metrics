"""
Central configuration for the Anchor Group Monthly Metrics system.
Credentials and secrets are loaded from environment variables.
Never commit .env or secrets to version control.
"""

import os
from pathlib import Path

# ── Paths ────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"
TEMPLATES_DIR = BASE_DIR / "templates"
OUTPUT_DIR = BASE_DIR / "output"
REVIEW_DIR = OUTPUT_DIR / "review"
THRESHOLDS_FILE = CONFIG_DIR / "thresholds.json"

# ── Follow Up Boss API ────────────────────────────────────────────────────────
FUB_API_KEY = os.environ.get("FUB_API_KEY", "")
FUB_BASE_URL = "https://api.followupboss.com/v1"
FUB_TIMEOUT_SECONDS = 30
FUB_MAX_RETRIES = 3

# ── Claude API (used by threshold_researcher.py) ──────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
RESEARCH_MODEL = "claude-sonnet-4-6"          # Fast, capable for web research
RESEARCH_MAX_TOKENS = 1024

# ── Email / SMTP ──────────────────────────────────────────────────────────────
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
EMAIL_FROM_NAME = "The Anchor Group"
EMAIL_FROM_ADDRESS = os.environ.get("EMAIL_FROM", SMTP_USER)
EMAIL_SUBJECT_TEMPLATE = "Your {month} Performance Report — The Anchor Group"

# ── Agent Roster ──────────────────────────────────────────────────────────────
# Each entry: fub_agent_id must match the ID in Follow Up Boss.
# Set fub_agent_id to None to skip API fetch and use mock data (for testing).
AGENTS = [
    # {
    #     "name": "Jane Smith",
    #     "email": "jane@anchorgroup.com",
    #     "fub_agent_id": "12345",
    # },
    # Add your agents here. This list is intentionally left empty so you can
    # populate it without risk of committing real agent data.
]

# ── Brand (placeholder — will be updated from brand template) ─────────────────
BRAND = {
    # Colors — replace hex values once brand template is uploaded
    "color_primary":    "#1A3A5C",   # Deep ocean navy (placeholder)
    "color_secondary":  "#2E86AB",   # Dolphin blue (placeholder)
    "color_accent":     "#F4A261",   # Warm sand (placeholder)
    "color_bg":         "#FAFBFC",   # Off-white background
    "color_text":       "#1C2B3A",   # Dark text
    "color_green":      "#2ECC71",   # Gauge: on-track
    "color_yellow":     "#F39C12",   # Gauge: at-risk
    "color_red":        "#E74C3C",   # Gauge: needs improvement
    # Typography — replace once brand template is uploaded
    "font_heading":     "Georgia, 'Times New Roman', serif",
    "font_body":        "'Helvetica Neue', Arial, sans-serif",
    # Footer sign-off copy (Dolphins, Not Sharks ethos)
    "footer_message":   "Keep showing up with integrity — that's what sets great agents apart.",
}

# ── Reporting Period ──────────────────────────────────────────────────────────
# The system auto-detects the prior calendar month at runtime.
# Override here only if you need to rerun a specific period.
OVERRIDE_REPORT_MONTH = None   # e.g. "2026-03" or None for auto
