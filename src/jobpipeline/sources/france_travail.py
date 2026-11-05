"""France Travail "Offres d'emploi v2" API.

Authentication uses the OAuth2 client-credentials flow: the client ID and secret are
exchanged for an access token valid for about 25 minutes, which is cached and renewed
shortly before it expires.

Access to this API is granted on request (francetravail.io > contact). Until it is,
the token request fails and the pipeline continues with the other sources.
"""

from __future__ import annotations

import logging
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

TOKEN_URL = "https://entreprise.francetravail.fr/connexion/oauth2/access_token"
SEARCH_URL = "https://api.francetravail.io/partenaire/offresdemploi/v2/offres/search"
OFFER_PAGE_URL = "https://candidat.francetravail.fr/offres/recherche/detail/{offer_id}"
SCOPE = "api_offresdemploiv2 o2dsoffre"

TOKEN_REFRESH_MARGIN_S = 60  # renew the token 1 minute before it expires
DEFAULT_TOKEN_LIFETIME_S = 1499
SERVICE = "France Travail"


class FranceTravailSource(JobSource):
    source = OfferSource.FRANCE_TRAVAIL
    display_name = SERVICE

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        http: httpx.Client,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = http
        self._clock = clock
        self._sleep = sleep
        self._token: str | None = None
        self._token_expires_at = 0.0

    # ---------------------------------------------------------------- searching

    def _search_keyword(self, keyword: str, search: SearchConfig) -> list[JobOffer]:
        params: dict[str, Any] = {
            "motsCles": keyword,
            "publieeDepuis": search.max_age_days,
            "range": f"0-{search.max_results_per_query - 1}",
        }
        if search.departement:
            params["departement"] = search.departement

        response = self._authorized_get(SEARCH_URL, params)

        if response.status_code == 204:  # no offer matches
            return []
        if response.status_code not in (200, 206):  # 206 = more results exist than requested
            raise SourceError(
                f"{SERVICE}: HTTP {response.status_code} for {keyword!r}: {response.text[:200]}"
            )

        try:
            results = response.json().get("resultats", [])
        except ValueError as exc:
            raise SourceError(f"{SERVICE}: invalid JSON for {keyword!r}") from exc
        return list(_parse_offers(results))

    def _authorized_get(self, url: str, params: dict[str, Any]) -> httpx.Response:
        """GET with a valid token. If the token was rejected (401), renew it and retry once."""
        for attempt in range(2):
            response = request_with_retry(
                self._http,
                "GET",
                url,
                service=SERVICE,
                params=params,
                headers={"Authorization": f"Bearer {self._get_token()}"},
                sleep=self._sleep,
            )
            if response.status_code != 401 or attempt == 1:
                return response
            logger.info("%s: token rejected, requesting a new one", SERVICE)
            self._token = None
        raise AssertionError("unreachable")

    # ----------------------------------------------------------- authentication

    def _get_token(self) -> str:
        if self._token and self._clock() < self._token_expires_at:
            return self._token

        response = request_with_retry(
            self._http,
            "POST",
            TOKEN_URL,
            service=SERVICE,
            params={"realm": "/partenaire"},
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "scope": SCOPE,
            },
            sleep=self._sleep,
        )
        if response.status_code in (400, 401):
            raise AuthenticationError(
                f"{SERVICE}: authentication refused (HTTP {response.status_code}). Check "
                "FRANCE_TRAVAIL_CLIENT_ID / FRANCE_TRAVAIL_CLIENT_SECRET in .env and that your "
                "application has been granted access to the 'Offres d'emploi' API."
            )
        if response.status_code != 200:
            raise SourceError(f"{SERVICE}: token request failed (HTTP {response.status_code})")

        try:
            payload = response.json()
            token = payload["access_token"]
        except (ValueError, KeyError) as exc:
            raise SourceError(f"{SERVICE}: unexpected token response") from exc

        lifetime = float(payload.get("expires_in", DEFAULT_TOKEN_LIFETIME_S))
        self._token = token
        self._token_expires_at = self._clock() + lifetime - TOKEN_REFRESH_MARGIN_S
        logger.debug("%s: new access token, valid %.0fs", SERVICE, lifetime)
        return token


# --------------------------------------------------------------------- parsing


def _parse_offers(results: Iterable[dict[str, Any]]) -> Iterable[JobOffer]:
    """Convert raw results, skipping (and logging) any malformed offer."""
    for raw in results:
        try:
            yield parse_offer(raw)
        except (KeyError, TypeError, ValidationError) as exc:
            logger.warning("%s: skipped malformed offer %s (%s)", SERVICE, raw.get("id"), exc)


def parse_offer(raw: dict[str, Any]) -> JobOffer:
    """Map one France Travail result to a ``JobOffer``."""
    offer_id = str(raw["id"])
    workplace = raw.get("lieuTravail") or {}
    company = raw.get("entreprise") or {}
    salary = raw.get("salaire") or {}

    return JobOffer(
        source=OfferSource.FRANCE_TRAVAIL,
        source_id=offer_id,
        title=(raw.get("intitule") or "").strip(),
        company=(company.get("nom") or "").strip() or None,
        location=workplace.get("libelle"),
        url=OFFER_PAGE_URL.format(offer_id=offer_id),
        description=raw.get("description") or "",
        published_at=_parse_datetime(raw.get("dateCreation")),
        contract_type=raw.get("typeContratLibelle") or raw.get("typeContrat"),
        salary=salary.get("libelle"),
    )


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
