"""Tests for pipeline orchestration, with fake sources."""

from __future__ import annotations

from datetime import datetime

import pytest

from jobpipeline.config import load_settings
from jobpipeline.errors import LLMError, LLMFatalError, SourceError
from jobpipeline.llm.client import TokenUsage
from jobpipeline.models import JobOffer, OfferAnalysis, OfferSource
from jobpipeline.pipeline import Pipeline
from jobpipeline.sources import JobSource


def offer(source_id: str, day: int = 1) -> JobOffer:
    return JobOffer(
        source=OfferSource.ADZUNA,
        source_id=source_id,
        title="Data Scientist",
        company="Acme",
        url=f"https://example.com/{source_id}",
        published_at=datetime(2026, 11, day),
    )


class FakeSource(JobSource):
    source = OfferSource.ADZUNA
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


# ------------------------------------------------------------------ analysis


class FakeAnalyzer:
    """Returns a preset score per offer id, or raises a preset error."""

    def __init__(self, scores: dict[str, float | Exception]) -> None:
        self.scores = scores
        self.usage = TokenUsage()
        self.analyzed: list[str] = []

    def analyze(self, offer: JobOffer) -> OfferAnalysis:
        self.analyzed.append(offer.source_id)
        result = self.scores[offer.source_id]
        if isinstance(result, Exception):
            raise result
        return OfferAnalysis(
            language="fr",
            summary="A role.",
            key_requirements=["Python"],
            strengths=[],
            gaps=[],
            description_complete=True,
            fit_reasoning="Because.",
            score=result,
        )


def run_with(write_config, offers, analyzer, **kwargs):
    settings = load_settings(write_config(), env_file=None)  # min_score is 6
    return Pipeline(settings, sources=[FakeSource(offers)], analyzer=analyzer).run(**kwargs)


def test_offers_at_or_above_min_score_are_matches(write_config):
    analyzer = FakeAnalyzer({"A": 8.0, "B": 6.0, "C": 5.9})
    summary = run_with(write_config, [offer("A"), offer("B"), offer("C")], analyzer)

    assert summary.analyzed == 3
    assert summary.matched == 2
    assert sorted(m.offer.source_id for m in summary.matches) == ["A", "B"]


def test_failed_analysis_is_recorded_and_others_continue(write_config):
    analyzer = FakeAnalyzer({"A": LLMError("timeout"), "B": 7.0})
    summary = run_with(write_config, [offer("A"), offer("B")], analyzer)

    assert summary.analyzed == 1
    assert summary.matched == 1
    assert summary.errors == 1
    assert "adzuna:A: timeout" in summary.error_messages[0]


def test_fatal_error_stops_the_run(write_config):
    analyzer = FakeAnalyzer({"A": LLMFatalError("bad key"), "B": 7.0})
    with pytest.raises(LLMFatalError):
        run_with(write_config, [offer("A"), offer("B")], analyzer)
    assert analyzer.analyzed == ["A"]


def test_limit_caps_the_number_of_analyses(write_config):
    analyzer = FakeAnalyzer({"A": 7.0, "B": 7.0, "C": 7.0})
    run_with(write_config, [offer("A", 3), offer("B", 2), offer("C", 1)], analyzer, limit=2)
    assert analyzer.analyzed == ["A", "B"]  # newest first


def test_dry_run_never_calls_claude(write_config):
    analyzer = FakeAnalyzer({})
    summary = run_with(write_config, [offer("A")], analyzer, dry_run=True)
    assert analyzer.analyzed == []
    assert summary.analyzed == 0
