"""Tests for the offer analyzer (prompt building), with a fake Claude client."""

from __future__ import annotations

from pathlib import Path

from jobpipeline.cv import load_master_cv
from jobpipeline.llm.analyzer import MAX_DESCRIPTION_CHARS, OfferAnalyzer, format_offer
from jobpipeline.llm.client import TokenUsage
from jobpipeline.models import JobOffer, OfferAnalysis, OfferSource

EXAMPLE_CV = Path(__file__).resolve().parents[1] / "data" / "master_cv.example.yaml"

ANALYSIS = OfferAnalysis(
    language="fr",
    summary="Data scientist role in a consulting firm.",
    key_requirements=["Python", "Machine Learning"],
    strengths=["Python: used in every experience"],
    gaps=[],
    description_complete=True,
    fit_reasoning="Strong match on the core skills.",
    score=7.5,
)


def make_offer(**overrides) -> JobOffer:
    data = {
        "source": OfferSource.ADZUNA,
        "source_id": "1",
        "title": "Data Scientist H/F",
        "company": "Capgemini",
        "url": "https://example.com/1",
        "description": "Nous recherchons un Data Scientist maîtrisant Python.",
    }
    data.update(overrides)
    return JobOffer(**data)


class FakeClient:
    def __init__(self) -> None:
        self.usage = TokenUsage()
        self.calls: list[dict] = []

    def structured(self, **kwargs):
        self.calls.append(kwargs)
        return ANALYSIS


def test_analyze_sends_cv_as_cached_context_and_offer_as_prompt():
    client = FakeClient()
    analyzer = OfferAnalyzer(client, load_master_cv(EXAMPLE_CV), "claude-haiku-5-5")

    result = analyzer.analyze(make_offer())

    assert result == ANALYSIS
    call = client.calls[0]
    assert call["model"] == "claude-haiku-5-5"
    assert call["output"] is OfferAnalysis
    assert call["cached_context"].startswith("<candidate_cv>")
    assert "Alex Martin" in call["cached_context"]
    assert "Data Scientist H/F" in call["prompt"]
    assert "Scoring scale" in call["system"]


def test_format_offer_marks_missing_fields():
    text = format_offer(make_offer(company=None, salary=None))
    assert "Company: not specified" in text
    assert "Salary: not specified" in text


def test_format_offer_truncates_very_long_descriptions():
    text = format_offer(make_offer(description="x" * (MAX_DESCRIPTION_CHARS + 500)))
    assert "x" * MAX_DESCRIPTION_CHARS + " [...]" in text
    assert "x" * (MAX_DESCRIPTION_CHARS + 1) not in text


def test_format_offer_states_description_length():
    """Lets Claude notice short snippets (Adzuna returns only the start of the text)."""
    offer = make_offer()
    assert f"Description ({len(offer.description)} characters)" in format_offer(offer)


def test_analysis_schema_puts_score_last():
    """Claude fills fields in order: reasoning before score gives better-calibrated scores."""
    assert list(OfferAnalysis.model_fields)[-1] == "score"
