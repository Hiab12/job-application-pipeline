"""Renders the cover letter as a Word document (French or English layout)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Cm, Pt

from jobpipeline.cv import Language, MasterCV
from jobpipeline.documents.styling import format_letter_date, set_base_style
from jobpipeline.models import CoverLetter, JobOffer


def render_cover_letter(
    master: MasterCV,
    letter: CoverLetter,
    offer: JobOffer,
    lang: Language,
    path: Path,
    today: date | None = None,
) -> Path:
    """Write the letter to ``path`` and return it."""
    doc = Document()
    set_base_style(doc, size_pt=11)
    section = doc.sections[0]
    section.page_width, section.page_height = Cm(21.0), Cm(29.7)
    section.left_margin = section.right_margin = Cm(2.3)
    section.top_margin = section.bottom_margin = Cm(2.0)

    p = master.personal
    sender = doc.add_paragraph()
    sender.add_run(p.name).bold = True
    for line in (p.phone, p.email, p.location):
        if line:
            sender.add_run().add_break()
            sender.add_run(line)

    if offer.company:
        recipient = doc.add_paragraph()
        recipient.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        recipient.paragraph_format.space_before = Pt(12)
        recipient.add_run(offer.company)

    city = p.location.split(",")[0].strip()
    when = doc.add_paragraph(format_letter_date(today or date.today(), city, lang))
    when.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    when.paragraph_format.space_before = Pt(12)

    subject = doc.add_paragraph()
    subject.paragraph_format.space_before = Pt(18)
    subject.paragraph_format.space_after = Pt(12)
    subject.add_run("Objet : " if lang == "fr" else "Subject: ").bold = True
    subject.add_run(letter.subject).bold = True

    _body_paragraph(doc, letter.salutation)
    for text in letter.paragraphs:
        _body_paragraph(doc, text, justify=True)
    _body_paragraph(doc, letter.closing, justify=True)

    signature = doc.add_paragraph(p.name)
    signature.paragraph_format.space_before = Pt(18)
    signature.alignment = WD_ALIGN_PARAGRAPH.RIGHT

    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)
    return path


def _body_paragraph(doc, text: str, justify: bool = False) -> None:
    paragraph = doc.add_paragraph(text)
    paragraph.paragraph_format.space_after = Pt(8)
    if justify:
        paragraph.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
