"""Tests for the Notion client and tracker, against a simulated Notion API."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from jobpipeline.errors import NotionError
from jobpipeline.http import build_http_client
from jobpipeline.models import ApplicationStatus, GeneratedDocuments
from jobpipeline.notion import NotionClient, NotionTracker, create_tracker_database
from jobpipeline.notion.client import NOTION_VERSION, extract_notion_id
from jobpipeline.notion.tracker import MAX_TEXT, SCHEMA, build_properties
from tests.test_writer import scored

DB_ID = "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d"
DS_ID = "ds-0001"


class FakeNotion:
    """Simulates the endpoints the pipeline uses and records every request."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.properties = {name: {"type": next(iter(d))} for name, d in SCHEMA.items()}
        self.fail_pages = False
        self.uploads = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix("/v1")
        if request.headers.get("Authorization") != "Bearer ntn_test":
            return httpx.Response(401, json={"code": "unauthorized", "message": "bad token"})
        if request.method == "POST" and path == "/databases":
            return httpx.Response(200, json={"id": "new-db", "url": "https://notion.so/new-db"})
        if request.method == "GET" and path == f"/databases/{DB_ID}":
            return httpx.Response(200, json={"id": DB_ID, "data_sources": [{"id": DS_ID}]})
        if request.method == "GET" and path == f"/data_sources/{DS_ID}":
            return httpx.Response(200, json={"properties": self.properties})
        if request.method == "POST" and path == "/file_uploads":
            self.uploads += 1
            return httpx.Response(200, json={"id": f"upload-{self.uploads}"})
        if request.method == "POST" and path.endswith("/send"):
            return httpx.Response(200, json={"status": "uploaded"})
        if request.method == "POST" and path == "/pages":
            if self.fail_pages:
                return httpx.Response(400, json={"code": "validation_error", "message": "bad"})
            return httpx.Response(200, json={"url": "https://notion.so/page-1"})
        return httpx.Response(404, json={"code": "object_not_found", "message": "nope"})

    def bodies(self, method: str, path: str) -> list[dict]:
        return [
            json.loads(r.content)
            for r in self.requests
            if r.method == method and r.url.path == f"/v1{path}"
        ]


@pytest.fixture
def api() -> FakeNotion:
    return FakeNotion()


@pytest.fixture
def client(api: FakeNotion) -> NotionClient:
    http = build_http_client(httpx.MockTransport(api.handler))
    return NotionClient("ntn_test", http, sleep=lambda _: None)


def docs(tmp_path: Path) -> GeneratedDocuments:
    cv, letter = tmp_path / "CV_Alex.docx", tmp_path / "Lettre_Alex.docx"
    cv.write_bytes(b"cv bytes")
    letter.write_bytes(b"letter bytes")
    return GeneratedDocuments(folder=tmp_path, cv_path=cv, cover_letter_path=letter)


# ------------------------------------------------------------------ ids


@pytest.mark.parametrize(
    "value",
    [
        DB_ID,
        f"https://www.notion.so/me/Candidatures-{DB_ID}?v=0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f",
        "1a2b3c4d-5e6f-7a8b-9c0d-1e2f3a4b5c6d",
    ],
)
def test_extract_notion_id(value):
    assert extract_notion_id(value).replace("-", "") == DB_ID


def test_extract_notion_id_rejects_garbage():
    with pytest.raises(NotionError):
        extract_notion_id("my database")


# ---------------------------------------------------------------- setup


def test_setup_creates_database_with_full_schema(api, client):
    result = create_tracker_database(client, f"https://www.notion.so/Jobs-{DB_ID}")

    body = api.bodies("POST", "/databases")[0]
    assert result == {"id": "new-db", "url": "https://notion.so/new-db"}
    assert body["parent"] == {"type": "page_id", "page_id": DB_ID}
    assert body["initial_data_source"]["properties"].keys() == SCHEMA.keys()
    assert api.requests[0].headers["Notion-Version"] == NOTION_VERSION


def test_bad_token_gives_a_helpful_error():
    http = build_http_client(httpx.MockTransport(FakeNotion().handler))
    with pytest.raises(NotionError, match="NOTION_TOKEN"):
        NotionClient("wrong", http).get_database(DB_ID)


def test_database_not_shared_gives_a_helpful_error(client):
    with pytest.raises(NotionError, match="Connections"):
        NotionTracker(client, "ffffffffffffffffffffffffffffffff").connect()


# --------------------------------------------------------------- connect


def test_connect_accepts_matching_columns(client):
    NotionTracker(client, DB_ID).connect()


def test_connect_reports_missing_and_wrong_columns(api, client):
    del api.properties["Score"]
    api.properties["Link"] = {"type": "rich_text"}
    with pytest.raises(NotionError) as exc_info:
        NotionTracker(client, DB_ID).connect()
    message = str(exc_info.value)
    assert "'Score' is missing" in message
    assert "'Link' is rich_text, expected url" in message
    assert "--setup-notion" in message


def test_record_requires_connect(client):
    with pytest.raises(NotionError, match="connect"):
        NotionTracker(client, DB_ID).record(scored())


# ---------------------------------------------------------------- record


def test_record_match_uploads_documents_and_attaches_them(api, client, tmp_path):
    tracker = NotionTracker(client, DB_ID)
    tracker.connect()
    offer = scored().model_copy(update={"documents": docs(tmp_path)})

    url = tracker.record(offer, today=date(2026, 11, 6))

    assert url == "https://notion.so/page-1"
    assert [b["filename"] for b in api.bodies("POST", "/file_uploads")] == [
        "CV_Alex.docx",
        "Lettre_Alex.docx",
    ]
    page = api.bodies("POST", "/pages")[0]
    assert page["parent"] == {"type": "data_source_id", "data_source_id": DS_ID}
    props = page["properties"]
    assert props["CV"]["files"][0]["file_upload"] == {"id": "upload-1"}
    assert props["Cover letter"]["files"][0]["file_upload"] == {"id": "upload-2"}
    assert props["Status"] == {"select": {"name": "Ready to apply"}}
    assert props["Added"] == {"date": {"start": "2026-11-06"}}
    assert page["children"]  # analysis written in the page body


def test_record_without_documents_uploads_nothing(api, client):
    tracker = NotionTracker(client, DB_ID)
    tracker.connect()
    tracker.record(scored().model_copy(update={"status": ApplicationStatus.SKIPPED}))

    assert api.uploads == 0
    props = api.bodies("POST", "/pages")[0]["properties"]
    assert props["CV"] == {"files": []}
    assert props["Status"] == {"select": {"name": "Skipped"}}


def test_failed_page_creation_raises(api, client):
    api.fail_pages = True
    tracker = NotionTracker(client, DB_ID)
    tracker.connect()
    with pytest.raises(NotionError, match="validation_error"):
        tracker.record(scored())


def test_properties_cover_the_schema_and_truncate_long_text():
    long = scored()
    long = long.model_copy(
        update={"analysis": long.analysis.model_copy(update={"fit_reasoning": "x" * 5000})}
    )
    props = build_properties(long, {}, date(2026, 11, 6))

    assert props.keys() == SCHEMA.keys()
    assert len(props["Why"]["rich_text"][0]["text"]["content"]) == MAX_TEXT
    assert props["Offer ID"]["rich_text"][0]["text"]["content"] == "adzuna:1"
