"""Tailored CV and cover letter content for one offer.

Claude writes the *content* (headline, summary, selected bullets, letter); the code keeps
control of the *facts*: entries are referenced by their id in the master CV, so job titles,
companies, dates, education and contact details are always copied from the CV, never typed
by the model. Skills are checked against the CV, and unknown ones are dropped.
"""

from __future__ import annotations

import logging
from pathlib import Path

from jobpipeline.cv import Language, MasterCV
from jobpipeline.errors import LLMError
from jobpipeline.llm.analyzer import format_offer, load_prompt
from jobpipeline.llm.client import ClaudeClient, TokenUsage
from jobpipeline.models import ApplicationContent, OfferAnalysis, ScoredOffer, SkillLine

logger = logging.getLogger(__name__)

MAX_TOKENS = 8000
LANGUAGE_NAMES = {"fr": "French", "en": "English"}

PROMPTS_DIR = Path(__file__).parent / "prompts"


class DocumentWriter:
    """Writes the application content for a matched offer, with Claude."""

    def __init__(self, client: ClaudeClient, cv: MasterCV, model: str) -> None:
        self._client = client
        self._cv = cv
        self._model = model
        self._system = load_prompt("write_system.md")
        self._known_ids = {e.id for e in [*cv.experience, *cv.projects]}
        self._known_skills = _known_skills(cv)

    @property
    def usage(self) -> TokenUsage:
        return self._client.usage

    def write(self, scored: ScoredOffer) -> ApplicationContent:
        """Write the CV content and cover letter, in the offer's language.

        Raises:
            LLMFatalError: if no call can succeed.
            LLMError: if the content couldn't be written or references unknown CV entries.
        """
        lang: Language = scored.analysis.language
        content = self._client.structured(
            model=self._model,
            system=self._system,
            cached_context=f"<candidate_cv>\n{self._cv.render_text(lang)}\n</candidate_cv>",
            prompt=_build_prompt(scored, lang),
            output=ApplicationContent,
            max_tokens=MAX_TOKENS,
        )
        return self._check(content)

    def _check(self, content: ApplicationContent) -> ApplicationContent:
        """Enforce truthfulness rules the code can verify."""
        unknown = [e.id for e in content.cv.entries if e.id not in self._known_ids]
        if unknown:
            raise LLMError(f"Claude referenced CV entries that don't exist: {', '.join(unknown)}")

        cleaned: list[SkillLine] = []
        for line in content.cv.skills:
            kept = [s for s in line.items if _normalize(s) in self._known_skills]
            dropped = [s for s in line.items if _normalize(s) not in self._known_skills]
            if dropped:
                logger.warning("Dropped skills not found in the CV: %s", ", ".join(dropped))
            if kept:
                cleaned.append(line.model_copy(update={"items": kept}))
        if not cleaned:
            raise LLMError("None of the skills Claude listed were found in the CV")

        cv = content.cv.model_copy(update={"skills": cleaned})
        return content.model_copy(update={"cv": cv})


def _build_prompt(scored: ScoredOffer, lang: Language) -> str:
    a: OfferAnalysis = scored.analysis
    return (
        f"Write the application in {LANGUAGE_NAMES[lang]}.\n\n"
        f"{format_offer(scored.offer)}\n\n"
        "<analysis>\n"
        f"Key requirements: {'; '.join(a.key_requirements)}\n"
        f"Candidate's strengths: {'; '.join(a.strengths) or 'none listed'}\n"
        f"Candidate's gaps (never claim these): {'; '.join(a.gaps) or 'none listed'}\n"
        "</analysis>"
    )


def _normalize(skill: str) -> str:
    return " ".join(skill.lower().split())


def _known_skills(cv: MasterCV) -> set[str]:
    skills = {_normalize(s) for group in cv.skills for s in group.items}
    skills |= {_normalize(t) for e in [*cv.experience, *cv.projects] for t in e.technologies}
    return skills
