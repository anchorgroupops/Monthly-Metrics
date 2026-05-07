"""
Review Mode: writes all agent emails and the team deck to output/review/
so you can inspect everything locally before n8n sends them.

Usage:
    python main.py --mode review [--mock]

Then open in browser:
    python -m http.server 8080 --directory output/review
    → http://localhost:8080
"""

import logging
import re
from pathlib import Path

from config.settings import BRAND, REVIEW_DIR
from src.deck_builder import build_deck
from src.email_builder import build_all_emails

log = logging.getLogger(__name__)

# Status → (bg_color, text_color, icon)
STATUS_STYLES = {
    "Preferred": ("#2ECC71", "#FFFFFF", "✓"),
    "At Risk": ("#F39C12", "#1C2B3A", "⚠"),
    "Needs Improvement": ("#E74C3C", "#FFFFFF", "↑"),
    "No Data": ("#CCCCCC", "#1C2B3A", "?"),
}


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def run_review(scored_agents: list[dict]) -> None:
    """
    Generate all review files.

    1. Per-agent email HTML → output/review/{slug}.html
    2. Team deck           → output/review/deck.html
    3. Index page          → output/review/index.html
    """
    REVIEW_DIR.mkdir(parents=True, exist_ok=True)

    # ── 1. Agent emails ───────────────────────────────────────────────────────
    built_emails = build_all_emails(scored_agents)
    for item in built_emails:
        slug = _slugify(item["agent"]["name"])
        out_path = REVIEW_DIR / f"{slug}.html"
        out_path.write_text(item["html"], encoding="utf-8")
        log.info(
            "  Wrote %s",
            out_path.relative_to(Path.cwd()) if Path.cwd() in out_path.parents else out_path,
        )

    # ── 2. Team deck ──────────────────────────────────────────────────────────
    deck_html = build_deck(scored_agents)
    deck_path = REVIEW_DIR / "deck.html"
    deck_path.write_text(deck_html, encoding="utf-8")
    log.info("  Wrote deck.html")

    # ── 3. Index page ─────────────────────────────────────────────────────────
    index_html = _build_index(built_emails, scored_agents)
    index_path = REVIEW_DIR / "index.html"
    index_path.write_text(index_html, encoding="utf-8")
    log.info("  Wrote index.html")

    print(f"\n  Review files written to: {REVIEW_DIR}")
    print(f"  {len(built_emails)} agent email(s)  +  1 team deck  +  index")
    print("\n  To preview, run:")
    print(f"    python -m http.server 8080 --directory {REVIEW_DIR}")
    print("    → http://localhost:8080\n")


def _build_index(built_emails: list[dict], scored_agents: list[dict]) -> str:
    """Build the review/index.html overview page."""
    period = scored_agents[0]["period"] if scored_agents else ""

    cards = []
    for item in built_emails:
        agent = item["agent"]
        slug = _slugify(agent["name"])
        status = agent["overall_status"]
        bg, txt, icon = STATUS_STYLES.get(status, STATUS_STYLES["No Data"])

        pCVR_metric = agent["metrics"].get("pCVR", {})
        pCVR_val = pCVR_metric.get("value")
        pCVR_display = f"{pCVR_val * 100:.1f}%" if pCVR_val is not None else "N/A"

        cards.append(f"""
    <div class="card">
      <div class="card-header">
        <span class="agent-name">{agent["name"]}</span>
        <span class="badge" style="background:{bg};color:{txt};">{icon} {status}</span>
      </div>
      <div class="card-body">
        <span class="pcvr-label">pCVR</span>
        <span class="pcvr-value">{pCVR_display}</span>
      </div>
      <div class="card-footer">
        <a href="{slug}.html" target="_blank" class="btn-preview">View Email →</a>
      </div>
    </div>""")

    cards_html = "\n".join(cards)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1.0">
  <title>Review Mode — {period} — The Anchor Group</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: {BRAND["font_body"]};
      background: {BRAND["color_bg"]};
      color: {BRAND["color_text"]};
      padding: 32px 24px;
    }}
    .page-header {{
      background: {BRAND["color_primary"]};
      color: white;
      padding: 20px 28px;
      border-radius: 10px;
      margin-bottom: 28px;
    }}
    .page-header h1 {{
      font-family: {BRAND["font_heading"]};
      font-size: 20px;
      font-weight: 700;
      margin-bottom: 4px;
    }}
    .page-header p {{ font-size: 13px; opacity: 0.75; }}
    .deck-link {{
      display: inline-block;
      background: {BRAND["color_secondary"]};
      color: white;
      text-decoration: none;
      padding: 10px 20px;
      border-radius: 8px;
      font-size: 14px;
      font-weight: 600;
      margin-bottom: 28px;
    }}
    .deck-link:hover {{ opacity: 0.85; }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
      gap: 16px;
    }}
    .card {{
      background: white;
      border-radius: 10px;
      box-shadow: 0 2px 8px rgba(0,0,0,0.07);
      overflow: hidden;
    }}
    .card-header {{
      background: {BRAND["color_bg"]};
      padding: 14px 16px 10px;
      border-bottom: 1px solid #E8ECEF;
      display: flex;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 6px;
    }}
    .agent-name {{
      font-size: 15px;
      font-weight: 700;
      color: {BRAND["color_primary"]};
    }}
    .badge {{
      font-size: 11px;
      font-weight: 700;
      padding: 3px 10px;
      border-radius: 12px;
      letter-spacing: 0.3px;
    }}
    .card-body {{
      padding: 16px;
      display: flex;
      align-items: center;
      justify-content: space-between;
    }}
    .pcvr-label {{
      font-size: 12px;
      opacity: 0.6;
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }}
    .pcvr-value {{
      font-size: 22px;
      font-weight: 700;
      color: {BRAND["color_primary"]};
    }}
    .card-footer {{
      padding: 0 16px 16px;
    }}
    .btn-preview {{
      display: block;
      text-align: center;
      background: {BRAND["color_primary"]};
      color: white;
      text-decoration: none;
      padding: 9px;
      border-radius: 7px;
      font-size: 13px;
      font-weight: 600;
    }}
    .btn-preview:hover {{ opacity: 0.85; }}
    .footer-note {{
      margin-top: 32px;
      font-size: 12px;
      opacity: 0.5;
      text-align: center;
    }}
  </style>
</head>
<body>
  <div class="page-header">
    <h1>Review Mode — {period}</h1>
    <p>The Anchor Group · Zillow Preferred Performance Reports · Local Preview</p>
  </div>

  <a href="deck.html" target="_blank" class="deck-link">
    &#9654; Open Team Meeting Deck
  </a>

  <div class="grid">
{cards_html}
  </div>

  <p class="footer-note">
    This is a local preview only. Approve and run
    <code>python main.py --mode send</code> to deliver emails.
  </p>
</body>
</html>
"""
