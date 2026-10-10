"""The provider client (AI1): what one call sends and how its outcome is read. No network."""

from typing import Any

import anthropic
import httpx2
import pytest
from anthropic.types import Message

from api.extraction import proposer
from api.extraction.proposer import (
    MAX_TOKENS,
    AnthropicProposer,
    build_request,
    call_cost,
    configuration,
    request_sha256,
    worst_case_cost,
)

REQUEST = build_request("system prompt", ["provider_name"], {1: "Supplier: Acme Ltd"})


def message(content: list[dict[str, Any]], stop_reason: str = "end_turn", **usage: int) -> Message:
    return Message.model_validate(
        {
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-5-5",
            "content": content,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {"input_tokens": 1200, "output_tokens": 300, **usage},
        }
    )


class _Messages:
    def __init__(self, outcome: Message | Exception) -> None:
        self.outcome = outcome
        self.sent: list[dict[str, Any]] = []

    def create(self, **request: Any) -> Message:
        self.sent.append(request)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class _Client:
    def __init__(self, outcome: Message | Exception) -> None:
        self.messages = _Messages(outcome)


def live(outcome: Message | Exception) -> tuple[AnthropicProposer, _Client]:
    client = _Client(outcome)
    instance = AnthropicProposer(api_key="test-key-not-real")
    instance._client = client  # type: ignore[assignment]
    return instance, client


def test_the_client_never_retries_and_has_a_timeout() -> None:
    instance = AnthropicProposer(api_key="test-key-not-real")
    assert instance._client.max_retries == 0
    assert instance._client.timeout == proposer.TIMEOUT_SECONDS


def test_the_exact_request_is_sent_and_a_complete_answer_is_ok() -> None:
    thinking = {"type": "thinking", "thinking": "", "signature": "sig"}
    instance, client = live(message([thinking, {"type": "text", "text": '{"a": 1}'}]))
    result = instance.propose(REQUEST)
    assert client.messages.sent == [REQUEST]
    assert (result.status, result.text, result.model) == ("ok", '{"a": 1}', "claude-sonnet-5-5")
    assert (result.input_tokens, result.output_tokens) == (1200, 300)
    assert result.cost_usd == pytest.approx(1200 * 2e-6 + 300 * 10e-6)
    assert result.latency_seconds >= 0 and result.error is None


@pytest.mark.parametrize(
    ("stop_reason", "content", "status"),
    [
        ("refusal", [], "refused"),
        ("max_tokens", [{"type": "text", "text": '{"a"'}], "truncated"),
        ("model_context_window_exceeded", [], "truncated"),
        ("end_turn", [], "invalid"),
        ("end_turn", [{"type": "text", "text": "{}"}, {"type": "text", "text": "{}"}], "invalid"),
        ("pause_turn", [{"type": "text", "text": "{}"}], "invalid"),
    ],
)
def test_an_incomplete_answer_is_never_ok(
    stop_reason: str, content: list[dict[str, Any]], status: str
) -> None:
    instance, _ = live(message(content, stop_reason))
    result = instance.propose(REQUEST)
    assert (result.status, result.text) == (status, None)
    assert result.output_tokens == 300  # still accounted for


def test_a_provider_error_is_unavailable_and_reports_only_the_error_class() -> None:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    error = anthropic.APIConnectionError(message="secret detail", request=request)
    instance, _ = live(error)
    result = instance.propose(REQUEST)
    assert (result.status, result.text, result.error) == ("unavailable", None, "APIConnectionError")
    assert "secret" not in repr(result)
    assert result.cost_usd == 0


def test_cost_and_its_worst_case_bound() -> None:
    assert call_cost(1_000_000, 0) == 2.0
    assert call_cost(0, 1_000_000) == 10.0
    assert call_cost(0, 0, 1_000_000, 0) == pytest.approx(0.2)
    worst = worst_case_cost(REQUEST)
    assert worst >= call_cost(0, MAX_TOKENS)
    assert worst < 0.05


def test_the_request_hash_covers_every_byte_that_is_sent() -> None:
    other = build_request("system prompt", ["provider_name"], {1: "Supplier: Acme Ltd."})
    assert request_sha256(REQUEST) == request_sha256(dict(reversed(list(REQUEST.items()))))
    assert request_sha256(other) != request_sha256(REQUEST)


def test_the_configuration_pins_model_effort_sampling_and_schema() -> None:
    config = configuration()
    assert config["model"] == "claude-sonnet-5-5"
    assert config["effort"] == "low"
    assert config["sampling"] == "default"
    assert config["inference_geo"] == "global"
    assert config["sdk"].startswith("anthropic 1.11.")
    assert len(config["output_schema_sha256"]) == 64
