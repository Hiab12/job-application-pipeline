"""Tests for configuration loading and validation."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from jobpipeline.config import load_settings
from jobpipeline.errors import ConfigError
from tests.conftest import VALID_CONFIG


def _config_with(**search_overrides) -> dict:
    data = copy.deepcopy(VALID_CONFIG)
    data["search"].update(search_overrides)
    return data


def test_loads_valid_config_with_defaults(write_config):
    settings = load_settings(write_config(), env_file=None)

    assert settings.config.search.keywords == ["Machine Learning", "Data Scientist"]
    assert settings.config.scoring.min_score == 6
    assert settings.config.llm.analysis_model  # default applied


def test_relative_paths_are_resolved_against_config_folder(write_config):
    path = write_config()
    settings = load_settings(path, env_file=None)

    assert settings.config.paths.output_dir == (path.parent / "output").resolve()


def test_missing_config_file_raises(tmp_path: Path):
    with pytest.raises(ConfigError, match="not found"):
        load_settings(tmp_path / "nope.yaml", env_file=None)


def test_invalid_yaml_raises(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text("search: [unclosed", "utf-8")
    with pytest.raises(ConfigError, match="Could not parse"):
        load_settings(path, env_file=None)


@pytest.mark.parametrize(
    "data, expected_field",
    [
        ({**VALID_CONFIG, "scoring": {"min_score": 11}}, "scoring.min_score"),
        (_config_with(max_age_days=0), "search.max_age_days"),
        (_config_with(keywords=["  ", ""]), "search.keywords"),
        (_config_with(keywrods=["typo"]), "search.keywrods"),
        ({"scoring": {"min_score": 6}}, "search"),
    ],
)
def test_invalid_values_are_reported_with_their_location(write_config, data, expected_field):
    with pytest.raises(ConfigError) as exc_info:
        load_settings(write_config(data), env_file=None)
    assert expected_field in str(exc_info.value)


def test_secrets_are_read_from_environment(write_config, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    settings = load_settings(write_config(), env_file=None)

    assert settings.secrets.anthropic_api_key.get_secret_value() == "sk-test"
    assert settings.secrets.missing_for("anthropic") == []
    assert settings.secrets.missing_for("notion") == ["NOTION_TOKEN", "NOTION_DATABASE_ID"]


def test_secrets_are_read_from_env_file(write_config, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("ADZUNA_APP_ID=abc\nADZUNA_APP_KEY=def\n", "utf-8")
    settings = load_settings(write_config(), env_file=env_file)

    assert settings.secrets.missing_for("adzuna") == []


def test_secret_values_are_hidden_in_repr(write_config, monkeypatch):
    monkeypatch.setenv("NOTION_TOKEN", "super-secret")
    settings = load_settings(write_config(), env_file=None)

    assert "super-secret" not in repr(settings.secrets)


def test_unknown_service_raises(write_config):
    settings = load_settings(write_config(), env_file=None)
    with pytest.raises(ValueError):
        settings.secrets.missing_for("linkedin")
