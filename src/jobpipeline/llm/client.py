"""Thin wrapper around the Anthropic SDK that returns validated Pydantic objects.

Structured output uses the API's *JSON outputs* feature (``output_config.format``): the
Pydantic model's JSON schema is sent with the request, and the model's answer is
constrained to match it, so it always arrives as parseable JSON. Some constraints the
API doesn't enforce (e.g. a score between 0 and 10) are moved into field descriptions by
``anthropic.transform_schema``; Pydantic still checks them when the answer comes back.

The SDK already retries rate limits and server errors with backoff; this wrapper maps
the remaining failures to the pipeline's errors and tracks token usage.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import cache
from typing import Any, TypeVar

import anthropic
from pydantic import BaseModel, ValidationError

from jobpipeline.errors import LLMError, LLMFatalError

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

DEFAULT_TIMEOUT_S = 120.0


@dataclass
class TokenUsage:
    """Running token totals, to keep an eye on cost."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def add(self, usage: Any) -> None:
        self.calls += 1
        self.input_tokens += getattr(usage, "input_tokens", 0) or 0
        self.output_tokens += getattr(usage, "output_tokens", 0) or 0
        self.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0
        self.cache_write_tokens += getattr(usage, "cache_creation_input_tokens", 0) or 0

    def describe(self) -> str:
        return (
            f"{self.calls} calls, {self.input_tokens:,} input tokens "
            f"(+{self.cache_read_tokens:,} read from cache, +{self.cache_write_tokens:,} cached), "
            f"{self.output_tokens:,} output tokens"
        )


@cache
def output_schema(model: type[BaseModel]) -> dict[str, Any]:
    """The JSON schema sent to the API for ``model`` (computed once per model)."""
    return anthropic.transform_schema(model)


class ClaudeClient:
    """Calls Claude and returns validated structured output."""

    def __init__(
        self,
        api_key: str,
        *,
        max_retries: int = 3,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        sdk_client: Any | None = None,
    ) -> None:
        """
        Args:
            api_key: Anthropic API key.
            max_retries: Retries for rate limits and server errors (handled by the SDK).
            timeout_s: Timeout per request.
            sdk_client: Replaces the real SDK client (tests pass a fake here).
        """
        self._sdk = sdk_client or anthropic.Anthropic(
            api_key=api_key, max_retries=max_retries, timeout=timeout_s
        )
        self.usage = TokenUsage()

    def structured(
        self,
        *,
        model: str,
        system: str,
        prompt: str,
        output: type[T],
        cached_context: str | None = None,
        max_tokens: int = 4000,
    ) -> T:
        """Ask Claude for an answer matching the ``output`` model.

        Args:
            model: Model ID, e.g. ``claude-haiku-5-5``.
            system: Instructions.
            prompt: The per-call content (e.g. one job offer).
            output: Pydantic model describing the expected answer.
            cached_context: Large text identical across calls (e.g. the master CV). It is
                marked for prompt caching, so repeated calls cost less. Caching only kicks in
                above a minimum length; below it, the text is simply sent as usual.
            max_tokens: Upper bound on the answer length, including any thinking. Only the
                tokens actually produced are billed, so a generous limit costs nothing.

        Raises:
            LLMFatalError: for errors that will fail every call (key, credit, model).
            LLMError: for any other failure of this call.
        """
        system_blocks: list[dict[str, Any]] = [{"type": "text", "text": system}]
        if cached_context:
            system_blocks.append(
                {"type": "text", "text": cached_context, "cache_control": {"type": "ephemeral"}}
            )

        try:
            response = self._sdk.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system_blocks,
                messages=[{"role": "user", "content": prompt}],
                output_config={"format": {"type": "json_schema", "schema": output_schema(output)}},
            )
        except anthropic.APIStatusError as exc:
            raise _map_status_error(exc, model) from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError(f"Cannot reach the Claude API: {exc}") from exc

        self.usage.add(getattr(response, "usage", None))
        return _parse_json_output(response, output)


def _map_status_error(exc: anthropic.APIStatusError, model: str) -> LLMError:
    message = getattr(exc, "message", str(exc))
    if isinstance(exc, anthropic.AuthenticationError | anthropic.PermissionDeniedError):
        return LLMFatalError(
            f"Claude API key rejected: check ANTHROPIC_API_KEY in .env ({message})"
        )
    if isinstance(exc, anthropic.NotFoundError):
        return LLMFatalError(
            f"Model {model!r} not found: check the llm section of config.yaml ({message})"
        )
    lowered = message.lower()
    if "credit balance" in lowered or "spend limit" in lowered:
        return LLMFatalError(
            f"Claude API credit exhausted: add credit or raise the limit ({message})"
        )
    if exc.status_code == 400 and ("schema" in lowered or "output_config" in lowered):
        # A request the API rejects by design fails identically for every offer.
        return LLMFatalError(f"Request rejected by the Claude API ({message})")
    return LLMError(f"Claude API error {exc.status_code}: {message}")


def _parse_json_output(response: Any, output: type[T]) -> T:
    """Read the JSON answer from the response's text block(s) and validate it."""
    stop_reason = getattr(response, "stop_reason", None)
    if stop_reason == "max_tokens":
        raise LLMError("Claude's answer was cut off (max_tokens reached)")
    if stop_reason == "refusal":
        raise LLMError("Claude declined to answer this request")

    text = "".join(
        getattr(block, "text", "")
        for block in getattr(response, "content", [])
        if getattr(block, "type", None) == "text"
    ).strip()
    if not text:
        raise LLMError(f"Claude returned no answer (stop reason: {stop_reason})")
    try:
        return output.model_validate_json(text)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(map(str, e['loc'])) or '(root)'}: {e['msg']}" for e in exc.errors()[:3]
        )
        raise LLMError(f"Claude's answer didn't match the expected format ({details})") from exc
