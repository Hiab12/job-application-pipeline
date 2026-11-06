"""Tests for the Claude client, with a fake SDK (no network, no API key)."""

from __future__ import annotations

from types import SimpleNamespace

import anthropic
import httpx
import pytest
from pydantic import BaseModel

from jobpipeline.errors import LLMError, LLMFatalError
from jobpipeline.llm.client import ClaudeClient


class Answer(BaseModel):
    score: int
    reason: str


def tool_response(payload: dict, name: str = "record", usage: dict | None = None):
    return SimpleNamespace(
        content=[
            SimpleNamespace(type="text", text="Let me think."),
            SimpleNamespace(type="tool_use", name=name, input=payload),
        ],
        stop_reason="tool_use",
        usage=SimpleNamespace(
            **(usage or {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 0})
        ),
    )


class FakeSDK:
    """Mimics ``anthropic.Anthropic``: records calls, returns or raises what it's given."""

    def __init__(self, result) -> None:
        self.result = result
        self.calls: list[dict] = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def make_client(result) -> tuple[ClaudeClient, FakeSDK]:
    sdk = FakeSDK(result)
    return ClaudeClient("key", sdk_client=sdk), sdk


def ask(client: ClaudeClient, **overrides) -> Answer:
    kwargs = {
        "model": "claude-haiku-5-5",
        "system": "Instructions",
        "prompt": "Offer",
        "output": Answer,
        "tool_name": "record",
        "tool_description": "Record the answer",
    }
    kwargs.update(overrides)
    return client.structured(**kwargs)


def status_error(cls, status: int, message: str = "error"):
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls(message, response=httpx.Response(status, request=request), body=None)


# ---------------------------------------------------------------- success


def test_returns_validated_tool_output():
    client, _ = make_client(tool_response({"score": 7, "reason": "good"}))
    assert ask(client) == Answer(score=7, reason="good")


def test_request_forces_the_tool_and_sends_the_schema():
    client, sdk = make_client(tool_response({"score": 7, "reason": "good"}))
    ask(client)

    call = sdk.calls[0]
    assert call["model"] == "claude-haiku-5-5"
    assert call["tool_choice"] == {"type": "tool", "name": "record"}
    assert call["tools"][0]["input_schema"]["properties"].keys() == {"score", "reason"}
    assert call["messages"] == [{"role": "user", "content": "Offer"}]


def test_cached_context_is_marked_for_caching():
    client, sdk = make_client(tool_response({"score": 7, "reason": "good"}))
    ask(client, cached_context="THE CV")

    system = sdk.calls[0]["system"]
    assert system[0] == {"type": "text", "text": "Instructions"}
    assert system[1]["text"] == "THE CV"
    assert system[1]["cache_control"] == {"type": "ephemeral"}


def test_usage_is_accumulated():
    usage = {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 900}
    client, _ = make_client(tool_response({"score": 7, "reason": "ok"}, usage=usage))
    ask(client)
    ask(client)

    assert client.usage.calls == 2
    assert client.usage.input_tokens == 200
    assert client.usage.cache_read_tokens == 1800
    assert "2 calls" in client.usage.describe()


# ------------------------------------------------------------- bad answers


def test_missing_tool_call_raises():
    response = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="hi")], stop_reason="max_tokens", usage=None
    )
    client, _ = make_client(response)
    with pytest.raises(LLMError, match="max_tokens"):
        ask(client)


def test_answer_not_matching_schema_raises():
    client, _ = make_client(tool_response({"score": "high"}))
    with pytest.raises(LLMError, match="expected format"):
        ask(client)


# ------------------------------------------------------------------ errors


@pytest.mark.parametrize(
    "error, match",
    [
        (status_error(anthropic.AuthenticationError, 401), "ANTHROPIC_API_KEY"),
        (status_error(anthropic.PermissionDeniedError, 403), "ANTHROPIC_API_KEY"),
        (status_error(anthropic.NotFoundError, 404), "claude-haiku-5-5"),
        (
            status_error(anthropic.BadRequestError, 400, "Your credit balance is too low"),
            "credit",
        ),
    ],
)
def test_errors_affecting_every_call_are_fatal(error, match):
    client, _ = make_client(error)
    with pytest.raises(LLMFatalError, match=match):
        ask(client)


def test_other_api_errors_are_not_fatal():
    client, _ = make_client(status_error(anthropic.InternalServerError, 500))
    with pytest.raises(LLMError) as exc_info:
        ask(client)
    assert not isinstance(exc_info.value, LLMFatalError)


def test_connection_error_is_not_fatal():
    error = anthropic.APIConnectionError(
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    )
    client, _ = make_client(error)
    with pytest.raises(LLMError, match="Cannot reach"):
        ask(client)
