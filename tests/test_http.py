"""Tests for the shared retry policy."""

from __future__ import annotations

import httpx
import pytest

from jobpipeline.errors import SourceError
from jobpipeline.http import build_http_client, request_with_retry


def client_with(responses: list) -> tuple[httpx.Client, list[httpx.Request]]:
    """A client whose transport returns the given responses (or raises exceptions) in order."""
    calls: list[httpx.Request] = []
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return build_http_client(httpx.MockTransport(handler)), calls


def test_returns_first_successful_response():
    client, calls = client_with([httpx.Response(200)])
    response = request_with_retry(client, "GET", "https://api.test", service="Test")
    assert response.status_code == 200
    assert len(calls) == 1


def test_retries_rate_limit_and_honours_retry_after():
    delays: list[float] = []
    client, calls = client_with(
        [httpx.Response(429, headers={"Retry-After": "2"}), httpx.Response(200)]
    )
    response = request_with_retry(
        client, "GET", "https://api.test", service="Test", sleep=delays.append
    )
    assert response.status_code == 200
    assert delays == [2.0]
    assert len(calls) == 2


def test_retries_network_errors_with_exponential_backoff():
    delays: list[float] = []
    error = httpx.ConnectError("boom")
    client, _ = client_with([error, error, httpx.Response(200)])
    request_with_retry(client, "GET", "https://api.test", service="Test", sleep=delays.append)
    assert delays == [1.0, 2.0]


def test_gives_up_after_max_retries():
    client, calls = client_with([httpx.Response(503)] * 4)
    with pytest.raises(SourceError, match="HTTP 503 after 4 attempts"):
        request_with_retry(client, "GET", "https://api.test", service="Test", sleep=lambda _: None)
    assert len(calls) == 4


def test_client_errors_are_returned_without_retry():
    client, calls = client_with([httpx.Response(404)])
    response = request_with_retry(client, "GET", "https://api.test", service="Test")
    assert response.status_code == 404
    assert len(calls) == 1
