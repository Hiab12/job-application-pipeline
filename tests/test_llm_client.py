"""Tests for the Claude client, with a fake SDK (no network, no API key)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import anthropic
import httpx
import pytest
from pydantic import BaseModel, Field

from jobpipeline.errors import LLMError, LLMFatalError
from jobpipeline.llm.client import ClaudeClient, output_schema


class Answer(BaseModel):
    score: float = Field(ge=0, le=10)
    reason: str


def json_response(payload, stop_reason: str = "end_turn", usage: dict | None = None):
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return SimpleNamespace(
        content=[
            SimpleNamespace(type="thinking", thinking="Let me weigh this."),
            SimpleNamespace(type="text", text=text),
        ],
        stop_reason=stop_reason,
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
        "model": "claude-sonnet-5-5",
        "system": "Instructions",
        "prompt": "Offer",
        "output": Answer,
    }
    kwargs.update(overrides)
    return client.structured(**kwargs)


def status_error(cls, status: int, message: str = "error"):
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls(message, response=httpx.Response(status, request=request), body=None)


# ---------------------------------------------------------------- success


def test_returns_validated_json_output():
    client, _ = make_client(json_response({"score": 7, "reason": "good"}))
    assert ask(client) == Answer(score=7, reason="good")


def test_request_uses_json_outputs_and_no_forced_tool():
    client, sdk = make_client(json_response({"score": 7, "reason": "good"}))
    ask(client)

    call = sdk.calls[0]
    assert call["model"] == "claude-sonnet-5-5"
    assert "tools" not in call and "tool_choice" not in call
    fmt = call["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["schema"]["properties"].keys() == {"score", "reason"}
    assert call["messages"] == [{"role": "user", "content": "Offer"}]


def test_schema_has_no_constraints_the_api_rejects():
    """minimum/maximum aren't supported by JSON outputs: they move into the description."""
    schema = output_schema(Answer)
    score = schema["properties"]["score"]
    assert "minimum" not in score and "maximum" not in score
    assert schema["additionalProperties"] is False


def test_out_of_range_value_is_still_rejected_locally():
    client, _ = make_client(json_response({"score": 12, "reason": "too high"}))
    with pytest.raises(LLMError, match="expected format"):
        ask(client)


def test_cached_context_is_marked_for_caching():
    client, sdk = make_client(json_response({"score": 7, "reason": "good"}))
    ask(client, cached_context="THE CV")

    system = sdk.calls[0]["system"]
    assert system[0] == {"type": "text", "text": "Instructions"}
    assert system[1]["text"] == "THE CV"
    assert system[1]["cache_control"] == {"type": "ephemeral"}


def test_usage_is_accumulated():
    usage = {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 900}
    client, _ = make_client(json_response({"score": 7, "reason": "ok"}, usage=usage))
    ask(client)
    ask(client)

    assert client.usage.calls == 2
    assert client.usage.input_tokens == 200
    assert client.usage.cache_read_tokens == 1800
    assert "2 calls" in client.usage.describe()


# ------------------------------------------------------------- bad answers


@pytest.mark.parametrize("stop_reason, match", [("max_tokens", "cut off"), ("refusal", "declined")])
def test_incomplete_answers_raise(stop_reason, match):
    client, _ = make_client(json_response("{", stop_reason=stop_reason))
    with pytest.raises(LLMError, match=match):
        ask(client)


def test_empty_answer_raises():
    response = SimpleNamespace(content=[], stop_reason="end_turn", usage=None)
    client, _ = make_client(response)
    with pytest.raises(LLMError, match="no answer"):
        ask(client)


def test_invalid_json_raises():
    client, _ = make_client(json_response("not json"))
    with pytest.raises(LLMError, match="expected format"):
        ask(client)


# ------------------------------------------------------------------ errors


@pytest.mark.parametrize(
    "error, match",
    [
        (status_error(anthropic.AuthenticationError, 401), "ANTHROPIC_API_KEY"),
        (status_error(anthropic.PermissionDeniedError, 403), "ANTHROPIC_API_KEY"),
        (status_error(anthropic.NotFoundError, 404), "claude-sonnet-5-5"),
        (status_error(anthropic.BadRequestError, 400, "Your credit balance is too low"), "credit"),
        (status_error(anthropic.BadRequestError, 400, "output_config: invalid schema"), "rejected"),
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


def test_real_models_produce_accepted_schemas():
    """Both pipeline schemas go through the transform without unsupported keywords."""
    from jobpipeline.models import ApplicationContent, OfferAnalysis

    forbidden = {"minimum", "maximum", "minLength", "maxLength", "maxItems", "pattern"}

    def walk(node):
        if isinstance(node, dict):
            assert not forbidden & node.keys(), node
            if node.get("type") == "object":
                assert node.get("additionalProperties") is False
            if "minItems" in node:
                assert node["minItems"] in (0, 1)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(output_schema(OfferAnalysis))
    walk(output_schema(ApplicationContent))
