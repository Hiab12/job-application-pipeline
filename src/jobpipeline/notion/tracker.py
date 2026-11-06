"""Application tracker: one Notion row per analyzed offer.

Every analyzed offer gets a row, including skipped ones, so the database is the memory of
what was already processed (used for deduplication). Create a filtered view in Notion to
hide the "Skipped" rows. Matches get their CV and cover letter uploaded into the row.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from jobpipeline.errors import NotionError
from jobpipeline.models import ApplicationStatus, ScoredOffer
from jobpipeline.notion.client import NotionClient, extract_notion_id

logger = logging.getLogger(__name__)

DATABASE_TITLE = "Candidatures"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MAX_TEXT = 2000  # Notion's limit per rich text object

STATUS_COLORS = {
    ApplicationStatus.READY: "green",
    ApplicationStatus.TO_REVIEW: "yellow",
    ApplicationStatus.APPLIED: "blue",
    ApplicationStatus.SKIPPED: "gray",
    ApplicationStatus.ERROR: "red",
}

# Column name -> Notion property definition. Used both to create the database and to
# check that an existing one has every column the pipeline writes to.
SCHEMA: dict[str, dict[str, Any]] = {
    "Role": {"title": {}},
    "Company": {"rich_text": {}},
    "Status": {
        "select": {"options": [{"name": str(s), "color": c} for s, c in STATUS_COLORS.items()]}
    },
    "Score": {"number": {"format": "number"}},
    "Confidence": {
        "select": {
            "options": [{"name": "high", "color": "green"}, {"name": "low", "color": "orange"}]
        }
    },
    "Location": {"rich_text": {}},
    "Contract": {"rich_text": {}},
    "Salary": {"rich_text": {}},
    "Key requirements": {"rich_text": {}},
    "Gaps": {"rich_text": {}},
    "Why": {"rich_text": {}},
    "Link": {"url": {}},
    "CV": {"files": {}},
    "Cover letter": {"files": {}},
    "Published": {"date": {}},
    "Added": {"date": {}},
    "Source": {"select": {}},
    "Offer ID": {"rich_text": {}},
}


def create_tracker_database(client: NotionClient, parent_page: str) -> dict[str, str]:
    """Create the tracker database inside a page. Returns its id and URL."""
    database = client.create_database(extract_notion_id(parent_page), DATABASE_TITLE, SCHEMA)
    return {"id": database["id"], "url": database.get("url", "")}


class NotionTracker:
    def __init__(self, client: NotionClient, database_id: str) -> None:
        self._client = client
        self._database_id = extract_notion_id(database_id)
        self._data_source_id: str | None = None

    def connect(self) -> None:
        """Find the database's data source and check its columns.

        Called before any Claude call, so a Notion problem stops the run before it costs
        anything.

        Raises:
            NotionError: if the database is unreachable or misses columns.
        """
        database = self._client.get_database(self._database_id)
        sources = database.get("data_sources") or []
        if not sources:
            raise NotionError("Notion: this database has no data source")
        self._data_source_id = sources[0]["id"]

        properties = self._client.get_data_source(self._data_source_id).get("properties", {})
        problems = []
        for name, definition in SCHEMA.items():
            expected = next(iter(definition))
            actual = properties.get(name, {}).get("type")
            if actual is None:
                problems.append(f"'{name}' is missing")
            elif actual != expected:
                problems.append(f"'{name}' is {actual}, expected {expected}")
        if problems:
            raise NotionError(
                "Notion database columns don't match: "
                + "; ".join(problems)
                + ". Create a fresh database with: python -m jobpipeline --setup-notion <page URL>"
            )
        logger.info("Notion: connected to the tracker database")

    def record(self, scored: ScoredOffer, today: date | None = None) -> str:
        """Create the row for one offer (uploading its documents if any). Returns its URL.

        Raises:
            NotionError: if the row couldn't be created.
        """
        if self._data_source_id is None:
            raise NotionError("NotionTracker.connect() must be called first")

        files: dict[str, list[dict[str, Any]]] = {"CV": [], "Cover letter": []}
        if scored.documents:
            for column, path in (
                ("CV", scored.documents.cv_path),
                ("Cover letter", scored.documents.cover_letter_path),
            ):
                upload_id = self._client.upload_file(path, DOCX_MIME)
                files[column] = [
                    {"type": "file_upload", "file_upload": {"id": upload_id}, "name": path.name}
                ]

        page = self._client.create_page(
            self._data_source_id,
            build_properties(scored, files, today or date.today()),
            build_body(scored),
        )
        return page.get("url", "")


# ----------------------------------------------------------------- formatting


def _text(value: str | None) -> list[dict[str, Any]]:
    if not value:
        return []
    if len(value) > MAX_TEXT:
        value = value[: MAX_TEXT - 1] + "…"
    return [{"type": "text", "text": {"content": value}}]


def build_properties(
    scored: ScoredOffer, files: dict[str, list[dict[str, Any]]], today: date
) -> dict[str, Any]:
    offer, a = scored.offer, scored.analysis
    published = offer.published_at.date().isoformat() if offer.published_at else None
    return {
        "Role": {"title": _text(offer.title)},
        "Company": {"rich_text": _text(offer.company)},
        "Status": {"select": {"name": str(scored.status)}},
        "Score": {"number": a.score},
        "Confidence": {"select": {"name": a.confidence}},
        "Location": {"rich_text": _text(offer.location)},
        "Contract": {"rich_text": _text(offer.contract_type)},
        "Salary": {"rich_text": _text(offer.salary)},
        "Key requirements": {"rich_text": _text(" • ".join(a.key_requirements))},
        "Gaps": {"rich_text": _text(" • ".join(a.gaps))},
        "Why": {"rich_text": _text(a.fit_reasoning)},
        "Link": {"url": offer.url},
        "CV": {"files": files.get("CV", [])},
        "Cover letter": {"files": files.get("Cover letter", [])},
        "Published": {"date": {"start": published} if published else None},
        "Added": {"date": {"start": today.isoformat()}},
        "Source": {"select": {"name": str(offer.source)}},
        "Offer ID": {"rich_text": _text(offer.uid)},
    }


def build_body(scored: ScoredOffer) -> list[dict[str, Any]]:
    """Page content: the full analysis and the offer text, readable on any device."""
    a, offer = scored.analysis, scored.offer

    def heading(text: str) -> dict[str, Any]:
        return {"object": "block", "type": "heading_3", "heading_3": {"rich_text": _text(text)}}

    def paragraph(text: str) -> dict[str, Any]:
        return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": _text(text)}}

    def bullets(items: list[str]) -> list[dict[str, Any]]:
        return [
            {
                "object": "block",
                "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": _text(item)},
            }
            for item in items
        ] or [paragraph("—")]

    blocks = [heading("Résumé du poste"), paragraph(a.summary)]
    blocks += [heading(f"Analyse : {a.score:g}/10 (confiance {a.confidence})")]
    blocks += [paragraph(a.fit_reasoning)]
    blocks += [heading("Points forts"), *bullets(a.strengths)]
    blocks += [heading("Lacunes"), *bullets(a.gaps)]
    blocks += [heading("Exigences clés"), *bullets(a.key_requirements)]
    if offer.description:
        note = "" if a.description_complete else " (extrait fourni par la source)"
        blocks += [heading(f"Description{note}"), paragraph(offer.description)]
    return blocks
