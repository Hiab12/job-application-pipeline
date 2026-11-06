"""Master CV: the single source of truth for every tailored CV and cover letter.

The master CV is a YAML file listing *everything* worth saying about the candidate,
in French and English. It holds more than fits on one page: for each offer, Claude
selects and rephrases the most relevant parts, but never adds anything that isn't here.

The file holds personal data, so the real one (``data/master_cv.yaml``) is gitignored.
``data/master_cv.example.yaml`` shows the format with a fictional person.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from jobpipeline.config import format_validation_error
from jobpipeline.errors import ConfigError

Language = Literal["fr", "en"]
_MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
PRESENT = "present"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Localized(_Strict):
    """A text in both languages."""

    fr: str = Field(min_length=1)
    en: str = Field(min_length=1)

    def get(self, lang: Language) -> str:
        return self.fr if lang == "fr" else self.en


class Personal(_Strict):
    name: str
    title: Localized
    email: str
    phone: str | None = None
    location: str
    linkedin: str | None = None
    github: str | None = None
    availability: Localized | None = None
    mobility: Localized | None = None


class SkillGroup(_Strict):
    category: Localized
    items: list[str] = Field(min_length=1)


class Entry(_Strict):
    """An experience or a project."""

    id: str = Field(pattern=r"^[a-z0-9-]+$", description="Stable slug, e.g. 'totalenergies-ml'")
    title: Localized
    organization: str | None = None
    location: str | None = None
    start: str | None = None  # optional for projects without dates
    end: str = PRESENT
    bullets: list[Localized] = Field(min_length=1)
    technologies: list[str] = []

    @field_validator("start", "end")
    @classmethod
    def _check_month(cls, value: str | None) -> str | None:
        if value is not None and value != PRESENT and not _MONTH.match(value):
            raise ValueError("must be YYYY-MM or 'present'")
        return value

    @model_validator(mode="after")
    def _check_order(self) -> Entry:
        if self.start and self.end != PRESENT and self.end < self.start:
            raise ValueError(f"end ({self.end}) is before start ({self.start})")
        return self

    def period(self, lang: Language) -> str:
        if not self.start:
            return ""
        end = ("présent" if lang == "fr" else "present") if self.end == PRESENT else self.end
        return f"{self.start} – {end}"


class Education(_Strict):
    degree: Localized
    school: str
    location: str | None = None
    start: int = Field(ge=1950, le=2100)
    end: int = Field(ge=1950, le=2100)
    details: Localized | None = None


class LanguageSkill(_Strict):
    name: Localized
    level: Localized


class MasterCV(_Strict):
    personal: Personal
    summary: Localized
    skills: list[SkillGroup] = Field(min_length=1)
    experience: list[Entry] = Field(min_length=1)
    projects: list[Entry] = []
    education: list[Education] = Field(min_length=1)
    languages: list[LanguageSkill] = []

    @model_validator(mode="after")
    def _unique_ids(self) -> MasterCV:
        ids = [e.id for e in [*self.experience, *self.projects]]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            raise ValueError(f"duplicate ids: {', '.join(duplicates)}")
        return self

    def render_text(self, lang: Language) -> str:
        """Plain-text version for prompts: compact, complete, with entry ids for reference."""
        p = self.personal
        lines = [
            f"# {p.name} — {p.title.get(lang)}",
            f"Location: {p.location}",
        ]
        if p.mobility:
            lines.append(f"Mobility: {p.mobility.get(lang)}")
        if p.availability:
            lines.append(f"Availability: {p.availability.get(lang)}")
        lines += ["", "## Summary", self.summary.get(lang), "", "## Skills"]
        lines += [f"- {g.category.get(lang)}: {', '.join(g.items)}" for g in self.skills]

        for heading, entries in (("Experience", self.experience), ("Projects", self.projects)):
            if not entries:
                continue
            lines += ["", f"## {heading}"]
            for e in entries:
                header = f"### [{e.id}] {e.title.get(lang)}"
                where = ", ".join(x for x in (e.organization, e.location) if x)
                if where:
                    header += f" — {where}"
                if e.start:
                    header += f" ({e.period(lang)})"
                lines.append(header)
                lines += [f"- {b.get(lang)}" for b in e.bullets]
                if e.technologies:
                    lines.append(f"  Technologies: {', '.join(e.technologies)}")

        lines += ["", "## Education"]
        for ed in self.education:
            lines.append(f"- {ed.degree.get(lang)} — {ed.school} ({ed.start}–{ed.end})")
            if ed.details:
                lines.append(f"  {ed.details.get(lang)}")

        if self.languages:
            lines += ["", "## Languages"]
            lines += [f"- {lg.name.get(lang)}: {lg.level.get(lang)}" for lg in self.languages]
        return "\n".join(lines)


def load_master_cv(path: Path) -> MasterCV:
    """Load and validate the master CV.

    Raises:
        ConfigError: if the file is missing, unreadable or invalid.
    """
    if not path.is_file():
        raise ConfigError(
            f"Master CV not found: {path}\n"
            "Copy data/master_cv.example.yaml to data/master_cv.yaml and fill it in."
        )
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Could not parse {path}: {exc}") from exc
    try:
        return MasterCV.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(format_validation_error(str(path), exc)) from exc
