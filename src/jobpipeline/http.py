"""Shared HTTP helpers: one configured client and a retry policy for external APIs.

External APIs fail in predictable ways: rate limits (429), temporary server errors (5xx)
and network hiccups. ``request_with_retry`` retries those with exponential backoff and
returns every other response untouched, so each caller decides what a 400 or 404 means.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

import httpx

from jobpipeline import __version__
from jobpipeline.errors import PipelineError, SourceError

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = httpx.Timeout(20.0, connect=10.0)
DEFAULT_MAX_RETRIES = 3
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
MAX_RETRY_AFTER_S = 60.0
USER_AGENT = f"job-application-pipeline/{__version__}"


def build_http_client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    """Create the HTTP client shared by all sources.

    Args:
        transport: Optional custom transport. Tests pass an ``httpx.MockTransport`` here
            so no real network call is ever made.
    """
    return httpx.Client(
        timeout=DEFAULT_TIMEOUT,
        headers={"User-Agent": USER_AGENT},
        transport=transport,
        follow_redirects=True,
    )


def request_with_retry(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    service: str,
    max_retries: int = DEFAULT_MAX_RETRIES,
    backoff_base_s: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
    error_cls: type[PipelineError] = SourceError,
    **kwargs,
) -> httpx.Response:
    """Send a request, retrying on rate limits, server errors and network errors.

    Args:
        service: Human-readable API name, used in logs and error messages.
        max_retries: Retries after the first attempt (so ``max_retries + 1`` attempts in total).
        backoff_base_s: First delay; it doubles at each retry (1s, 2s, 4s...).
            A ``Retry-After`` header from the server takes precedence.
        sleep: Injected so tests run instantly.
        error_cls: Exception raised when every attempt failed (SourceError by default).
        **kwargs: Passed to ``httpx.Client.request`` (params, data, headers...).

    Raises:
        error_cls: when every attempt failed with a retryable error.
    """
    for attempt in range(max_retries + 1):
        is_last = attempt == max_retries
        try:
            response = client.request(method, url, **kwargs)
        except httpx.TransportError as exc:
            if is_last:
                raise error_cls(
                    f"{service}: network error after {attempt + 1} attempts ({exc})"
                ) from exc
            delay = backoff_base_s * 2**attempt
            logger.warning(
                "%s: network error (%s), retry in %.1fs", service, type(exc).__name__, delay
            )
            sleep(delay)
            continue

        if response.status_code not in RETRYABLE_STATUS:
            return response
        if is_last:
            raise error_cls(f"{service}: HTTP {response.status_code} after {attempt + 1} attempts")
        delay = _retry_after_seconds(response) or backoff_base_s * 2**attempt
        logger.warning(
            "%s: HTTP %d, retry %d/%d in %.1fs",
            service,
            response.status_code,
            attempt + 1,
            max_retries,
            delay,
        )
        sleep(delay)

    raise AssertionError("unreachable")  # the loop always returns or raises


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """Read a numeric ``Retry-After`` header, capped to avoid hanging a run."""
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return min(max(float(value), 0.0), MAX_RETRY_AFTER_S)
    except ValueError:
        return None
