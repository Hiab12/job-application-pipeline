"""Tests for the document writer's truthfulness checks, with a fake Claude client."""

from __future__ import annotations

from pathlib import Path

import pytest

from jobpipeline.cv import load_master_cv
from jobpipeline.errors import LLMError
from jobpipeline.llm.client import TokenUsage
from jobpipeline.llm.writer import DocumentWriter
from jobpipeline.models import (
    ApplicationContent,
    ApplicationStatus,
    CoverLetter,
    JobOffer,
    OfferAnalysis,
    OfferSource,
    ScoredOffer,
    SelectedEntry,
    SkillLine,
    TailoredCV,
)

EXAMPLE_CV = Path(__file__).resolve().parents[1] / "data" / "master_cv.example.yaml"


def scored(language: str = "fr") -> ScoredOffer:
    return ScoredOffer(
        offer=JobOffer(
            source=OfferSource.ADZUNA,
            source_id="1",
            title="Data Scientist",
            company="Acme",
            url="https://example.com/1",
        ),
        analysis=OfferAnalysis(
            language=language,
            summary="Poste de data scientist.",
            key_requirements=["Python", "XGBoost"],
            strengths=["Python"],
            gaps=["Kubernetes"],
            description_complete=True,
            fit_reasoning="Bon profil.",
            confidence="high",
            score=7,
        ),
        status=ApplicationStatus.READY,
    )


def content(entry_ids=("acme-data-scientist",), skills=("Python", "XGBoost")) -> ApplicationContent:
    return ApplicationContent(
        cv=TailoredCV(
            headline="Data Scientist",
            summary="Résumé.",
            skills=[SkillLine(category="ML", items=list(skills))],
            entries=[SelectedEntry(id=i, bullets=["Une puce."]) for i in entry_ids],
        ),
        cover_letter=CoverLetter(
            subject="Candidature",
            salutation="Madame, Monsieur,",
            paragraphs=["Un.", "Deux.", "Trois."],
            closing="Cordialement.",
        ),
    )


class FakeClient:
    def __init__(self, answer: ApplicationContent) -> None:
        self.answer = answer
        self.usage = TokenUsage()
        self.calls: list[dict] = []

    def structured(self, **kwargs):
        self.calls.append(kwargs)
        return self.answer


def make_writer(answer: ApplicationContent) -> tuple[DocumentWriter, FakeClient]:
    client = FakeClient(answer)
    return DocumentWriter(client, load_master_cv(EXAMPLE_CV), "claude-sonnet-5-5"), client


@pytest.mark.parametrize("language, cv_text", [("fr", "(stage)"), ("en", "(internship)")])
def test_writes_in_the_offer_language(language, cv_text):
    writer, client = make_writer(content())
    writer.write(scored(language))

    call = client.calls[0]
    assert call["model"] == "claude-sonnet-5-5"
    assert cv_text in call["cached_context"]
    assert call["prompt"].startswith(
        f"Write the application in {'French' if language == 'fr' else 'English'}"
    )


def test_prompt_lists_gaps_as_not_to_be_claimed():
    writer, client = make_writer(content())
    writer.write(scored())
    assert "gaps (never claim these): Kubernetes" in client.calls[0]["prompt"]


def test_unknown_entry_id_is_rejected():
    writer, _ = make_writer(content(entry_ids=("acme-data-scientist", "google-ceo")))
    with pytest.raises(LLMError, match="google-ceo"):
        writer.write(scored())


def test_skills_not_in_the_cv_are_dropped():
    # "Surprise" is a technology of a project in the example CV, so it's allowed.
    writer, _ = make_writer(content(skills=("python", "Kubernetes", "Surprise")))
    result = writer.write(scored())
    assert result.cv.skills[0].items == ["python", "Surprise"]


def test_fails_if_no_skill_is_from_the_cv():
    writer, _ = make_writer(content(skills=("Kubernetes", "Rust")))
    with pytest.raises(LLMError, match="skills"):
        writer.write(scored())
