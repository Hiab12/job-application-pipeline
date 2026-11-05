"""Pipeline orchestration.

``Pipeline.run`` chains the stages: fetch -> dedup -> analyze -> generate -> Notion.
Stages are plugged in step by step; this skeleton already provides the run summary
and the dry-run / limit options that every later step relies on.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from jobpipeline.config import Settings
from jobpipeline.models import JobOffer

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

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

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

        offers = self._fetch_offers(summary)
        summary.fetched = len(offers)

        # Steps 5 to 9 (dedup, analysis, documents, Notion) plug in here.

        summary.duration_s = time.perf_counter() - start
        summary.log()
        return summary

    def _fetch_offers(self, summary: RunSummary) -> list[JobOffer]:
        """Collect offers from every source. Implemented in steps 2 and 3."""
        logger.warning("No job sources implemented yet (coming in step 2)")
        return []
