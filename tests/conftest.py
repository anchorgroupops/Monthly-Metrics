"""
Shared fixtures for the Monthly Metrics test suite.
"""

import json
import pytest


@pytest.fixture
def sample_thresholds():
    """Fully populated thresholds dict matching the real thresholds.json schema."""
    return {
        "metrics": {
            "pCVR": {
                "label": "Predicted Conversion Rate",
                "target": 0.035,
                "yellow_floor": 0.030,
                "unit": "percent",
                "weight": 1.0,
                "gauge_size": "hero",
            },
            "pickup_rate": {
                "label": "Pickup Rate",
                "target": 0.85,
                "yellow_floor": 0.75,
                "unit": "percent",
                "weight": 0.65,
                "gauge_size": "secondary",
            },
            "csat": {
                "label": "Customer Satisfaction",
                "target": 4.5,
                "yellow_floor": 4.0,
                "unit": "score",
                "weight": 0.65,
                "gauge_size": "secondary",
            },
            "zhl_transfers": {
                "label": "ZHL Transfers",
                "target": 3,
                "yellow_floor": 2,
                "unit": "count",
                "weight": 0.65,
                "gauge_size": "secondary",
            },
        }
    }


@pytest.fixture
def thresholds_file(tmp_path, sample_thresholds):
    """Write sample thresholds to a temp JSON file and return its path."""
    f = tmp_path / "thresholds.json"
    f.write_text(json.dumps(sample_thresholds))
    return f


@pytest.fixture
def agent_cfg():
    """A minimal agent config entry matching the AGENTS list schema."""
    return {
        "fub_agent_id": "test-001",
        "name": "Jane Smith",
        "email": "jane@example.com",
    }
