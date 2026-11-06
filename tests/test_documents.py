"""Tests for the Word renderers and the document generator (real .docx files in tmp_path)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from docx import Document

from jobpipeline.cv import load_master_cv
from jobpipeline.documents import DocumentGenerator, render_cover_letter, render_cv
from jobpipeline.documents.styling import format_letter_date, format_period, slugify
from tests.test_writer import content, scored

EXAMPLE_CV = Path(__file__).resolve().parents[1] / "data" / "master_cv.example.yaml"


@pytest.fixture
def cv():
    return load_master_cv(EXAMPLE_CV)


def text_of(path: Path) -> str:
    return "\n".join(p.text for p in Document(path).paragraphs)


def test_cv_contains_facts_from_master_and_content_from_claude(cv, tmp_path):
    path = render_cv(cv, content().cv, "fr", tmp_path / "cv.docx")
    text = text_of(path)

    assert "Alex Martin" in text  # name: from the master CV
    assert "alex.martin@example.com" in text
    assert "Data Scientist (stage) — Acme Corp, Lyon\tfévr. 2025 – août 2025" in text
    assert "Une puce." in text  # bullet: from Claude
    assert "École d'ingénieurs Exemple" in text
    assert "FORMATION" in text and "COMPÉTENCES" in text


def test_cv_leaves_out_entries_claude_did_not_select(cv, tmp_path):
    text = text_of(render_cv(cv, content().cv, "en", tmp_path / "cv.docx"))
    assert "Movie recommendation system" not in text
    assert "PROJECTS" not in text


def test_letter_has_french_layout(cv, tmp_path):
    letter = content().cover_letter
    path = render_cover_letter(
        cv, letter, scored().offer, "fr", tmp_path / "l.docx", date(2026, 11, 1)
    )
    text = text_of(path)

    assert "Lyon, le 1er novembre 2026" in text
    assert "Objet : Candidature" in text
    assert "Acme" in text
    assert text.rstrip().endswith("Alex Martin")


def test_generator_creates_one_folder_per_offer(cv, tmp_path):
    from jobpipeline.llm.writer import DocumentWriter
    from tests.test_writer import FakeClient

    writer = DocumentWriter(FakeClient(content()), cv, "model")
    docs = DocumentGenerator(writer, cv, tmp_path).generate(scored(), today=date(2026, 11, 6))

    assert docs.folder == tmp_path / "2026-11-06_acme_data-scientist"
    assert docs.cv_path.name == "CV_Alex_Martin.docx"
    assert docs.cover_letter_path.name == "Lettre_de_motivation_Alex_Martin.docx"
    assert docs.cv_path.exists() and docs.cover_letter_path.exists()


@pytest.mark.parametrize(
    "start, end, lang, expected",
    [
        ("2025-09", "2026-09", "fr", "sept. 2025 – sept. 2026"),
        ("2025-09", "present", "en", "Sep 2025 – present"),
        (None, "present", "fr", ""),
    ],
)
def test_format_period(start, end, lang, expected):
    assert format_period(start, end, lang) == expected


def test_format_letter_date_in_english():
    assert format_letter_date(date(2026, 11, 6), "Paris", "en") == "Paris, 6 November 2026"


def test_slugify():
    assert slugify("Hachette Livre – Ingénieur IA (F/H)") == "hachette-livre-ingenieur-ia-f-h"
