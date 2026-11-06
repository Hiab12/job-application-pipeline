"""Offer analysis: extract the key requirements and score the fit with the master CV."""

from __future__ import annotations

from pathlib import Path

from jobpipeline.cv import MasterCV
from jobpipeline.llm.client import ClaudeClient, TokenUsage
from jobpipeline.models import JobOffer, OfferAnalysis

PROMPTS_DIR = Path(__file__).parent / "prompts"
MAX_DESCRIPTION_CHARS = 8000  # long enough for any real offer, short enough to cap cost
TOOL_NAME = "record_analysis"
TOOL_DESCRIPTION = "Record the analysis of the job offer: requirements, fit and score."


def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8").strip()


class OfferAnalyzer:
    """Scores job offers against the master CV with Claude.

    The instructions and the CV are identical for every offer, so they go in the system
    prompt (the CV is marked for caching). Only the offer changes from call to call.
    """

    def __init__(self, client: ClaudeClient, cv: MasterCV, model: str) -> None:
        self._client = client
        self._model = model
        self._system = load_prompt("analyze_system.md")
        self._cv_context = f"<candidate_cv>\n{cv.render_text('en')}\n</candidate_cv>"

    @property
    def usage(self) -> TokenUsage:
        return self._client.usage

    def analyze(self, offer: JobOffer) -> OfferAnalysis:
        """Analyze one offer.

        Raises:
            LLMFatalError: if no call can succeed (stop the run).
            LLMError: if this offer couldn't be analyzed.
        """
        return self._client.structured(
            model=self._model,
            system=self._system,
            cached_context=self._cv_context,
            prompt=format_offer(offer),
            output=OfferAnalysis,
            tool_name=TOOL_NAME,
            tool_description=TOOL_DESCRIPTION,
        )


def format_offer(offer: JobOffer) -> str:
    """Render an offer for the prompt. Missing fields are written as 'not specified'."""
    description = offer.description or "(no description)"
    if len(description) > MAX_DESCRIPTION_CHARS:
        description = description[:MAX_DESCRIPTION_CHARS] + " [...]"

    def field(value: object) -> str:
        return str(value) if value else "not specified"

    published = offer.published_at.date().isoformat() if offer.published_at else None
    return (
        "Analyze this job offer for the candidate.\n\n"
        "<job_offer>\n"
        f"Title: {offer.title}\n"
        f"Company: {field(offer.company)}\n"
        f"Location: {field(offer.location)}\n"
        f"Contract: {field(offer.contract_type)}\n"
        f"Salary: {field(offer.salary)}\n"
        f"Published: {field(published)}\n"
        f"Description ({len(offer.description)} characters):\n{description}\n"
        "</job_offer>"
    )
