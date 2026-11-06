"""Tests for pipeline orchestration, with fake sources."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from jobpipeline.config import ScoringConfig, load_settings
from jobpipeline.errors import DocumentError, LLMError, LLMFatalError, NotionError, SourceError
from jobpipeline.llm.client import TokenUsage
from jobpipeline.models import (
    ApplicationStatus,
    GeneratedDocuments,
    JobOffer,
    OfferAnalysis,
    OfferSource,
    ScoredOffer,
)
from jobpipeline.pipeline import Pipeline, decide_status
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


def analysis(score: float, confidence: str = "high") -> OfferAnalysis:
    return OfferAnalysis(
        language="fr",
        summary="Un poste.",
        key_requirements=["Python"],
        strengths=[],
        gaps=[],
        description_complete=confidence == "high",
        fit_reasoning="Parce que.",
        confidence=confidence,
        score=score,
    )


class FakeAnalyzer:
    """Returns a preset (score, confidence) per offer id, or raises a preset error."""

    def __init__(self, results: dict) -> None:
        self.results = results
        self.usage = TokenUsage()
        self.analyzed: list[str] = []

    def analyze(self, offer: JobOffer) -> OfferAnalysis:
        self.analyzed.append(offer.source_id)
        result = self.results[offer.source_id]
        if isinstance(result, Exception):
            raise result
        score, confidence = result if isinstance(result, tuple) else (result, "high")
        return analysis(score, confidence)


class FakeGenerator:
    def __init__(self, errors: dict | None = None) -> None:
        self.errors = errors or {}
        self.generated: list[str] = []

    def generate(self, scored: ScoredOffer) -> GeneratedDocuments:
        if scored.offer.source_id in self.errors:
            raise self.errors[scored.offer.source_id]
        self.generated.append(scored.offer.source_id)
        folder = Path("output") / scored.offer.source_id
        return GeneratedDocuments(
            folder=folder, cv_path=folder / "cv.docx", cover_letter_path=folder / "letter.docx"
        )


class FakeTracker:
    def __init__(self, fail_connect: bool = False, fail_for: set[str] | None = None) -> None:
        self.fail_connect = fail_connect
        self.fail_for = fail_for or set()
        self.recorded: list[tuple[str, ApplicationStatus]] = []

    def connect(self) -> None:
        if self.fail_connect:
            raise NotionError("database not shared")

    def record(self, scored: ScoredOffer) -> str:
        if scored.offer.source_id in self.fail_for:
            raise NotionError("validation_error")
        self.recorded.append((scored.offer.source_id, scored.status))
        return f"https://notion.so/{scored.offer.source_id}"


def run_with(write_config, offers, analyzer, generator=None, tracker=None, **kwargs):
    settings = load_settings(write_config(), env_file=None)  # min 6, review from 5
    pipeline = Pipeline(
        settings,
        sources=[FakeSource(offers)],
        analyzer=analyzer,
        generator=generator or FakeGenerator(),
        tracker=tracker,
    )
    return pipeline.run(**kwargs)


def status_of(summary, source_id):
    return next(r.status for r in summary.results if r.offer.source_id == source_id)


@pytest.mark.parametrize(
    "score, confidence, expected",
    [
        (8.0, "high", ApplicationStatus.READY),
        (6.0, "low", ApplicationStatus.READY),
        (5.5, "low", ApplicationStatus.TO_REVIEW),
        (5.0, "low", ApplicationStatus.TO_REVIEW),
        (5.5, "high", ApplicationStatus.SKIPPED),  # confident it's a weak match
        (4.9, "low", ApplicationStatus.SKIPPED),
    ],
)
def test_decide_status(score, confidence, expected):
    assert decide_status(analysis(score, confidence), ScoringConfig()) is expected


def test_matches_get_documents_and_others_dont(write_config):
    analyzer = FakeAnalyzer({"A": 8.0, "B": (5.5, "low"), "C": 3.0})
    generator = FakeGenerator()
    summary = run_with(write_config, [offer("A"), offer("B"), offer("C")], analyzer, generator)

    assert generator.generated == ["A"]
    assert (summary.analyzed, summary.matched, summary.to_review, summary.generated) == (3, 1, 1, 1)
    assert status_of(summary, "A") is ApplicationStatus.READY
    assert status_of(summary, "B") is ApplicationStatus.TO_REVIEW
    assert status_of(summary, "C") is ApplicationStatus.SKIPPED


def test_failed_generation_marks_the_offer_as_error_and_continues(write_config):
    analyzer = FakeAnalyzer({"A": 8.0, "B": 7.0})
    generator = FakeGenerator(errors={"A": DocumentError("disk full")})
    summary = run_with(write_config, [offer("A", 2), offer("B", 1)], analyzer, generator)

    assert status_of(summary, "A") is ApplicationStatus.ERROR
    assert status_of(summary, "B") is ApplicationStatus.READY
    assert summary.generated == 1
    assert summary.errors == 1


def test_failed_analysis_is_recorded_and_others_continue(write_config):
    analyzer = FakeAnalyzer({"A": LLMError("timeout"), "B": 7.0})
    summary = run_with(write_config, [offer("A"), offer("B")], analyzer)

    assert summary.analyzed == 1
    assert summary.matched == 1
    assert summary.errors == 1
    assert "adzuna:A: timeout" in summary.error_messages[0]


@pytest.mark.parametrize("stage", ["analysis", "generation"])
def test_fatal_error_stops_the_run(write_config, stage):
    if stage == "analysis":
        analyzer = FakeAnalyzer({"A": LLMFatalError("bad key"), "B": 7.0})
        generator = FakeGenerator()
    else:
        analyzer = FakeAnalyzer({"A": 7.0, "B": 7.0})
        generator = FakeGenerator(errors={"A": LLMFatalError("no credit")})
    with pytest.raises(LLMFatalError):
        run_with(write_config, [offer("A", 2), offer("B", 1)], analyzer, generator)
    assert generator.generated == []


def test_limit_caps_the_number_of_analyses(write_config):
    analyzer = FakeAnalyzer({"A": 7.0, "B": 7.0, "C": 7.0})
    run_with(write_config, [offer("A", 3), offer("B", 2), offer("C", 1)], analyzer, limit=2)
    assert analyzer.analyzed == ["A", "B"]  # newest first


def test_dry_run_never_calls_claude(write_config):
    analyzer = FakeAnalyzer({})
    summary = run_with(write_config, [offer("A")], analyzer, dry_run=True)
    assert analyzer.analyzed == []
    assert summary.analyzed == 0


# -------------------------------------------------------------------- notion


def test_every_analyzed_offer_is_recorded_with_its_status(write_config):
    analyzer = FakeAnalyzer({"A": 8.0, "B": (5.5, "low"), "C": 3.0, "D": LLMError("x")})
    tracker = FakeTracker()
    offers = [offer("A", 4), offer("B", 3), offer("C", 2), offer("D", 1)]
    summary = run_with(write_config, offers, analyzer, tracker=tracker)

    assert tracker.recorded == [
        ("A", ApplicationStatus.READY),
        ("B", ApplicationStatus.TO_REVIEW),
        ("C", ApplicationStatus.SKIPPED),
    ]
    assert summary.in_notion == 3
    assert summary.results[0].notion_url == "https://notion.so/A"


def test_notion_failure_for_one_offer_does_not_stop_the_others(write_config):
    tracker = FakeTracker(fail_for={"A"})
    summary = run_with(
        write_config,
        [offer("A", 2), offer("B", 1)],
        FakeAnalyzer({"A": 8.0, "B": 7.0}),
        tracker=tracker,
    )
    assert [r[0] for r in tracker.recorded] == ["B"]
    assert summary.in_notion == 1
    assert summary.errors == 1


def test_unreachable_notion_stops_the_run_before_any_claude_call(write_config):
    analyzer = FakeAnalyzer({"A": 8.0})
    with pytest.raises(NotionError):
        run_with(write_config, [offer("A")], analyzer, tracker=FakeTracker(fail_connect=True))
    assert analyzer.analyzed == []


def test_without_notion_keys_results_stay_local(write_config):
    summary = run_with(write_config, [offer("A")], FakeAnalyzer({"A": 8.0}))
    assert summary.generated == 1
    assert summary.in_notion == 0
