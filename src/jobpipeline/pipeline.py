"""Pipeline orchestration.

``Pipeline.run`` chains the stages: fetch -> dedup -> analyze -> generate -> Notion.
Each stage is isolated: one failing source or offer is logged and counted, and the run
continues with the rest. Only errors that would fail every call (a rejected API key,
no credit) stop the run.

Decision after analysis, with the thresholds from config.yaml:

    score >= min_score                                -> documents, "Ready to apply"
    review_min_score <= score < min_score, low conf.  -> "To review" (no documents)
    anything else                                     -> "Skipped"
"""

from __future__ import annotations

import logging
import time
from contextlib import ExitStack
from dataclasses import dataclass, field
from typing import Protocol

from jobpipeline.config import ScoringConfig, Settings
from jobpipeline.cv import load_master_cv
from jobpipeline.documents import DocumentGenerator
from jobpipeline.errors import (
    ConfigError,
    DocumentError,
    LLMError,
    LLMFatalError,
    NotionError,
    SourceError,
)
from jobpipeline.http import build_http_client
from jobpipeline.llm import ClaudeClient, DocumentWriter, OfferAnalyzer, TokenUsage
from jobpipeline.models import (
    ApplicationStatus,
    GeneratedDocuments,
    JobOffer,
    OfferAnalysis,
    ScoredOffer,
)
from jobpipeline.notion import NotionClient, NotionTracker
from jobpipeline.sources import JobSource, build_sources

logger = logging.getLogger(__name__)


class Analyzer(Protocol):
    @property
    def usage(self) -> TokenUsage: ...

    def analyze(self, offer: JobOffer) -> OfferAnalysis: ...


class Generator(Protocol):
    def generate(self, scored: ScoredOffer) -> GeneratedDocuments: ...


class Tracker(Protocol):
    def connect(self) -> None: ...

    def record(self, scored: ScoredOffer) -> str: ...


@dataclass
class RunSummary:
    """Counters and results reported at the end of every run."""

    fetched: int = 0
    new: int = 0
    analyzed: int = 0
    matched: int = 0
    to_review: int = 0
    generated: int = 0
    in_notion: int = 0
    errors: int = 0
    duration_s: float = 0.0
    error_messages: list[str] = field(default_factory=list)
    results: list[ScoredOffer] = field(default_factory=list)

    def record_error(self, message: str) -> None:
        self.errors += 1
        self.error_messages.append(message)

    def with_status(self, *statuses: ApplicationStatus) -> list[ScoredOffer]:
        return [r for r in self.results if r.status in statuses]

    def log(self) -> None:
        logger.info(
            "Run finished in %.1fs | fetched=%d new=%d analyzed=%d matched=%d to_review=%d "
            "generated=%d in_notion=%d errors=%d",
            self.duration_s,
            self.fetched,
            self.new,
            self.analyzed,
            self.matched,
            self.to_review,
            self.generated,
            self.in_notion,
            self.errors,
        )
        for message in self.error_messages:
            logger.warning("Error during run: %s", message)


def decide_status(analysis: OfferAnalysis, scoring: ScoringConfig) -> ApplicationStatus:
    """Status before document generation (a match becomes READY once its documents exist)."""
    if analysis.score >= scoring.min_score:
        return ApplicationStatus.READY
    if analysis.score >= scoring.review_min_score and analysis.confidence == "low":
        return ApplicationStatus.TO_REVIEW
    return ApplicationStatus.SKIPPED


class Pipeline:
    """Runs one full pass of the job application pipeline."""

    def __init__(
        self,
        settings: Settings,
        sources: list[JobSource] | None = None,
        analyzer: Analyzer | None = None,
        generator: Generator | None = None,
        tracker: Tracker | None = None,
    ) -> None:
        """
        Args:
            settings: Validated configuration.
            sources, analyzer, generator, tracker: Overrides used by tests. By default
                they are built from the configuration and the API keys. Without Notion
                keys, results stay in the logs and the output folder.
        """
        self.settings = settings
        self._sources = sources
        self._analyzer = analyzer
        self._generator = generator
        self._tracker = tracker

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
            http = None
            if self._sources is None or (self._tracker is None and not dry_run):
                http = stack.enter_context(build_http_client())
            sources = (
                self._sources if self._sources is not None else build_sources(self.settings, http)
            )
            offers = self._fetch_offers(sources, summary)

            summary.fetched = len(offers)
            summary.new = len(offers)  # until deduplication (step 8), every offer is new
            selected = offers[:limit] if limit else offers

            if dry_run:
                self._log_offers(selected)
            elif selected:
                tracker = self._connect_tracker(http)  # fails fast, before any Claude cost
                self._build_llm_stages()
                try:
                    self._analyze_offers(selected, summary)
                    self._generate_documents(summary)
                finally:
                    logger.info("Claude usage: %s", self._analyzer.usage.describe())
                if tracker:
                    self._record_in_notion(tracker, summary)
                self._log_results(summary)
                # Step 8 (deduplication) plugs in here.

        summary.duration_s = time.perf_counter() - start
        summary.log()
        return summary

    # ------------------------------------------------------------------ setup

    def _build_llm_stages(self) -> None:
        """Create the analyzer and generator, sharing one Claude client (one usage count)."""
        if self._analyzer and self._generator:
            return
        config, secrets = self.settings.config, self.settings.secrets
        if secrets.anthropic_api_key is None:
            raise ConfigError("ANTHROPIC_API_KEY is missing in .env")
        client = ClaudeClient(
            secrets.anthropic_api_key.get_secret_value(), max_retries=config.llm.max_retries
        )
        cv = load_master_cv(config.paths.master_cv)
        self._analyzer = self._analyzer or OfferAnalyzer(client, cv, config.llm.analysis_model)
        self._generator = self._generator or DocumentGenerator(
            DocumentWriter(client, cv, config.llm.writer_model), cv, config.paths.output_dir
        )

    def _connect_tracker(self, http) -> Tracker | None:
        tracker = self._tracker
        if tracker is None:
            secrets = self.settings.secrets
            if secrets.missing_for("notion"):
                logger.warning("Notion disabled: keys missing in .env (results stay local)")
                return None
            client = NotionClient(secrets.notion_token.get_secret_value(), http)
            tracker = NotionTracker(client, secrets.notion_database_id.get_secret_value())
        tracker.connect()
        return tracker

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

    def _analyze_offers(self, offers: list[JobOffer], summary: RunSummary) -> None:
        scoring = self.settings.config.scoring
        logger.info(
            "Analyzing %d offers (match from %.1f, review from %.1f)",
            len(offers),
            scoring.min_score,
            scoring.review_min_score,
        )
        labels = {
            ApplicationStatus.READY: "MATCH ",
            ApplicationStatus.TO_REVIEW: "REVIEW",
            ApplicationStatus.SKIPPED: "skip  ",
        }

        for i, offer in enumerate(offers, 1):
            try:
                analysis = self._analyzer.analyze(offer)
            except LLMFatalError:
                raise
            except LLMError as exc:
                logger.error("Analysis failed for %s: %s", offer.uid, exc)
                summary.record_error(f"{offer.uid}: {exc}")
                continue

            status = decide_status(analysis, scoring)
            summary.analyzed += 1
            summary.matched += status is ApplicationStatus.READY
            summary.to_review += status is ApplicationStatus.TO_REVIEW
            summary.results.append(ScoredOffer(offer=offer, analysis=analysis, status=status))
            logger.info(
                "%3d/%d %s %4.1f/10 (%s confidence) | %s | %s",
                i,
                len(offers),
                labels[status],
                analysis.score,
                analysis.confidence,
                offer.title,
                offer.company or "(company not disclosed)",
            )

    # --------------------------------------------------------------- generate

    def _generate_documents(self, summary: RunSummary) -> None:
        matches = summary.with_status(ApplicationStatus.READY)
        if matches:
            logger.info("Writing tailored CV and cover letter for %d matches", len(matches))
        for scored in matches:
            try:
                scored.documents = self._generator.generate(scored)
            except LLMFatalError:
                raise
            except (LLMError, DocumentError) as exc:
                logger.error("Documents failed for %s: %s", scored.offer.uid, exc)
                summary.record_error(f"{scored.offer.uid}: {exc}")
                scored.status = ApplicationStatus.ERROR
                continue
            summary.generated += 1
            logger.info("Documents ready: %s", scored.documents.folder)

    # ----------------------------------------------------------------- notion

    def _record_in_notion(self, tracker: Tracker, summary: RunSummary) -> None:
        """One row per analyzed offer. A failed row is logged; the documents stay on disk."""
        logger.info("Saving %d offers to Notion", len(summary.results))
        for scored in summary.results:
            try:
                scored.notion_url = tracker.record(scored)
            except NotionError as exc:
                logger.error("Notion failed for %s: %s", scored.offer.uid, exc)
                summary.record_error(f"{scored.offer.uid}: {exc}")
                continue
            summary.in_notion += 1

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
    def _log_results(summary: RunSummary) -> None:
        shown = summary.with_status(
            ApplicationStatus.READY, ApplicationStatus.ERROR, ApplicationStatus.TO_REVIEW
        )
        if not shown:
            logger.info("No match and nothing to review in this run")
            return
        logger.info("Results, best first:")
        for r in sorted(shown, key=lambda r: r.analysis.score, reverse=True):
            a = r.analysis
            logger.info(
                "  [%s] %.1f/10 %s | %s\n     Pourquoi : %s\n     Exigences : %s\n"
                "     Lacunes : %s\n     %s%s",
                r.status,
                a.score,
                r.offer.title,
                r.offer.company or "(company not disclosed)",
                a.fit_reasoning,
                "; ".join(a.key_requirements),
                "; ".join(a.gaps) or "aucune",
                r.offer.url,
                (f"\n     Documents : {r.documents.folder}" if r.documents else "")
                + (f"\n     Notion : {r.notion_url}" if r.notion_url else ""),
            )
