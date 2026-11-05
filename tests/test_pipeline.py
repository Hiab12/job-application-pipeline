"""Tests for pipeline orchestration, with fake sources."""

from __future__ import annotations

from datetime import datetime

from jobpipeline.config import load_settings
from jobpipeline.errors import SourceError
from jobpipeline.models import JobOffer, OfferSource
from jobpipeline.pipeline import Pipeline
from jobpipeline.sources import JobSource


def offer(source_id: str, day: int = 1) -> JobOffer:
    return JobOffer(
        source=OfferSource.FRANCE_TRAVAIL,
        source_id=source_id,
        title="Data Scientist",
        company="Acme",
        url=f"https://example.com/{source_id}",
        published_at=datetime(2026, 11, day),
    )


class FakeSource(JobSource):
    source = OfferSource.FRANCE_TRAVAIL
    display_name = "Fake"

    def __init__(self, offers=None, error: str | None = None) -> None:
        self.offers = offers or []
        self.error = error

    def fetch(self, search):
        if self.error:
            raise SourceError(self.error)
        return self.offers

    def _search_keyword(self, keyword, search):  # not used: fetch is overridden
        return []


def test_offers_from_all_sources_are_merged_without_duplicates(write_config):
    settings = load_settings(write_config(), env_file=None)
    sources = [
        FakeSource([offer("A", 1), offer("B", 3)]),
        FakeSource([offer("B", 3), offer("C", 2)]),
    ]

    summary = Pipeline(settings, sources=sources).run(dry_run=True)

    assert summary.fetched == 3
    assert summary.errors == 0


def test_failing_source_is_recorded_and_others_still_run(write_config):
    settings = load_settings(write_config(), env_file=None)
    sources = [FakeSource(error="API down"), FakeSource([offer("A")])]

    summary = Pipeline(settings, sources=sources).run(dry_run=True)

    assert summary.fetched == 1
    assert summary.errors == 1
    assert summary.error_messages == ["API down"]


def test_dry_run_logs_each_offer(write_config, caplog):
    settings = load_settings(write_config(), env_file=None)
    with caplog.at_level("INFO"):
        Pipeline(settings, sources=[FakeSource([offer("A"), offer("B")])]).run(
            dry_run=True, limit=1
        )
    logged = [r.getMessage() for r in caplog.records if "example.com" in r.getMessage()]
    assert len(logged) == 1


def test_no_source_enabled_is_not_an_error(write_config):
    settings = load_settings(write_config(), env_file=None)
    summary = Pipeline(settings, sources=[]).run(dry_run=True)
    assert summary.fetched == 0
    assert summary.errors == 0
