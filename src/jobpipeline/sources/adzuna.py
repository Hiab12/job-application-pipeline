"""Adzuna job search API (France).

Adzuna aggregates offers from many French job boards. Authentication is two keys sent
as query parameters. Results come 50 per page, so larger requests are paginated.

Note: Adzuna returns a description *snippet* (a few hundred characters), not the full
text. The full offer is on the page behind ``redirect_url``.

API reference: https://developer.adzuna.com/docs/search
"""

from __future__ import annotations

import html
import logging
import math
import re
import time
from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Any

import httpx
from pydantic import ValidationError

from jobpipeline.config import SearchConfig
from jobpipeline.errors import AuthenticationError, SourceError
from jobpipeline.http import request_with_retry
from jobpipeline.models import JobOffer, OfferSource
from jobpipeline.sources.base import JobSource

logger = logging.getLogger(__name__)

SEARCH_URL = "https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"
COUNTRY = "fr"
PAGE_SIZE = 50  # Adzuna's maximum results_per_page
SERVICE = "Adzuna"

_HTML_TAG = re.compile(r"<[^>]+>")

# Adzuna's contract codes, translated for display.
_CONTRACT_LABELS = {
    "permanent": "CDI",
    "contract": "CDD / Contrat",
    "full_time": "Temps plein",
    "part_time": "Temps partiel",
}


class AdzunaSource(JobSource):
    source = OfferSource.ADZUNA
    display_name = SERVICE

    def __init__(
        self,
        app_id: str,
        app_key: str,
        http: httpx.Client,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._app_id = app_id
        self._app_key = app_key
        self._http = http
        self._sleep = sleep

    def _search_keyword(self, keyword: str, search: SearchConfig) -> list[JobOffer]:
        """Fetch up to ``max_results_per_query`` offers, one page of 50 at a time.

        Every page is requested at full size, because Adzuna computes a page's offset
        from the page size: asking for page 3 with 20 results would return offers
        41 to 60, not 101 to 120. Extra results are trimmed at the end.
        """
        wanted = search.max_results_per_query
        offers: list[JobOffer] = []

        for page in range(1, math.ceil(wanted / PAGE_SIZE) + 1):
            results = self._fetch_page(keyword, search, page)
            offers.extend(_parse_offers(results))
            if len(results) < PAGE_SIZE:  # last page reached
                break
        return offers[:wanted]

    def _fetch_page(self, keyword: str, search: SearchConfig, page: int) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "app_id": self._app_id,
            "app_key": self._app_key,
            "what": keyword,  # all words must appear, in any order
            "where": search.location,
            "distance": search.distance_km,
            "max_days_old": search.max_age_days,
            "results_per_page": PAGE_SIZE,
            "sort_by": "date",
            "content-type": "application/json",
        }
        response = request_with_retry(
            self._http,
            "GET",
            SEARCH_URL.format(country=COUNTRY, page=page),
            service=SERVICE,
            params=params,
            sleep=self._sleep,
        )

        if response.status_code in (401, 403):
            raise AuthenticationError(
                f"{SERVICE}: authentication refused (HTTP {response.status_code}). "
                "Check ADZUNA_APP_ID / ADZUNA_APP_KEY in .env."
            )
        if response.status_code != 200:
            raise SourceError(
                f"{SERVICE}: HTTP {response.status_code} for {keyword!r}: "
                f"{_error_message(response)}"
            )
        try:
            return response.json().get("results", [])
        except ValueError as exc:
            raise SourceError(f"{SERVICE}: invalid JSON for {keyword!r}") from exc


# --------------------------------------------------------------------- parsing


def _parse_offers(results: Iterable[dict[str, Any]]) -> Iterable[JobOffer]:
    """Convert raw results, skipping (and logging) any malformed offer."""
    for raw in results:
        try:
            yield parse_offer(raw)
        except (KeyError, TypeError, ValidationError) as exc:
            logger.warning("%s: skipped malformed offer %s (%s)", SERVICE, raw.get("id"), exc)


def parse_offer(raw: dict[str, Any]) -> JobOffer:
    """Map one Adzuna result to a ``JobOffer``."""
    company = raw.get("company") or {}
    location = raw.get("location") or {}

    return JobOffer(
        source=OfferSource.ADZUNA,
        source_id=str(raw["id"]),
        title=clean_text(raw.get("title")),
        company=clean_text(company.get("display_name")) or None,
        location=clean_text(location.get("display_name")) or None,
        url=raw["redirect_url"],
        description=clean_text(raw.get("description")),
        published_at=_parse_datetime(raw.get("created")),
        contract_type=_contract_label(raw.get("contract_type"), raw.get("contract_time")),
        salary=_salary_label(raw),
    )


def clean_text(value: str | None) -> str:
    """Remove the HTML highlighting tags Adzuna adds around search terms."""
    if not value:
        return ""
    text = html.unescape(_HTML_TAG.sub("", value))
    return " ".join(text.split())


def _contract_label(contract_type: str | None, contract_time: str | None) -> str | None:
    labels = [_CONTRACT_LABELS[v] for v in (contract_type, contract_time) if v in _CONTRACT_LABELS]
    return ", ".join(labels) or None


def _salary_label(raw: dict[str, Any]) -> str | None:
    """Format the salary range. Adzuna's own estimates are left out: they aren't the employer's."""
    if raw.get("salary_is_predicted") in ("1", 1, True):
        return None
    low, high = raw.get("salary_min"), raw.get("salary_max")
    if not low and not high:
        return None
    if low and high and low != high:
        return f"{low:,.0f} - {high:,.0f} € / an".replace(",", " ")
    return f"{(low or high):,.0f} € / an".replace(",", " ")


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _error_message(response: httpx.Response) -> str:
    try:
        return str(response.json().get("display") or response.text[:200])
    except ValueError:
        return response.text[:200]
