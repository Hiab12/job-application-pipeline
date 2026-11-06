"""Minimal Notion API client (API version 2026-03-11), on top of the shared HTTP helper.

Only what the pipeline needs: create and inspect a database, create pages, upload files.
Since API version 2025-09-03, a database holds one or more *data sources*: pages are
created in a data source, and the columns (properties) belong to the data source.

API reference: https://developers.notion.com/reference
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from jobpipeline.errors import NotionError
from jobpipeline.http import request_with_retry

logger = logging.getLogger(__name__)

API_URL = "https://api.notion.com/v1"
NOTION_VERSION = "2026-03-11"
SERVICE = "Notion"

_ID_32 = re.compile(r"([0-9a-f]{32})")
_ID_UUID = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")


def extract_notion_id(value: str) -> str:
    """Accept an ID or a Notion URL and return the ID.

    >>> extract_notion_id("https://www.notion.so/me/Jobs-1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d?v=99")
    '1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d'
    """
    text = value.strip().lower().split("?")[0]
    for pattern in (_ID_UUID, _ID_32):
        matches = pattern.findall(text)
        if matches:
            return matches[-1]
    raise NotionError(f"Not a Notion ID or URL: {value!r}")


class NotionClient:
    def __init__(
        self, token: str, http: httpx.Client, *, sleep: Callable[[float], None] = time.sleep
    ) -> None:
        self._http = http
        self._sleep = sleep
        self._headers = {"Authorization": f"Bearer {token}", "Notion-Version": NOTION_VERSION}

    # --------------------------------------------------------------- databases

    def create_database(
        self, parent_page_id: str, title: str, properties: dict[str, Any]
    ) -> dict[str, Any]:
        """Create a database (and its first data source) inside a page."""
        return self._request(
            "POST",
            "/databases",
            json={
                "parent": {"type": "page_id", "page_id": parent_page_id},
                "title": [{"type": "text", "text": {"content": title}}],
                "initial_data_source": {"properties": properties},
            },
        )

    def get_database(self, database_id: str) -> dict[str, Any]:
        return self._request("GET", f"/databases/{database_id}")

    def get_data_source(self, data_source_id: str) -> dict[str, Any]:
        return self._request("GET", f"/data_sources/{data_source_id}")

    # ------------------------------------------------------------------- pages

    def create_page(
        self,
        data_source_id: str,
        properties: dict[str, Any],
        children: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "parent": {"type": "data_source_id", "data_source_id": data_source_id},
            "properties": properties,
        }
        if children:
            body["children"] = children
        return self._request("POST", "/pages", json=body)

    # ------------------------------------------------------------------- files

    def upload_file(self, path: Path, content_type: str) -> str:
        """Upload a small file (under 5 MB on free plans) and return its file upload ID.

        The returned ID must be attached to a page within one hour.
        """
        created = self._request(
            "POST",
            "/file_uploads",
            json={"mode": "single_part", "filename": path.name, "content_type": content_type},
        )
        upload_id = created["id"]
        self._request(
            "POST",
            f"/file_uploads/{upload_id}/send",
            files={"file": (path.name, path.read_bytes(), content_type)},
        )
        return upload_id

    # ---------------------------------------------------------------- internals

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response = request_with_retry(
            self._http,
            method,
            f"{API_URL}{path}",
            service=SERVICE,
            headers=self._headers,
            sleep=self._sleep,
            error_cls=NotionError,
            **kwargs,
        )
        if response.is_success:
            return response.json()
        raise NotionError(_describe_error(response))


def _describe_error(response: httpx.Response) -> str:
    try:
        payload = response.json()
        code, message = payload.get("code", ""), payload.get("message", response.text[:200])
    except ValueError:
        code, message = "", response.text[:200]

    if response.status_code == 401:
        return f"{SERVICE}: token rejected. Check NOTION_TOKEN in .env ({message})"
    if response.status_code == 404 or code == "object_not_found":
        return (
            f"{SERVICE}: not found, or not shared with your integration. In Notion, open the "
            f"page or database, then ••• > Connections > add your integration ({message})"
        )
    return f"{SERVICE}: HTTP {response.status_code} {code}: {message}"
