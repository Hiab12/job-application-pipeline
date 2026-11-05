"""Common interface for job sources."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import ClassVar

from jobpipeline.config import SearchConfig
from jobpipeline.errors import AuthenticationError, SourceError
from jobpipeline.models import JobOffer, OfferSource

logger = logging.getLogger(__name__)


class JobSource(ABC):
    """A job board API that returns offers as ``JobOffer`` objects.

    Subclasses implement ``_search_keyword`` for one keyword. ``fetch`` runs it for
    every keyword, merges the results by uid, and isolates failures: one failing
    keyword is logged and skipped, while rejected API keys stop the source at once.
    """

    source: ClassVar[OfferSource]
    display_name: ClassVar[str]

    @property
    def name(self) -> str:
        return str(self.source)

    def fetch(self, search: SearchConfig) -> list[JobOffer]:
        """Return the offers matching the search, without duplicates.

        Raises:
            AuthenticationError: if the API keys are rejected.
            SourceError: if every keyword search failed.
        """
        offers: dict[str, JobOffer] = {}
        failures: list[str] = []

        for keyword in search.keywords:
            try:
                found = self._search_keyword(keyword, search)
            except AuthenticationError:
                raise
            except SourceError as exc:
                logger.warning("%s: search failed for %r: %s", self.display_name, keyword, exc)
                failures.append(str(exc))
                continue
            logger.info("%s: %d offers for %r", self.display_name, len(found), keyword)
            for offer in found:
                offers.setdefault(offer.uid, offer)

        if failures and len(failures) == len(search.keywords):
            raise SourceError(
                f"{self.display_name}: every search failed. First error: {failures[0]}"
            )

        logger.info(
            "%s: %d unique offers for %d keywords",
            self.display_name,
            len(offers),
            len(search.keywords),
        )
        return list(offers.values())

    @abstractmethod
    def _search_keyword(self, keyword: str, search: SearchConfig) -> list[JobOffer]:
        """Search one keyword.

        Raises:
            AuthenticationError: if the API keys are rejected.
            SourceError: for any other failure of this search.
        """
