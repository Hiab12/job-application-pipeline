"""Tests for the France Travail source, against a simulated API."""

from __future__ import annotations

import httpx
import pytest

from jobpipeline.config import SearchConfig
from jobpipeline.errors import SourceError
from jobpipeline.http import build_http_client
from jobpipeline.models import OfferSource
from jobpipeline.sources.france_travail import (
    SEARCH_URL,
    TOKEN_URL,
    FranceTravailSource,
    parse_offer,
)

SEARCH = SearchConfig(
    keywords=["Data Scientist", "Python"], location="Paris", departement="75", max_age_days=3
)


def raw_offer(offer_id: str = "123ABC", **overrides) -> dict:
    data = {
        "id": offer_id,
        "intitule": "Data Scientist (H/F)",
        "description": "Nous recherchons un(e) Data Scientist...",
        "dateCreation": "2026-11-04T15:23:11.000Z",
        "lieuTravail": {"libelle": "75 - PARIS 08"},
        "entreprise": {"nom": "Capgemini"},
        "typeContrat": "CDI",
        "typeContratLibelle": "Contrat à durée indéterminée",
        "salaire": {"libelle": "Annuel de 45000 Euros"},
    }
    data.update(overrides)
    return data


class FakeAPI:
    """Simulates the token endpoint and the search endpoint."""

    def __init__(self) -> None:
        self.token_status = 200
        self.token_requests = 0
        self.search_requests: list[httpx.Request] = []
        # keyword -> (status, results)
        self.results: dict[str, tuple[int, list[dict]]] = {}
        self.reject_next_search_with_401 = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith(TOKEN_URL):
            self.token_requests += 1
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": "invalid_client"})
            return httpx.Response(
                200, json={"access_token": f"token-{self.token_requests}", "expires_in": 1499}
            )
        if url.startswith(SEARCH_URL):
            self.search_requests.append(request)
            if self.reject_next_search_with_401:
                self.reject_next_search_with_401 = False
                return httpx.Response(401)
            keyword = request.url.params["motsCles"]
            status, results = self.results.get(keyword, (204, []))
            if status == 204:
                return httpx.Response(204)
            return httpx.Response(status, json={"resultats": results})
        return httpx.Response(404)


@pytest.fixture
def api() -> FakeAPI:
    return FakeAPI()


@pytest.fixture
def clock():
    now = {"t": 0.0}

    def _clock() -> float:
        return now["t"]

    _clock.advance = lambda s: now.__setitem__("t", now["t"] + s)  # type: ignore[attr-defined]
    return _clock


@pytest.fixture
def source(api: FakeAPI, clock) -> FranceTravailSource:
    http = build_http_client(httpx.MockTransport(api.handler))
    return FranceTravailSource("id", "secret", http, clock=clock, sleep=lambda _: None)


# ----------------------------------------------------------------- parsing


def test_parse_offer_maps_all_fields():
    offer = parse_offer(raw_offer())

    assert offer.source is OfferSource.FRANCE_TRAVAIL
    assert offer.uid == "france_travail:123ABC"
    assert offer.title == "Data Scientist (H/F)"
    assert offer.company == "Capgemini"
    assert offer.location == "75 - PARIS 08"
    assert offer.url.endswith("/detail/123ABC")
    assert offer.contract_type == "Contrat à durée indéterminée"
    assert offer.salary == "Annuel de 45000 Euros"
    assert offer.published_at is not None and offer.published_at.year == 2026


def test_parse_offer_handles_missing_optional_fields():
    offer = parse_offer({"id": "X1", "intitule": "Développeur Python"})

    assert offer.company is None
    assert offer.location is None
    assert offer.published_at is None
    assert offer.fingerprint is None


# --------------------------------------------------------------- searching


def test_fetch_sends_expected_parameters(api, source):
    api.results["Data Scientist"] = (206, [raw_offer()])
    source.fetch(SEARCH)

    params = api.search_requests[0].url.params
    assert params["motsCles"] == "Data Scientist"
    assert params["departement"] == "75"
    assert params["publieeDepuis"] == "3"
    assert params["range"] == "0-49"
    assert api.search_requests[0].headers["Authorization"] == "Bearer token-1"


def test_fetch_merges_keywords_and_removes_duplicates(api, source):
    api.results["Data Scientist"] = (200, [raw_offer("A"), raw_offer("B")])
    api.results["Python"] = (206, [raw_offer("B"), raw_offer("C")])

    offers = source.fetch(SEARCH)

    assert sorted(o.source_id for o in offers) == ["A", "B", "C"]


def test_no_content_returns_empty_list(api, source):
    assert source.fetch(SEARCH) == []


def test_malformed_offer_is_skipped(api, source):
    api.results["Data Scientist"] = (200, [raw_offer("A"), {"id": "B"}, {"intitule": "no id"}])
    offers = source.fetch(SEARCH)
    assert [o.source_id for o in offers] == ["A"]


def test_one_failing_keyword_does_not_cancel_the_others(api, source):
    api.results["Data Scientist"] = (400, [])
    api.results["Python"] = (200, [raw_offer("C")])
    offers = source.fetch(SEARCH)
    assert [o.source_id for o in offers] == ["C"]


def test_all_keywords_failing_raises(api, source):
    api.results["Data Scientist"] = (400, [])
    api.results["Python"] = (400, [])
    with pytest.raises(SourceError, match="every search failed"):
        source.fetch(SEARCH)


# ---------------------------------------------------------- authentication


def test_token_is_reused_across_requests(api, source):
    source.fetch(SEARCH)
    assert api.token_requests == 1
    assert len(api.search_requests) == 2


def test_token_is_renewed_when_expired(api, source, clock):
    source.fetch(SEARCH)
    clock.advance(1500)
    source.fetch(SEARCH)
    assert api.token_requests == 2


def test_rejected_token_is_renewed_once(api, source):
    api.reject_next_search_with_401 = True
    api.results["Data Scientist"] = (200, [raw_offer("A")])
    offers = source.fetch(SEARCH)
    assert api.token_requests == 2
    assert [o.source_id for o in offers] == ["A"]


def test_invalid_credentials_raise_a_helpful_error(api, source):
    api.token_status = 401
    with pytest.raises(SourceError, match="FRANCE_TRAVAIL_CLIENT_ID"):
        source.fetch(SEARCH)
    assert api.search_requests == []
