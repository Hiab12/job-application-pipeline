"""Turns a matched offer into a tailored CV and cover letter (.docx)."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Protocol

from jobpipeline.cv import MasterCV
from jobpipeline.documents.cv_renderer import render_cv
from jobpipeline.documents.letter_renderer import render_cover_letter
from jobpipeline.documents.styling import file_safe, slugify
from jobpipeline.errors import DocumentError
from jobpipeline.models import ApplicationContent, GeneratedDocuments, ScoredOffer

__all__ = ["DocumentGenerator", "render_cover_letter", "render_cv"]

FILE_NAMES = {
    "fr": ("CV_{name}.docx", "Lettre_de_motivation_{name}.docx"),
    "en": ("CV_{name}.docx", "Cover_letter_{name}.docx"),
}


class ContentWriter(Protocol):
    def write(self, scored: ScoredOffer) -> ApplicationContent: ...


class DocumentGenerator:
    """Writes the content with Claude, then renders both documents in one folder per offer.

    Folder: ``output/2026-11-06_hachette-livre_ingenieur-ia-generative/``, so the files sent
    to recruiters keep clean names (``CV_Hiba_El_Harrab.docx``).
    """

    def __init__(self, writer: ContentWriter, cv: MasterCV, output_dir: Path) -> None:
        self._writer = writer
        self._cv = cv
        self._output_dir = output_dir

    def generate(self, scored: ScoredOffer, today: date | None = None) -> GeneratedDocuments:
        """
        Raises:
            LLMFatalError / LLMError: from the writer.
            DocumentError: if the files couldn't be written.
        """
        content = self._writer.write(scored)
        today = today or date.today()
        lang = scored.analysis.language
        offer = scored.offer
        folder = self._output_dir / (
            f"{today.isoformat()}_{slugify(offer.company or 'entreprise', 25)}"
            f"_{slugify(offer.title, 40)}"
        )
        name = file_safe(self._cv.personal.name)
        cv_name, letter_name = (n.format(name=name) for n in FILE_NAMES[lang])

        try:
            cv_path = render_cv(self._cv, content.cv, lang, folder / cv_name)
            letter_path = render_cover_letter(
                self._cv, content.cover_letter, offer, lang, folder / letter_name, today
            )
        except OSError as exc:
            raise DocumentError(f"Could not write documents in {folder}: {exc}") from exc
        return GeneratedDocuments(folder=folder, cv_path=cv_path, cover_letter_path=letter_path)
