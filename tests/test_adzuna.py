"""Tests for the Adzuna source, against a simulated API."""

from __future__ import annotations

import httpx
import pytest

from jobpipeline.config import SearchConfig
from jobpipeline.errors import AuthenticationError, SourceError
from jobpipeline.http import build_http_client
from jobpipeline.models import OfferSource
from jobpipeline.sources.adzuna import AdzunaSource, clean_text, parse_offer

SEARCH = SearchConfig(
    keywords=["Data Scientist", "Python"],
    location="Neuilly-Plaisance",
    distance_km=30,
    max_age_days=3,
    max_results_per_query=50,
)


def raw_offer(offer_id: str = "4211", **overrides) -> dict:
    data = {
        "id": offer_id,
        "title": "<strong>Data</strong> <strong>Scientist</strong> H/F",
        "description": "Rejoignez notre équipe &amp; travaillez sur des modèles…",
        "created": "2026-11-04T09:12:00Z",
        "redirect_url": f"https://www.adzuna.fr/land/ad/{offer_id}",
        "company": {"display_name": "Capgemini"},
        "location": {"display_name": "Paris, Île-de-France"},
        "contract_type": "permanent",
        "contract_time": "full_time",
        "salary_min": 45000,
        "salary_max": 55000,
        "salary_is_predicted": "0",
    }
    data.update(overrides)
    return data


class FakeAdzuna:
    """Simulates the search endpoint: keyword -> list of all results, served by page."""

    def __init__(self) -> None:
        self.results: dict[str, list[dict]] = {}
        self.status = 200
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={"display": "Authorisation failed"})
        params = request.url.params
        page = int(request.url.path.rsplit("/", 1)[-1])
        size = int(params["results_per_page"])
        all_results = self.results.get(params["what"], [])
        return httpx.Response(200, json={"results": all_results[(page - 1) * size : page * size]})


@pytest.fixture
def api() -> FakeAdzuna:
    return FakeAdzuna()


@pytest.fixture
def source(api: FakeAdzuna) -> AdzunaSource:
    http = build_http_client(httpx.MockTransport(api.handler))
    return AdzunaSource("app-id", "app-key", http, sleep=lambda _: None)


# ----------------------------------------------------------------- parsing


def test_parse_offer_maps_and_cleans_fields():
    offer = parse_offer(raw_offer())

    assert offer.source is OfferSource.ADZUNA
    assert offer.uid == "adzuna:4211"
    assert offer.title == "Data Scientist H/F"  # highlight tags removed
    assert offer.description.startswith("Rejoignez notre équipe & travaillez")
    assert offer.company == "Capgemini"
    assert offer.location == "Paris, Île-de-France"
    assert offer.url == "https://www.adzuna.fr/land/ad/4211"
    assert offer.contract_type == "CDI, Temps plein"
    assert offer.salary == "45 000 - 55 000 € / an"
    assert offer.published_at is not None and offer.published_at.day == 4


def test_predicted_salary_is_ignored():
    assert parse_offer(raw_offer(salary_is_predicted="1")).salary is None


def test_parse_offer_handles_missing_optional_fields():
    offer = parse_offer(
        {"id": 1, "title": "Développeur Python", "redirect_url": "https://adzuna.fr/x"}
    )
    assert offer.company is None
    assert offer.salary is None
    assert offer.contract_type is None


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("<strong>ML</strong> Engineer", "ML Engineer"),
        ("R&amp;D   data", "R&D data"),
        (None, ""),
    ],
)
def test_clean_text(raw, expected):
    assert clean_text(raw) == expected


# --------------------------------------------------------------- searching


def test_fetch_sends_expected_parameters(api, source):
    api.results["Data Scientist"] = [raw_offer()]
    source.fetch(SEARCH)

    request = api.requests[0]
    params = request.url.params
    assert request.url.path == "/v1/api/jobs/fr/search/1"
    assert params["what"] == "Data Scientist"
    assert params["where"] == "Neuilly-Plaisance"
    assert params["distance"] == "30"
    assert params["max_days_old"] == "3"
    assert params["app_id"] == "app-id"


def test_fetch_paginates_up_to_the_limit(api, source):
    search = SEARCH.model_copy(update={"keywords": ["Python"], "max_results_per_query": 120})
    api.results["Python"] = [raw_offer(str(i)) for i in range(200)]

    offers = source.fetch(search)

    assert len(offers) == 120
    pages = [r.url.path.rsplit("/", 1)[-1] for r in api.requests]
    sizes = [r.url.params["results_per_page"] for r in api.requests]
    assert pages == ["1", "2", "3"]
    assert sizes == ["50", "50", "50"]  # full pages, trimmed afterwards


def test_pagination_stops_on_last_page(api, source):
    search = SEARCH.model_copy(update={"keywords": ["Python"], "max_results_per_query": 150})
    api.results["Python"] = [raw_offer(str(i)) for i in range(30)]

    offers = source.fetch(search)

    assert len(offers) == 30
    assert len(api.requests) == 1


def test_fetch_merges_keywords_and_removes_duplicates(api, source):
    api.results["Data Scientist"] = [raw_offer("A"), raw_offer("B")]
    api.results["Python"] = [raw_offer("B"), raw_offer("C")]

    offers = source.fetch(SEARCH)

    assert sorted(o.source_id for o in offers) == ["A", "B", "C"]


def test_malformed_offer_is_skipped(api, source):
    api.results["Data Scientist"] = [raw_offer("A"), {"id": "B", "title": "no url"}]
    assert [o.source_id for o in source.fetch(SEARCH)] == ["A"]


# ----------------------------------------------------------------- errors


def test_invalid_keys_stop_the_source_immediately(api, source):
    api.status = 401
    with pytest.raises(AuthenticationError, match="ADZUNA_APP_ID"):
        source.fetch(SEARCH)
    assert len(api.requests) == 1  # second keyword not attempted


def test_other_errors_on_every_keyword_raise(api, source):
    api.status = 400
    with pytest.raises(SourceError, match="every search failed"):
        source.fetch(SEARCH)
