"""
Shared pytest fixtures.

Ensures the project root is on sys.path so `from src.X` and `from config.X`
imports resolve when pytest is invoked from any directory.
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


import pytest


@pytest.fixture
def thresholds_full() -> dict:
    """Fully-populated thresholds matching the canonical METRIC_KEYS."""
    return {
        "metrics": {
            "pCVR": {
                "target": 0.035, "yellow_floor": 0.030, "weight": 2.0,
                "gauge_size": "hero", "label": "pCVR", "unit": "percent",
            },
            "pickup_rate": {
                "target": 0.85, "yellow_floor": 0.75, "weight": 1.0,
                "gauge_size": "secondary", "label": "Pickup Rate", "unit": "percent",
            },
            "csat": {
                "target": 4.5, "yellow_floor": 4.0, "weight": 1.0,
                "gauge_size": "secondary", "label": "CSAT", "unit": "score",
            },
            "zhl_transfers": {
                "target": 3, "yellow_floor": 2, "weight": 1.0,
                "gauge_size": "secondary", "label": "ZHL Transfers", "unit": "count",
            },
        }
    }


@pytest.fixture
def agent_raw() -> dict:
    """Raw agent dict shaped like fub_client output, all metrics on-target."""
    return {
        "agent_id": "test-001",
        "name": "Test Agent",
        "email": "test@example.com",
        "period": "March 2026",
        "start_date": "2026-03-01",
        "end_date": "2026-03-31",
        "pCVR": 0.040,
        "pickup_rate": 0.90,
        "csat": 4.7,
        "zhl_transfers": 4,
        "_raw": {},
    }


@pytest.fixture
def tmp_db(tmp_path):
    """Isolated SQLite database for storage / webapp tests."""
    from src import storage
    db_path = tmp_path / "metrics.db"
    storage.init_schema(db_path=db_path)
    return db_path
