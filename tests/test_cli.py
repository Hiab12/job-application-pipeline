"""Tests for the command-line entry point."""

from __future__ import annotations

from jobpipeline.__main__ import EXIT_CONFIG, EXIT_OK, main


def test_dry_run_succeeds_without_api_keys(write_config, tmp_path):
    config_path = write_config()
    assert main(["--dry-run", "--config", str(config_path), "--env-file", ""]) == EXIT_OK
    assert (tmp_path / "logs" / "pipeline.log").exists()


def test_full_run_without_api_keys_fails_with_config_error(write_config):
    assert main(["--config", str(write_config()), "--env-file", ""]) == EXIT_CONFIG


def test_invalid_config_returns_config_error(write_config, capsys):
    path = write_config({"scoring": {"min_score": 6}})
    assert main(["--dry-run", "--config", str(path)]) == EXIT_CONFIG
    assert "search" in capsys.readouterr().err


def test_invalid_limit_returns_config_error(write_config):
    assert main(["--dry-run", "--config", str(write_config()), "--limit", "0"]) == EXIT_CONFIG
