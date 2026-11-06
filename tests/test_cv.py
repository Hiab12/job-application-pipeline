"""Tests for the master CV schema and loader."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from jobpipeline.__main__ import EXIT_CONFIG, EXIT_OK, main
from jobpipeline.cv import MasterCV, load_master_cv
from jobpipeline.errors import ConfigError

EXAMPLE = Path(__file__).resolve().parents[1] / "data" / "master_cv.example.yaml"


@pytest.fixture
def example_data() -> dict:
    return yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))


def write_cv(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "master_cv.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), "utf-8")
    return path


def test_example_file_is_valid():
    """The committed example must always match the schema."""
    cv = load_master_cv(EXAMPLE)
    assert cv.personal.name == "Alex Martin"
    assert cv.experience[0].id == "acme-data-scientist"


@pytest.mark.parametrize(
    "lang, expected", [("fr", "Data Scientist (stage)"), ("en", "(internship)")]
)
def test_render_text_uses_requested_language(lang, expected):
    text = load_master_cv(EXAMPLE).render_text(lang)
    assert expected in text
    assert "[acme-data-scientist]" in text  # ids let the writer reference entries


def test_missing_file_gives_setup_instructions(tmp_path):
    with pytest.raises(ConfigError, match="master_cv.example.yaml"):
        load_master_cv(tmp_path / "nope.yaml")


def test_missing_translation_is_rejected(tmp_path, example_data):
    del example_data["summary"]["en"]
    with pytest.raises(ConfigError, match="summary.en"):
        load_master_cv(write_cv(tmp_path, example_data))


@pytest.mark.parametrize("bad_date", ["2025-13", "2025/02", "Feb 2025"])
def test_invalid_dates_are_rejected(tmp_path, example_data, bad_date):
    example_data["experience"][0]["start"] = bad_date
    with pytest.raises(ConfigError, match="YYYY-MM"):
        load_master_cv(write_cv(tmp_path, example_data))


def test_end_before_start_is_rejected(tmp_path, example_data):
    example_data["experience"][0]["end"] = "2024-01"
    with pytest.raises(ConfigError, match="before start"):
        load_master_cv(write_cv(tmp_path, example_data))


def test_duplicate_ids_are_rejected(tmp_path, example_data):
    example_data["projects"].append(copy.deepcopy(example_data["experience"][0]))
    with pytest.raises(ConfigError, match="duplicate ids"):
        load_master_cv(write_cv(tmp_path, example_data))


def test_project_without_dates_is_allowed(example_data):
    del example_data["projects"][0]["start"]
    del example_data["projects"][0]["end"]
    cv = MasterCV.model_validate(example_data)
    assert cv.projects[0].period("en") == ""
    assert "[movie-recommender] Movie recommendation system\n" in cv.render_text("en")


def test_unknown_field_is_rejected(tmp_path, example_data):
    example_data["personal"]["nmae"] = "typo"
    with pytest.raises(ConfigError, match="personal.nmae"):
        load_master_cv(write_cv(tmp_path, example_data))


def test_check_cv_command(write_config, tmp_path, example_data, capsys):
    data = {
        "search": {"keywords": ["Python"], "location": "Paris"},
        "paths": {"master_cv": "cv.yaml"},
    }
    config_path = write_config(data)
    (tmp_path / "cv.yaml").write_text(yaml.safe_dump(example_data, allow_unicode=True), "utf-8")

    assert main(["--config", str(config_path), "--check-cv"]) == EXIT_OK
    assert "Master CV OK" in capsys.readouterr().out


def test_check_cv_command_reports_missing_file(write_config):
    assert main(["--config", str(write_config()), "--check-cv"]) == EXIT_CONFIG
