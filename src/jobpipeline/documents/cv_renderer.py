"""Renders the tailored CV as a one-page A4 Word document.

Layout is fixed in code; content comes from Claude (``TailoredCV``), facts from the master
CV. Entries keep the master CV's chronological order, whatever order Claude listed them in.
"""

from __future__ import annotations

from pathlib import Path

from docx import Document
from docx.enum.text import WD_TAB_ALIGNMENT
from docx.shared import Cm, Pt

from jobpipeline.cv import Entry, Language, MasterCV
from jobpipeline.documents.styling import (
    ACCENT,
    MUTED,
    add_bottom_border,
    format_period,
    set_base_style,
)
from jobpipeline.models import SelectedEntry, TailoredCV

PAGE_WIDTH_CM, MARGIN_X_CM, MARGIN_Y_CM = 21.0, 1.6, 1.2
TEXT_WIDTH_CM = PAGE_WIDTH_CM - 2 * MARGIN_X_CM

SECTION_TITLES = {
    "fr": {"summary": "Profil", "skills": "Compétences", "experience": "Expérience professionnelle",
           "projects": "Projets", "education": "Formation", "languages": "Langues"},
    "en": {"summary": "Profile", "skills": "Skills", "experience": "Professional experience",
           "projects": "Projects", "education": "Education", "languages": "Languages"},
}  # fmt: skip


def render_cv(master: MasterCV, content: TailoredCV, lang: Language, path: Path) -> Path:
    """Write the CV to ``path`` and return it."""
    doc = Document()
    set_base_style(doc, size_pt=10)
    section = doc.sections[0]
    section.page_width, section.page_height = Cm(PAGE_WIDTH_CM), Cm(29.7)
    section.left_margin = section.right_margin = Cm(MARGIN_X_CM)
    section.top_margin = section.bottom_margin = Cm(MARGIN_Y_CM)

    titles = SECTION_TITLES[lang]
    _header(doc, master, content, lang)

    _section_title(doc, titles["summary"])
    doc.add_paragraph(content.summary)

    _section_title(doc, titles["skills"])
    for line in content.skills:
        p = doc.add_paragraph()
        p.add_run(f"{line.category} : " if lang == "fr" else f"{line.category}: ").bold = True
        p.add_run(", ".join(line.items))

    selected = {e.id: e for e in content.entries}
    for key, entries in (("experience", master.experience), ("projects", master.projects)):
        chosen = [e for e in entries if e.id in selected]  # master CV order
        if chosen:
            _section_title(doc, titles[key])
            for entry in chosen:
                _entry(doc, entry, selected[entry.id], lang)

    _section_title(doc, titles["education"])
    for ed in master.education:
        _line_with_date(
            doc, ed.degree.get(lang), ed.school, f"{ed.start} – {ed.end}", space_before=2
        )

    if master.languages:
        _section_title(doc, titles["languages"])
        doc.add_paragraph(
            " • ".join(f"{lg.name.get(lang)} : {lg.level.get(lang)}" if lang == "fr"
                       else f"{lg.name.get(lang)}: {lg.level.get(lang)}" for lg in master.languages)
        )  # fmt: skip

    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)
    return path


def _header(doc, master: MasterCV, content: TailoredCV, lang: Language) -> None:
    p = master.personal
    name = doc.add_paragraph()
    run = name.add_run(p.name)
    run.bold, run.font.size, run.font.color.rgb = True, Pt(20), ACCENT

    headline = doc.add_paragraph()
    run = headline.add_run(content.headline)
    run.font.size, run.font.color.rgb = Pt(12), ACCENT
    headline.paragraph_format.space_after = Pt(2)

    contact = [x for x in (p.phone, p.email, p.location, p.linkedin, p.github) if x]
    extra = [x.get(lang) for x in (p.availability, p.mobility) if x]
    for parts in (contact, extra):
        if parts:
            line = doc.add_paragraph()
            run = line.add_run(" • ".join(parts))
            run.font.size, run.font.color.rgb = Pt(9), MUTED


def _section_title(doc, text: str) -> None:
    p = doc.add_paragraph()
    run = p.add_run(text.upper())
    run.bold, run.font.size, run.font.color.rgb = True, Pt(10.5), ACCENT
    p.paragraph_format.space_before = Pt(8)
    p.paragraph_format.space_after = Pt(3)
    p.paragraph_format.keep_with_next = True
    add_bottom_border(p)


def _line_with_date(doc, title: str, where: str | None, period: str, space_before: float) -> None:
    """Bold title, where, and the period aligned right on the same line."""
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(space_before)
    p.paragraph_format.keep_with_next = True
    p.paragraph_format.tab_stops.add_tab_stop(Cm(TEXT_WIDTH_CM), WD_TAB_ALIGNMENT.RIGHT)
    p.add_run(title).bold = True
    if where:
        p.add_run(f" — {where}")
    if period:
        run = p.add_run(f"\t{period}")
        run.font.color.rgb = MUTED


def _entry(doc, entry: Entry, selected: SelectedEntry, lang: Language) -> None:
    where = ", ".join(x for x in (entry.organization, entry.location) if x)
    _line_with_date(
        doc, entry.title.get(lang), where, format_period(entry.start, entry.end, lang), 4
    )
    for bullet in selected.bullets:
        p = doc.add_paragraph(bullet, style="List Bullet")
        p.paragraph_format.space_after = Pt(0)
    if entry.technologies:
        p = doc.add_paragraph()
        run = p.add_run(", ".join(entry.technologies))
        run.italic, run.font.size, run.font.color.rgb = True, Pt(9), MUTED
