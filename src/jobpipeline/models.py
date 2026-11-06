"""Domain models shared by every stage of the pipeline.

Each source converts its own API format into ``JobOffer``, so the rest of the
pipeline never depends on any job board's specific format.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class OfferSource(StrEnum):
    ADZUNA = "adzuna"


class ApplicationStatus(StrEnum):
    """Values of the Status column in Notion."""

    READY = "Ready to apply"  # good match, documents generated
    TO_REVIEW = "To review"  # borderline score on a partial description: check it yourself
    SKIPPED = "Skipped"  # below the review threshold
    APPLIED = "Applied"  # set by hand in Notion
    ERROR = "Error"  # match, but document generation failed


# Words removed from company names before comparing them.
_COMPANY_NOISE = frozenset(
    {"sa", "sas", "sasu", "sarl", "eurl", "inc", "ltd", "gmbh", "group", "groupe", "france"}
)
# Gender markers in French job titles: (H/F), F/H, H/F/X...
_GENDER_MARKER = re.compile(r"\b[hf]\s*/\s*[hf](?:\s*/\s*x)?\b")
# Inclusive writing: ingénieur(e), technicien(ne), développeur·euse is left as is.
_INCLUSIVE_SUFFIX = re.compile(r"\((?:e|ne)\)")


def normalize_text(text: str) -> str:
    """Lowercase, strip accents and punctuation, collapse whitespace."""
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def normalize_title(title: str) -> str:
    """Normalize a job title so the same job posted twice compares equal.

    >>> normalize_title("Ingénieur(e) Machine Learning (H/F)")
    'ingenieur machine learning'
    """
    title = _GENDER_MARKER.sub(" ", title.lower())
    title = _INCLUSIVE_SUFFIX.sub("", title)
    return normalize_text(title)


def normalize_company(company: str) -> str:
    """Normalize a company name, dropping legal forms and generic words.

    >>> normalize_company("Capgemini France SAS")
    'capgemini'
    """
    tokens = normalize_text(company).split()
    kept = [t for t in tokens if t not in _COMPANY_NOISE]
    return " ".join(kept or tokens)


class JobOffer(BaseModel):
    """A job offer, normalized across sources."""

    model_config = ConfigDict(frozen=True)

    source: OfferSource
    source_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    company: str | None = None
    location: str | None = None
    url: str
    description: str = ""
    published_at: datetime | None = None
    contract_type: str | None = None
    salary: str | None = None

    @property
    def uid(self) -> str:
        """Unique ID across sources, e.g. ``adzuna:4211``. Used for dedup across runs."""
        return f"{self.source}:{self.source_id}"

    @property
    def fingerprint(self) -> str | None:
        """Company + title key used to spot the same offer on both sources.

        Returns ``None`` when the company is unknown: anonymous offers would otherwise
        all collide on the title alone, so they are deduplicated by ``uid`` only.
        """
        if not self.company or not normalize_company(self.company):
            return None
        return f"{normalize_company(self.company)}|{normalize_title(self.title)}"


class OfferAnalysis(BaseModel):
    """Claude's analysis of one offer against the master CV.

    Field order matters: Claude fills the fields in this order, so it lists requirements,
    strengths and gaps *before* deciding the score, which gives better-calibrated scores.
    The descriptions are sent to Claude as part of the output schema.
    """

    language: Literal["fr", "en"] = Field(
        description="Language the offer is written in ('en' for any language other than French)"
    )
    summary: str = Field(description="One sentence describing the role, in French")
    key_requirements: list[str] = Field(
        min_length=1,
        description=(
            "The 3 to 8 most important requirements, as short phrases, in the offer's language"
        ),
    )
    strengths: list[str] = Field(
        description="Requirements the candidate clearly meets, each citing CV evidence, in French"
    )
    gaps: list[str] = Field(
        description="Requirements the candidate doesn't meet or the CV doesn't show, in French"
    )
    description_complete: bool = Field(
        description="False if the description is truncated or too short to judge the role well"
    )
    fit_reasoning: str = Field(description="2 to 3 sentences explaining the score, in French")
    confidence: Literal["high", "low"] = Field(
        description="'low' if missing information could change the score by 2 points or more"
    )
    score: float = Field(ge=0, le=10, description="Fit score from 0 to 10, at most one decimal")


# ------------------------------------------------------- generated documents


class SkillLine(BaseModel):
    category: str = Field(description="Category name, in the document's language")
    items: list[str] = Field(min_length=1, description="Skills, copied from the CV's skill lists")


class SelectedEntry(BaseModel):
    id: str = Field(description="id of an experience or project from the CV, e.g. 'acme-intern'")
    bullets: list[str] = Field(
        min_length=1,
        description="1 to 4 bullets, rephrased only from this entry's bullets in the CV",
    )


class TailoredCV(BaseModel):
    headline: str = Field(description="Title under the name, adapted to the offer, max 8 words")
    summary: str = Field(description="2 to 3 sentences, max 60 words, tailored to the offer")
    skills: list[SkillLine] = Field(
        min_length=1, description="3 to 5 skill lines, most relevant to the offer first"
    )
    entries: list[SelectedEntry] = Field(
        min_length=1,
        description="Experiences and projects to show, most relevant first, max 10 bullets total",
    )


class CoverLetter(BaseModel):
    subject: str = Field(description="Subject line, e.g. 'Candidature au poste de ...'")
    salutation: str = Field(description="e.g. 'Madame, Monsieur,' or 'Dear Hiring Manager,'")
    paragraphs: list[str] = Field(
        min_length=3, max_length=5, description="3 to 4 paragraphs, 250 to 350 words in total"
    )
    closing: str = Field(description="Formal closing sentence, without the signature")


class ApplicationContent(BaseModel):
    """Everything Claude writes for one application. Facts (dates, schools...) come from the CV."""

    cv: TailoredCV
    cover_letter: CoverLetter


class GeneratedDocuments(BaseModel):
    """Paths of the documents generated for one offer."""

    folder: Path
    cv_path: Path
    cover_letter_path: Path


class ScoredOffer(BaseModel):
    """An offer, its analysis, and what the pipeline did with it."""

    offer: JobOffer
    analysis: OfferAnalysis
    status: ApplicationStatus
    documents: GeneratedDocuments | None = None
