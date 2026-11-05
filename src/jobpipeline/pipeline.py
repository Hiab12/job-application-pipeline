"""Pipeline orchestration.

``Pipeline.run`` chains the stages: fetch -> dedup -> analyze -> generate -> Notion.
Stages are plugged in step by step. Each stage is isolated: one failing source or
offer is logged and counted, and the run continues with the rest.
"""

from __future__ import annotations

import logging
import time
from contextlib import ExitStack
from dataclasses import dataclass, field

from jobpipeline.config import Settings
from jobpipeline.errors import SourceError
from jobpipeline.http import build_http_client
from jobpipeline.models import JobOffer
from jobpipeline.sources import JobSource, build_sources

logger = logging.getLogger(__name__)


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

    def __init__(self, settings: Settings, sources: list[JobSource] | None = None) -> None:
        """
        Args:
            settings: Validated configuration.
            sources: Override the sources (used by tests). By default, every source
                whose keys are configured is enabled.
        """
        self.settings = settings
        self._sources = sources

    def run(self, *, dry_run: bool = False, limit: int | None = None) -> RunSummary:
        """Execute one pass.

        Args:
            dry_run: Fetch and log offers only: no Claude calls, no files, no Notion writes.
            limit: Maximum number of new offers to process (useful to cap API costs in tests).
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
        selected = offers[:limit] if limit else offers

        if dry_run:
            self._log_offers(selected)
        else:
            # Steps 5 to 9 (dedup, analysis, documents, Notion) plug in here.
            logger.warning("Analysis not implemented yet (coming in step 5)")

        summary.duration_s = time.perf_counter() - start
        summary.log()
        return summary

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
