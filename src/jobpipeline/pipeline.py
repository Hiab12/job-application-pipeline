"""Pipeline orchestration.

``Pipeline.run`` chains the stages: fetch -> dedup -> analyze -> generate -> Notion.
Stages are plugged in step by step. Each stage is isolated: one failing source or
offer is logged and counted, and the run continues with the rest. Only errors that
would fail every call (a rejected API key, no credit) stop the run.
"""

from __future__ import annotations

import logging
import time
from contextlib import ExitStack
from dataclasses import dataclass, field
from typing import Protocol

from jobpipeline.config import Settings
from jobpipeline.cv import load_master_cv
from jobpipeline.errors import ConfigError, LLMError, LLMFatalError, SourceError
from jobpipeline.http import build_http_client
from jobpipeline.llm import ClaudeClient, OfferAnalyzer, TokenUsage
from jobpipeline.models import JobOffer, OfferAnalysis, ScoredOffer
from jobpipeline.sources import JobSource, build_sources

logger = logging.getLogger(__name__)


class Analyzer(Protocol):
    """What the pipeline needs from an analyzer (lets tests pass a fake)."""

    @property
    def usage(self) -> TokenUsage: ...

    def analyze(self, offer: JobOffer) -> OfferAnalysis: ...


@dataclass
class RunSummary:
    """Counters reported at the end of every run."""

    fetched: int = 0
    new: int = 0
    analyzed: int = 0
    matched: int = 0
    generated: int = 0
    errors: int = 0
    duration_s: float = 0.0
    error_messages: list[str] = field(default_factory=list)
    matches: list[ScoredOffer] = field(default_factory=list)

    def record_error(self, message: str) -> None:
        self.errors += 1
        self.error_messages.append(message)

    def log(self) -> None:
        logger.info(
            "Run finished in %.1fs | fetched=%d new=%d analyzed=%d matched=%d "
            "generated=%d errors=%d",
            self.duration_s,
            self.fetched,
            self.new,
            self.analyzed,
            self.matched,
            self.generated,
            self.errors,
        )
        for message in self.error_messages:
            logger.warning("Error during run: %s", message)


class Pipeline:
    """Runs one full pass of the job application pipeline."""

    def __init__(
        self,
        settings: Settings,
        sources: list[JobSource] | None = None,
        analyzer: Analyzer | None = None,
    ) -> None:
        """
        Args:
            settings: Validated configuration.
            sources: Override the sources (used by tests). By default, every source
                whose keys are configured is enabled.
            analyzer: Override the analyzer (used by tests). By default, Claude with the
                master CV and the model from config.yaml.
        """
        self.settings = settings
        self._sources = sources
        self._analyzer = analyzer

    def run(self, *, dry_run: bool = False, limit: int | None = None) -> RunSummary:
        """Execute one pass.

        Args:
            dry_run: Fetch and log offers only: no Claude calls, no files, no Notion writes.
            limit: Maximum number of offers to analyze (caps API costs while testing).

        Raises:
            LLMFatalError: if Claude can't be used at all (bad key, no credit, unknown model).
        """
        summary = RunSummary()
        start = time.perf_counter()
        search = self.settings.config.search
        logger.info(
            "Pipeline started | dry_run=%s limit=%s keywords=%s location=%s",
            dry_run,
            limit,
            search.keywords,
            search.location,
        )

        with ExitStack() as stack:
            sources = self._sources
            if sources is None:
                http = stack.enter_context(build_http_client())
                sources = build_sources(self.settings, http)
            offers = self._fetch_offers(sources, summary)

        summary.fetched = len(offers)
        summary.new = len(offers)  # until deduplication (step 8), every offer counts as new
        selected = offers[:limit] if limit else offers

        if dry_run:
            self._log_offers(selected)
        elif selected:
            analyzer = self._analyzer or self._build_analyzer()
            try:
                self._analyze_offers(selected, analyzer, summary)
            finally:
                logger.info("Claude usage: %s", analyzer.usage.describe())
            # Steps 6 to 9 (documents, Notion, dedup) plug in here.

        summary.duration_s = time.perf_counter() - start
        summary.log()
        return summary

    # ------------------------------------------------------------------ fetch

    def _fetch_offers(self, sources: list[JobSource], summary: RunSummary) -> list[JobOffer]:
        """Collect offers from every source, merged by uid. A failing source is skipped."""
        if not sources:
            logger.warning("No job source enabled: add API keys to .env")
            return []

        offers: dict[str, JobOffer] = {}
        for source in sources:
            try:
                for offer in source.fetch(self.settings.config.search):
                    offers.setdefault(offer.uid, offer)
            except SourceError as exc:
                logger.error("Source %s failed: %s", source.name, exc)
                summary.record_error(str(exc))
        return sorted(
            offers.values(),
            key=lambda o: o.published_at.timestamp() if o.published_at else 0.0,
            reverse=True,
        )

    # ---------------------------------------------------------------- analyze

    def _build_analyzer(self) -> OfferAnalyzer:
        config, secrets = self.settings.config, self.settings.secrets
        if secrets.anthropic_api_key is None:
            raise ConfigError("ANTHROPIC_API_KEY is missing in .env")
        client = ClaudeClient(
            secrets.anthropic_api_key.get_secret_value(), max_retries=config.llm.max_retries
        )
        cv = load_master_cv(config.paths.master_cv)
        return OfferAnalyzer(client, cv, config.llm.analysis_model)

    def _analyze_offers(
        self, offers: list[JobOffer], analyzer: Analyzer, summary: RunSummary
    ) -> None:
        min_score = self.settings.config.scoring.min_score
        logger.info("Analyzing %d offers (minimum score %.1f)", len(offers), min_score)

        for i, offer in enumerate(offers, 1):
            try:
                analysis = analyzer.analyze(offer)
            except LLMFatalError:
                raise
            except LLMError as exc:
                logger.error("Analysis failed for %s: %s", offer.uid, exc)
                summary.record_error(f"{offer.uid}: {exc}")
                continue

            summary.analyzed += 1
            is_match = analysis.score >= min_score
            logger.info(
                "%3d/%d %s %4.1f/10 | %s | %s%s",
                i,
                len(offers),
                "MATCH" if is_match else "skip ",
                analysis.score,
                offer.title,
                offer.company or "(company not disclosed)",
                "" if analysis.description_complete else " | partial description",
            )
            if is_match:
                summary.matched += 1
                summary.matches.append(ScoredOffer(offer=offer, analysis=analysis))

        self._log_matches(summary.matches)

    # ---------------------------------------------------------------- logging

    @staticmethod
    def _log_offers(offers: list[JobOffer]) -> None:
        for i, offer in enumerate(offers, 1):
            published = offer.published_at.date().isoformat() if offer.published_at else "?"
            logger.info(
                "%3d. [%s] %s | %s | %s | %s\n     %s",
                i,
                offer.source,
                offer.title,
                offer.company or "(company not disclosed)",
                offer.location or "?",
                published,
                offer.url,
            )

    @staticmethod
    def _log_matches(matches: list[ScoredOffer]) -> None:
        if not matches:
            logger.info("No offer reached the minimum score")
            return
        logger.info("Matches, best first:")
        for m in sorted(matches, key=lambda m: m.analysis.score, reverse=True):
            a = m.analysis
            logger.info(
                "  %.1f/10 %s | %s\n     Why: %s\n     Requirements: %s\n     Gaps: %s\n     %s",
                a.score,
                m.offer.title,
                m.offer.company or "(company not disclosed)",
                a.fit_reasoning,
                "; ".join(a.key_requirements),
                "; ".join(a.gaps) or "none",
                m.offer.url,
            )
