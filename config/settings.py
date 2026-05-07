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
RESEARCH_MODEL = "claude-sonnet-4-6"  # Fast, capable for web research
RESEARCH_MAX_TOKENS = 1024

# ── Email / SMTP ──────────────────────────────────────────────────────────────
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
EMAIL_FROM_NAME = "The Anchor Group"
EMAIL_FROM_ADDRESS = os.environ.get("EMAIL_FROM", SMTP_USER)
EMAIL_SUBJECT_TEMPLATE = "Your {month} Performance Report — The Anchor Group"
# Where failure alerts go. Falls back to the from-address if unset.
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", EMAIL_FROM_ADDRESS)

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
    # ── Brand Colors (The Anchor Team — Color & Typography Deck B) ───────────────
    # Clear Water  = deep teal, primary brand color (30% usage per 60-30-10 rule)
    # Pearl Aqua   = lighter teal, accent color (10% usage)
    # Sandy Shore  = warm cream, dominant background (60% usage)
    #
    # NOTE: Exact hex codes estimated from brand deck screenshots.
    # If you have the source hex values from AgentFire, update these three lines:
    "color_primary": "#167272",  # Clear Water — deep teal
    "color_secondary": "#5DC8BE",  # Pearl Aqua — lighter teal/aqua
    "color_accent": "#D4A96A",  # Sandy Shore mid-tone (buttons/highlights)
    "color_bg": "#F5EDE0",  # Sandy Shore light — page/email background
    "color_text": "#1A1A1A",  # Near-black body text (high contrast on cream)
    # Gauge status colors — functional traffic-light, harmonized with teal palette
    "color_green": "#2ECC71",  # On-track / Preferred
    "color_yellow": "#F0A500",  # At-risk (warm amber, avoids clash with teal)
    "color_red": "#E05C4B",  # Needs improvement (muted red, brand-safe)
    # ── Typography ───────────────────────────────────────────────────────────────
    # Collier     = primary typeface (headers, titles, slide titles)
    # Dax Pro     = secondary typeface (sub-headers Medium, body Light, captions)
    # Both are licensed fonts. Email fallbacks applied for client compatibility.
    "font_heading": "'Collier', Georgia, 'Times New Roman', serif",
    "font_body": "'Dax Pro', 'Helvetica Neue', Arial, sans-serif",
    "font_heading_weight_title": "400",  # Collier Regular for main titles
    "font_body_weight_subhead": "500",  # Dax Pro Medium for sub-headers
    "font_body_weight_body": "300",  # Dax Pro Light for body text
    # Footer sign-off copy (Dolphins, Not Sharks ethos)
    "footer_message": "Keep showing up with integrity — that's what sets great agents apart.",
}

# ── Reporting Period ──────────────────────────────────────────────────────────
# The system auto-detects the prior calendar month at runtime.
# Override here only if you need to rerun a specific period.
OVERRIDE_REPORT_MONTH = None  # e.g. "2026-03" or None for auto
