"""Thin wrapper around the Anthropic SDK that returns validated Pydantic objects.

Structured output uses *forced tool use*: Claude must call a tool whose input schema is
the Pydantic model's JSON schema, so the answer always arrives as structured data,
never as free text to parse. The result is still validated before being returned.

The SDK already retries rate limits and server errors with backoff; this wrapper maps
the remaining failures to the pipeline's errors and tracks token usage.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, TypeVar

import anthropic
from pydantic import BaseModel, ValidationError

from jobpipeline.errors import LLMError, LLMFatalError

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

DEFAULT_TIMEOUT_S = 60.0


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
        tool_name: str,
        tool_description: str,
        cached_context: str | None = None,
        max_tokens: int = 2000,
    ) -> T:
        """Ask Claude for an answer matching the ``output`` model.

        Args:
            model: Model ID, e.g. ``claude-haiku-5-5``.
            system: Instructions.
            prompt: The per-call content (e.g. one job offer).
            output: Pydantic model describing the expected answer.
            tool_name: Name of the tool Claude must call to give its answer.
            tool_description: What the tool records, for Claude.
            cached_context: Large text identical across calls (e.g. the master CV). It is
                marked for prompt caching, so repeated calls cost less. Caching only kicks in
                above a minimum length; below it, the text is simply sent as usual.
            max_tokens: Upper bound on the answer length.

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
                tools=[
                    {
                        "name": tool_name,
                        "description": tool_description,
                        "input_schema": output.model_json_schema(),
                    }
                ],
                tool_choice={"type": "tool", "name": tool_name},
            )
        except anthropic.APIStatusError as exc:
            raise _map_status_error(exc, model) from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError(f"Cannot reach the Claude API: {exc}") from exc

        self.usage.add(getattr(response, "usage", None))
        return _parse_tool_output(response, tool_name, output)


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
    if "credit balance" in message.lower() or "spend limit" in message.lower():
        return LLMFatalError(
            f"Claude API credit exhausted: add credit or raise the limit ({message})"
        )
    return LLMError(f"Claude API error {exc.status_code}: {message}")


def _parse_tool_output(response: Any, tool_name: str, output: type[T]) -> T:
    block = next(
        (
            b
            for b in getattr(response, "content", [])
            if getattr(b, "type", None) == "tool_use" and getattr(b, "name", None) == tool_name
        ),
        None,
    )
    if block is None:
        reason = getattr(response, "stop_reason", "unknown")
        raise LLMError(f"Claude returned no structured answer (stop reason: {reason})")
    try:
        return output.model_validate(block.input)
    except ValidationError as exc:
        details = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:3])
        raise LLMError(f"Claude's answer didn't match the expected format ({details})") from exc
