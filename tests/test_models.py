"""Tests for the domain models and text normalization."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from jobpipeline.models import (
    JobOffer,
    OfferAnalysis,
    OfferSource,
    normalize_company,
    normalize_title,
)


def make_offer(**overrides) -> JobOffer:
    data = {
        "source": OfferSource.FRANCE_TRAVAIL,
        "source_id": "123ABC",
        "title": "Data Scientist (H/F)",
        "company": "Capgemini",
        "url": "https://example.com/offer/123ABC",
    }
    data.update(overrides)
    return JobOffer(**data)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Data Scientist (H/F)", "data scientist"),
        ("Data Scientist F/H", "data scientist"),
        ("DATA SCIENTIST - H / F / X", "data scientist"),
        ("Ingénieur(e) Machine Learning", "ingenieur machine learning"),
        ("Développeur Python Junior", "developpeur python junior"),
    ],
)
def test_normalize_title(raw, expected):
    assert normalize_title(raw) == expected


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Capgemini France SAS", "capgemini"),
        ("CAPGEMINI", "capgemini"),
        ("Groupe Renault", "renault"),
        ("SAS", "sas"),  # never normalize a name down to nothing
    ],
)
def test_normalize_company(raw, expected):
    assert normalize_company(raw) == expected


def test_uid_combines_source_and_id():
    assert make_offer().uid == "france_travail:123ABC"


def test_same_offer_on_two_sources_has_same_fingerprint():
    ft = make_offer(title="Data Scientist (H/F)", company="Capgemini France SAS")
    adzuna = make_offer(
        source=OfferSource.ADZUNA, source_id="999", title="Data Scientist F/H", company="CAPGEMINI"
    )
    assert ft.uid != adzuna.uid
    assert ft.fingerprint == adzuna.fingerprint == "capgemini|data scientist"


@pytest.mark.parametrize("company", [None, "", "  "])
def test_offer_without_company_has_no_fingerprint(company):
    assert make_offer(company=company).fingerprint is None


def test_offer_is_immutable():
    offer = make_offer()
    with pytest.raises(ValidationError):
        offer.title = "Something else"


def test_analysis_rejects_out_of_range_score():
    with pytest.raises(ValidationError):
        OfferAnalysis(score=12, key_requirements=[], language="fr", summary="", fit_reasoning="")
