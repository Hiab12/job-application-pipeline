"""Shared test fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from jobpipeline.config import Secrets

VALID_CONFIG = {
    "search": {
        "keywords": ["Machine Learning", "Data Scientist"],
        "location": "Paris",
        "departement": "75",
        "max_age_days": 3,
    },
    "scoring": {"min_score": 6},
}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make sure real API keys on the developer's machine never leak into tests."""
    for field_name in Secrets.model_fields:
        monkeypatch.delenv(field_name.upper(), raising=False)
    monkeypatch.delenv("JOBPIPELINE_CONFIG", raising=False)


@pytest.fixture
def write_config(tmp_path: Path):
    """Write a config dict to a temporary config.yaml and return its path."""

    def _write(data: dict | None = None) -> Path:
        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump(data if data is not None else VALID_CONFIG), "utf-8")
        return path

    return _write
