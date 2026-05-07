"""
Admin-facing CSV / JSON ingest.

Replaces autonomous FUB pulls when the admin prefers to upload data manually.
The metric set is dynamic — whatever metrics are currently defined in
config/thresholds.json must be present (as columns) in the upload.

Required columns (any order):
    agent_id, name, email, period, <metric_key_1>, <metric_key_2>, …

Period accepts "April 2026", "2026-04", or "2026-04-15".
"""

from __future__ import annotations

import csv
import json
import logging
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from src.metrics import load_thresholds, metric_keys

log = logging.getLogger(__name__)

REQUIRED_FIXED_COLUMNS = ("agent_id", "name", "email", "period")


# ── Public API ────────────────────────────────────────────────────────────────


def parse_file(path: str | Path) -> list[dict]:
    """
    Parse a CSV or JSON file at `path` into a list of normalized agent records.

    Format is detected by file extension. Validates that all required metric
    columns from the current thresholds.json are present.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Upload file not found: {p}")

    suffix = p.suffix.lower()
    if suffix == ".csv":
        return _parse_csv(p)
    if suffix == ".json":
        return _parse_json(p)
    raise ValueError(f"Unsupported file type: {suffix} (use .csv or .json)")


# ── CSV ───────────────────────────────────────────────────────────────────────


def _parse_csv(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise ValueError(f"{path} is empty or missing a header row")
        _validate_columns(reader.fieldnames)

        rows: list[dict] = []
        for i, raw_row in enumerate(reader, start=2):
            try:
                rows.append(_normalize_row(raw_row))
            except (TypeError, ValueError) as e:
                raise ValueError(f"{path}: row {i}: {e}") from e

    log.info("Parsed %d rows from %s", len(rows), path)
    return rows


def _parse_json(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        data = data.get("agents") or data.get("rows") or [data]
    if not isinstance(data, list):
        raise ValueError(f"{path}: top-level JSON must be a list or wrap rows in 'agents'/'rows'")

    if data:
        _validate_columns(data[0].keys())

    return [_normalize_row(row) for row in data]


# ── Validation + normalization ────────────────────────────────────────────────


def _validate_columns(columns: Iterable[str]) -> None:
    cols = {c.strip() for c in columns}
    missing_fixed = [c for c in REQUIRED_FIXED_COLUMNS if c not in cols]
    if missing_fixed:
        raise ValueError(
            f"Upload missing required columns: {', '.join(missing_fixed)}. "
            f"Required: {', '.join(REQUIRED_FIXED_COLUMNS)} + current metric keys."
        )

    expected_metrics = metric_keys(load_thresholds())
    missing_metrics = [m for m in expected_metrics if m not in cols]
    if missing_metrics:
        raise ValueError(
            f"Upload missing metric columns: {', '.join(missing_metrics)}.\n"
            f"Current metric registry: {', '.join(expected_metrics)}.\n"
            "Run `python main.py --mode research` if Zillow's program changed."
        )

    unknown = [
        c
        for c in cols
        if c not in REQUIRED_FIXED_COLUMNS and c not in expected_metrics and not c.startswith("_")
    ]
    if unknown:
        log.warning("Ignoring unknown columns: %s", ", ".join(sorted(unknown)))


def _normalize_row(row: dict) -> dict:
    expected_metrics = metric_keys(load_thresholds())

    record: dict = {
        "agent_id": str(row["agent_id"]).strip(),
        "name": str(row["name"]).strip(),
        "email": str(row["email"]).strip(),
        "period": _normalize_period_label(str(row["period"]).strip()),
        "_raw": dict(row),
    }
    for k in expected_metrics:
        record[k] = _coerce_number(row.get(k))
    return record


def _coerce_number(value) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().rstrip("%")
    if not s:
        return None
    try:
        return float(s)
    except ValueError as e:
        raise ValueError(f"Cannot parse numeric value: {value!r}") from e


def _normalize_period_label(period: str) -> str:
    """Display label like 'April 2026', regardless of input format."""
    try:
        return datetime.strptime(period[:7], "%Y-%m").strftime("%B %Y")
    except ValueError:
        pass
    for fmt in ("%B %Y", "%b %Y"):
        try:
            return datetime.strptime(period, fmt).strftime("%B %Y")
        except ValueError:
            continue
    return period
