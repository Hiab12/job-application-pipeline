"""Job sources. ``build_sources`` enables every source whose API keys are configured."""

from __future__ import annotations

import logging

import httpx

from jobpipeline.config import Settings
from jobpipeline.sources.adzuna import AdzunaSource
from jobpipeline.sources.base import JobSource

logger = logging.getLogger(__name__)

__all__ = ["AdzunaSource", "JobSource", "build_sources"]


def build_sources(settings: Settings, http: httpx.Client) -> list[JobSource]:
    """Create the sources that have their keys set; skip the others with a warning."""
    secrets = settings.secrets
    sources: list[JobSource] = []

    if secrets.missing_for("adzuna"):
        logger.warning("Adzuna disabled: keys missing in .env")
    else:
        sources.append(
            AdzunaSource(
                secrets.adzuna_app_id.get_secret_value(),
                secrets.adzuna_app_key.get_secret_value(),
                http,
            )
        )

    return sources
