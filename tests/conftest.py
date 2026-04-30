"""Shared test fixtures: isolate SQLite + thresholds per test."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

# Make the repo root importable regardless of where pytest is invoked from.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Point storage at a tmp SQLite file so tests never touch real data."""
    from src import storage

    db_path = tmp_path / "metrics.db"
    monkeypatch.setattr(storage, "DB_PATH", db_path)
    yield db_path


@pytest.fixture
def isolated_thresholds(tmp_path, monkeypatch):
    """
    Copy the real thresholds.json into tmp so tests can mutate freely.
    Also clears any cached env in Python by patching the module-level constant.
    """
    src_file = ROOT / "config" / "thresholds.json"
    dst_file = tmp_path / "thresholds.json"
    shutil.copy(src_file, dst_file)

    from config import settings

    monkeypatch.setattr(settings, "THRESHOLDS_FILE", dst_file)

    # Some modules import THRESHOLDS_FILE at module-load — patch their references too.
    from src import metrics as metrics_mod

    monkeypatch.setattr(metrics_mod, "THRESHOLDS_FILE", dst_file)

    return dst_file


def write_thresholds(path: Path, metrics: dict) -> None:
    path.write_text(
        json.dumps(
            {
                "_comment": "test",
                "last_updated": "2026-04-30",
                "source": "test",
                "program_year": "2026",
                "metrics": metrics,
            },
            indent=2,
        )
    )
