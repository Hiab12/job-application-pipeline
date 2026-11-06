"""Shared Word formatting helpers: fonts, borders, dates, file names."""

from __future__ import annotations

import re
import unicodedata
from datetime import date

from docx.document import Document as DocumentObject
from docx.enum.text import WD_LINE_SPACING
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor
from docx.text.paragraph import Paragraph

from jobpipeline.cv import PRESENT, Language

FONT = "Calibri"
ACCENT = RGBColor(0x1F, 0x3A, 0x5F)  # dark blue
MUTED = RGBColor(0x55, 0x55, 0x55)

_MONTHS_SHORT = {
    "fr": ["janv.", "févr.", "mars", "avr.", "mai", "juin",
           "juil.", "août", "sept.", "oct.", "nov.", "déc."],
    "en": ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
}  # fmt: skip
_MONTHS_LONG = {
    "fr": ["janvier", "février", "mars", "avril", "mai", "juin",
           "juillet", "août", "septembre", "octobre", "novembre", "décembre"],
    "en": ["January", "February", "March", "April", "May", "June",
           "July", "August", "September", "October", "November", "December"],
}  # fmt: skip


def set_base_style(doc: DocumentObject, size_pt: float) -> None:
    """Default font and tight spacing for the whole document."""
    style = doc.styles["Normal"]
    style.font.name = FONT
    style.font.size = Pt(size_pt)
    style.element.rPr.rFonts.set(qn("w:eastAsia"), FONT)  # some Word versions need this
    fmt = style.paragraph_format
    fmt.space_before = Pt(0)
    fmt.space_after = Pt(0)
    fmt.line_spacing_rule = WD_LINE_SPACING.SINGLE


def add_bottom_border(paragraph: Paragraph, color: str = "1F3A5F") -> None:
    """A thin line under the paragraph, used under section titles."""
    p_pr = paragraph._p.get_or_add_pPr()
    borders = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    for key, value in (("w:val", "single"), ("w:sz", "6"), ("w:space", "1"), ("w:color", color)):
        bottom.set(qn(key), value)
    borders.append(bottom)
    p_pr.append(borders)


def format_month(value: str | None, lang: Language) -> str:
    """'2025-09' -> 'sept. 2025' / 'Sep 2025'; 'present' -> 'présent' / 'present'."""
    if not value:
        return ""
    if value == PRESENT:
        return "présent" if lang == "fr" else "present"
    year, month = value.split("-")
    return f"{_MONTHS_SHORT[lang][int(month) - 1]} {year}"


def format_period(start: str | None, end: str, lang: Language) -> str:
    if not start:
        return ""
    return f"{format_month(start, lang)} – {format_month(end, lang)}"


def format_letter_date(day: date, city: str, lang: Language) -> str:
    """'Paris, le 6 novembre 2026' / 'Paris, 6 November 2026'."""
    month = _MONTHS_LONG[lang][day.month - 1]
    if lang == "fr":
        day_str = "1er" if day.day == 1 else str(day.day)
        return f"{city}, le {day_str} {month} {day.year}"
    return f"{city}, {day.day} {month} {day.year}"


def slugify(text: str, max_length: int = 40) -> str:
    """'Hachette Livre – Ingénieur IA (F/H)' -> 'hachette-livre-ingenieur-ia-f-h'."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return text[:max_length].rstrip("-") or "offer"


def file_safe(text: str) -> str:
    """'Hiba El Harrab' -> 'Hiba_El_Harrab' (keeps accents, removes risky characters)."""
    return re.sub(r"[^\w-]+", "_", text, flags=re.UNICODE).strip("_")
