"""Configuration loading and validation.

Two sources, kept separate on purpose:

* ``config.yaml``: non-secret parameters (keywords, location, thresholds, models). Committed.
* Environment variables / ``.env``: API keys. Never committed. On GitHub Actions they come
  from repository secrets, so the same code runs locally and in CI without changes.

Both are validated at startup so a typo fails immediately with a clear message,
instead of halfway through a run.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from jobpipeline.errors import ConfigError

DEFAULT_CONFIG_PATH = Path("config.yaml")
CONFIG_PATH_ENV_VAR = "JOBPIPELINE_CONFIG"

# France Travail's "publieeDepuis" filter only accepts these values.
ALLOWED_MAX_AGE_DAYS = (1, 3, 7, 14, 31)


class _StrictModel(BaseModel):
    """Base model that rejects unknown keys, so typos in config.yaml are caught."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class SearchConfig(_StrictModel):
    keywords: list[str] = Field(min_length=1)
    location: str = Field(min_length=1)
    departement: str | None = None
    distance_km: int = Field(default=30, ge=0, le=200)
    max_age_days: int = 3
    max_results_per_source: int = Field(default=50, ge=1, le=150)

    @field_validator("keywords")
    @classmethod
    def _clean_keywords(cls, keywords: list[str]) -> list[str]:
        cleaned = [k.strip() for k in keywords if k and k.strip()]
        if not cleaned:
            raise ValueError("at least one non-empty keyword is required")
        return cleaned

    @field_validator("max_age_days")
    @classmethod
    def _check_max_age(cls, value: int) -> int:
        if value not in ALLOWED_MAX_AGE_DAYS:
            raise ValueError(f"must be one of {ALLOWED_MAX_AGE_DAYS}")
        return value


class ScoringConfig(_StrictModel):
    min_score: float = Field(default=6, ge=0, le=10)


class LLMConfig(_StrictModel):
    analysis_model: str = "claude-haiku-5-5"
    writer_model: str = "claude-sonnet-5-5"
    max_retries: int = Field(default=3, ge=0, le=10)


class PathsConfig(_StrictModel):
    master_cv: Path = Path("data/master_cv.yaml")
    output_dir: Path = Path("output")
    log_dir: Path = Path("logs")

    def resolved_against(self, base_dir: Path) -> PathsConfig:
        """Return a copy where relative paths are anchored to ``base_dir``."""

        def resolve(p: Path) -> Path:
            return p if p.is_absolute() else (base_dir / p).resolve()

        return self.model_copy(
            update={
                "master_cv": resolve(self.master_cv),
                "output_dir": resolve(self.output_dir),
                "log_dir": resolve(self.log_dir),
            }
        )


class LoggingConfig(_StrictModel):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"


class AppConfig(_StrictModel):
    """Everything in config.yaml."""

    search: SearchConfig
    scoring: ScoringConfig = ScoringConfig()
    llm: LLMConfig = LLMConfig()
    paths: PathsConfig = PathsConfig()
    logging: LoggingConfig = LoggingConfig()


class Secrets(BaseSettings):
    """API keys, read from environment variables (and ``.env`` when present).

    Every field is optional at load time: each pipeline stage declares what it needs
    through ``missing_for``, so a dry run can work with only the source keys set.
    ``SecretStr`` keeps values out of logs and tracebacks.
    """

    model_config = SettingsConfigDict(env_file_encoding="utf-8", extra="ignore", frozen=True)

    france_travail_client_id: SecretStr | None = None
    france_travail_client_secret: SecretStr | None = None
    adzuna_app_id: SecretStr | None = None
    adzuna_app_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    notion_token: SecretStr | None = None
    notion_database_id: SecretStr | None = None

    # Which keys each service needs. ClassVar: a constant, not a setting.
    REQUIRED_BY_SERVICE: ClassVar[dict[str, tuple[str, ...]]] = {
        "france_travail": ("france_travail_client_id", "france_travail_client_secret"),
        "adzuna": ("adzuna_app_id", "adzuna_app_key"),
        "anthropic": ("anthropic_api_key",),
        "notion": ("notion_token", "notion_database_id"),
    }

    def missing_for(self, *services: str) -> list[str]:
        """Return the environment variable names that are missing for the given services."""
        missing: list[str] = []
        for service in services:
            if service not in self.REQUIRED_BY_SERVICE:
                raise ValueError(f"Unknown service: {service!r}")
            for field_name in self.REQUIRED_BY_SERVICE[service]:
                value = getattr(self, field_name)
                if value is None or not value.get_secret_value().strip():
                    missing.append(field_name.upper())
        return missing


@dataclass(frozen=True)
class Settings:
    """The full runtime configuration: parameters + secrets."""

    config: AppConfig
    secrets: Secrets
    config_path: Path


def _format_validation_error(source: str, exc: ValidationError) -> str:
    lines = [f"Invalid configuration in {source}:"]
    for err in exc.errors():
        location = ".".join(str(part) for part in err["loc"]) or "(root)"
        lines.append(f"  - {location}: {err['msg']}")
    return "\n".join(lines)


def load_settings(
    config_path: str | Path | None = None,
    env_file: str | Path | None = ".env",
) -> Settings:
    """Load and validate config.yaml and the secrets.

    Args:
        config_path: Path to the YAML config.
            Defaults to ``$JOBPIPELINE_CONFIG``, then ``config.yaml``.
        env_file: Path to a ``.env`` file, or ``None`` to read only real environment variables.

    Raises:
        ConfigError: if the file is missing, unreadable, or fails validation.
    """
    path = Path(config_path or os.environ.get(CONFIG_PATH_ENV_VAR) or DEFAULT_CONFIG_PATH)
    if not path.is_file():
        raise ConfigError(f"Config file not found: {path.resolve()}")

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Could not parse {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a YAML mapping at the top level")

    try:
        app_config = AppConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(str(path), exc)) from exc

    app_config = app_config.model_copy(
        update={"paths": app_config.paths.resolved_against(path.resolve().parent)}
    )

    env_path = Path(env_file) if env_file else None
    try:
        secrets = Secrets(_env_file=env_path if env_path and env_path.is_file() else None)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error("environment variables", exc)) from exc

    return Settings(config=app_config, secrets=secrets, config_path=path.resolve())
