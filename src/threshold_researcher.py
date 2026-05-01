"""
AI-driven Zillow Preferred threshold researcher.

Called at the start of each monthly run to web-search the current Zillow
Preferred 2026 benchmark values, then writes them to config/thresholds.json.

Uses the Claude API with web_search tool access so thresholds stay current
even as Zillow updates its program requirements.
"""

import json
import logging
from datetime import date
from typing import Optional

import anthropic

from config.settings import ANTHROPIC_API_KEY, RESEARCH_MODEL, RESEARCH_MAX_TOKENS, THRESHOLDS_FILE

log = logging.getLogger(__name__)

RESEARCH_PROMPT = """
You are a real estate industry researcher. Your task is to find the current
Zillow Preferred Agent program benchmark thresholds for {year}.

Please research and return the CURRENT required performance thresholds for
Zillow Preferred agents across these four metrics:

1. **pCVR (Predicted Conversion Rate)** — The minimum pCVR score required to
   maintain or qualify for Zillow Preferred status. This is typically expressed
   as a decimal (e.g., 0.035 = 3.5%).

2. **Pickup Rate** — The minimum percentage of inbound Zillow calls an agent
   must answer (within the required response window). Expressed as a decimal.

3. **CSAT (Customer Satisfaction Score)** — The minimum average satisfaction
   score required. Note the scale used (e.g., 1–5) and the minimum target.

4. **ZHL Transfers (Zillow Home Loans)** — The minimum number of Zillow Home
   Loans transfer referrals required per month (or per quarter if applicable).

For each metric also provide:
- The "yellow floor" — a threshold below the target where agents are considered
  "at risk" but not yet failing (typically ~85–90% of target).
- The unit (percent, score, count).

Return your findings as a JSON object in this exact format:
{{
  "source_notes": "Brief description of where you found this information",
  "metrics": {{
    "pCVR": {{
      "target": <float>,
      "yellow_floor": <float>,
      "unit": "percent"
    }},
    "pickup_rate": {{
      "target": <float>,
      "yellow_floor": <float>,
      "unit": "percent"
    }},
    "csat": {{
      "target": <float>,
      "yellow_floor": <float>,
      "unit": "score"
    }},
    "zhl_transfers": {{
      "target": <float or int>,
      "yellow_floor": <float or int>,
      "unit": "count"
    }}
  }}
}}

If you cannot find a specific value with confidence, use null for that field
and note it in source_notes. Do not guess — accuracy matters here.
Only return the JSON object, no other text.
""".strip()


def research_thresholds(year: Optional[str] = None) -> dict:
    """
    Use Claude with web search to find current Zillow Preferred thresholds.

    Returns the parsed research results dict.
    Raises on API errors or unparseable responses.
    """
    if not ANTHROPIC_API_KEY:
        raise EnvironmentError(
            "ANTHROPIC_API_KEY is not set. Export it before running:\n"
            "  export ANTHROPIC_API_KEY=your_key_here"
        )

    target_year = year or str(date.today().year)
    prompt = RESEARCH_PROMPT.format(year=target_year)

    log.info("Researching Zillow Preferred %s thresholds via Claude API…", target_year)

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # Use web_search tool so Claude can look up current program requirements
    response = client.messages.create(
        model=RESEARCH_MODEL,
        max_tokens=RESEARCH_MAX_TOKENS,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{"role": "user", "content": prompt}],
    )

    # Extract the text content block from the response
    result_text = None
    for block in response.content:
        if hasattr(block, "text"):
            result_text = block.text.strip()
            break

    if not result_text:
        raise ValueError("Claude returned no text content in threshold research response.")

    # Strip markdown code fences if present
    if result_text.startswith("```"):
        lines = result_text.splitlines()
        result_text = "\n".join(
            line for line in lines if not line.startswith("```")
        ).strip()

    try:
        parsed = json.loads(result_text)
    except json.JSONDecodeError as e:
        log.error("Could not parse Claude response as JSON:\n%s", result_text)
        raise ValueError(f"Claude threshold research returned non-JSON: {e}") from e

    log.info("Research complete. Source: %s", parsed.get("source_notes", "N/A"))
    return parsed


def update_thresholds_file(research_results: dict, year: Optional[str] = None) -> None:
    """
    Merge research results into thresholds.json, preserving static fields
    (weights, gauge_size, labels, descriptions).
    """
    # Load existing file to preserve static metadata
    if THRESHOLDS_FILE.exists():
        with open(THRESHOLDS_FILE) as f:
            existing = json.load(f)
    else:
        existing = {"metrics": {}}

    target_year = year or str(date.today().year)
    researched_metrics = research_results.get("metrics", {})

    for key, values in researched_metrics.items():
        if key not in existing["metrics"]:
            existing["metrics"][key] = {}
        existing["metrics"][key]["target"] = values.get("target")
        existing["metrics"][key]["yellow_floor"] = values.get("yellow_floor")
        existing["metrics"][key]["unit"] = values.get("unit", existing["metrics"][key].get("unit"))

    existing["last_updated"] = date.today().isoformat()
    existing["source"] = research_results.get("source_notes", "AI research")
    existing["program_year"] = target_year

    with open(THRESHOLDS_FILE, "w") as f:
        json.dump(existing, f, indent=2)

    log.info("thresholds.json updated at %s", THRESHOLDS_FILE)


def run_research(year: Optional[str] = None) -> None:
    """Top-level function called by main.py --mode research."""
    results = research_thresholds(year)
    update_thresholds_file(results, year)
    print(f"\nThresholds updated successfully.")
    print(f"Source: {results.get('source_notes', 'N/A')}")
    print(f"File:   {THRESHOLDS_FILE}\n")

    # Print a summary table
    metrics = results.get("metrics", {})
    print(f"{'Metric':<20} {'Target':>10} {'Yellow Floor':>14} {'Unit':>8}")
    print("-" * 56)
    for key in ["pCVR", "pickup_rate", "csat", "zhl_transfers"]:
        m = metrics.get(key, {})
        target = m.get("target", "N/A")
        floor  = m.get("yellow_floor", "N/A")
        unit   = m.get("unit", "")
        print(f"{key:<20} {str(target):>10} {str(floor):>14} {unit:>8}")
    print()


# Allow running standalone for debugging
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    year_arg = sys.argv[1] if len(sys.argv) > 1 else None
    run_research(year_arg)
