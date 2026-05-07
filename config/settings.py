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
# Roster lives in config/agents.csv (gitignored). Columns:
#   name, email, fub_agent_id, active
# Falls back to an empty list when the file is absent.
ROSTER_FILE = CONFIG_DIR / "agents.csv"


def _load_agents_from_csv() -> list[dict]:
    if not ROSTER_FILE.exists():
        return []
    import csv
    out: list[dict] = []
    with ROSTER_FILE.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            active = (row.get("active") or "1").strip().lower()
            if active in ("0", "false", "no"):
                continue
            out.append({
                "name": (row.get("name") or "").strip(),
                "email": (row.get("email") or "").strip().lower(),
                "fub_agent_id": (row.get("fub_agent_id") or "").strip() or None,
            })
    return [a for a in out if a["name"] and a["email"]]


AGENTS = _load_agents_from_csv()

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
    "color_primary":    "#167272",   # Clear Water — deep teal
    "color_secondary":  "#5DC8BE",   # Pearl Aqua — lighter teal/aqua
    "color_accent":     "#D4A96A",   # Sandy Shore mid-tone (buttons/highlights)
    "color_bg":         "#F5EDE0",   # Sandy Shore light — page/email background
    "color_text":       "#1A1A1A",   # Near-black body text (high contrast on cream)
    # Gauge status colors — functional traffic-light, harmonized with teal palette
    "color_green":      "#2ECC71",   # On-track / Preferred
    "color_yellow":     "#F0A500",   # At-risk (warm amber, avoids clash with teal)
    "color_red":        "#E05C4B",   # Needs improvement (muted red, brand-safe)
    # ── Typography ───────────────────────────────────────────────────────────────
    # Collier     = primary typeface (headers, titles, slide titles)
    # Dax Pro     = secondary typeface (sub-headers Medium, body Light, captions)
    # Both are licensed fonts. Email fallbacks applied for client compatibility.
    "font_heading":     "'Collier', Georgia, 'Times New Roman', serif",
    "font_body":        "'Dax Pro', 'Helvetica Neue', Arial, sans-serif",
    "font_heading_weight_title":    "400",   # Collier Regular for main titles
    "font_body_weight_subhead":     "500",   # Dax Pro Medium for sub-headers
    "font_body_weight_body":        "300",   # Dax Pro Light for body text
    # Footer sign-off copy (Dolphins, Not Sharks ethos)
    "footer_message":   "Keep showing up with integrity — that's what sets great agents apart.",
}

# ── Reporting Period ──────────────────────────────────────────────────────────
# The system auto-detects the prior calendar month at runtime.
# Override here only if you need to rerun a specific period.
OVERRIDE_REPORT_MONTH = None   # e.g. "2026-03" or None for auto

# ── Dashboard / web app ───────────────────────────────────────────────────────
DATA_DIR = BASE_DIR / "data"
DATABASE_PATH = Path(os.environ.get("METRICS_DB_PATH", DATA_DIR / "metrics.db"))

# Public URL the magic-link emails point at. Set this on the Pi to the
# Cloudflare-Tunnel hostname (e.g. https://metrics.anchorgroup.com).
WEB_BASE_URL = os.environ.get("WEB_BASE_URL", "http://localhost:8081").rstrip("/")

# Used to sign session cookies and magic-link tokens. MUST be set in production.
# A random default lets tests and `--mock` runs work without env config.
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-not-for-prod-change-me")

MAGIC_LINK_TTL_MINUTES = int(os.environ.get("MAGIC_LINK_TTL_MINUTES", "15"))
SESSION_TTL_DAYS = int(os.environ.get("SESSION_TTL_DAYS", "30"))
SESSION_COOKIE_NAME = "anchor_session"

# How many months of history the dashboard shows in trend charts.
DASHBOARD_TREND_MONTHS = 6

# When SMTP credentials are missing AND this flag is on, the magic-link URL is
# written to the server log instead of emailed. Off by default so production
# can't accidentally short-circuit auth — only turn this on for local dev.
DEV_LOG_MAGIC_LINK = os.environ.get("DEV_LOG_MAGIC_LINK", "").lower() in (
    "1", "true", "yes"
)
