"""The model call behind assisted extraction (AI1, architecture B').

A `Proposer` sends one exact request and returns what came back, unparsed: the
response text, how the call ended, the tokens it used and how long it took. It
never interprets the answer; `api.extraction.assisted` does, and trusts nothing
in it. The request is a plain dictionary built here, so the same bytes are
sent live, hashed for the recorded-response replay and priced before a call.

Configuration is fixed in code and part of the extractor version: one pinned
model, low effort, the model's default sampling (no temperature: the model
rejects a non-default value), strict JSON output through `output_config.format`,
a bounded response and global inference. The SDK does not retry: a failed call
is reported as `unavailable`, never repeated silently.
"""

import hashlib
import json
import time
from dataclasses import dataclass
from importlib.metadata import version as package_version
from typing import Any, Literal, Protocol

import anthropic

from api.extraction.contract import FIELDS, FieldKey

MODEL = "claude-sonnet-5-5"
EFFORT = "low"
MAX_TOKENS = 1024
INFERENCE_GEO = "global"
TIMEOUT_SECONDS = 30.0
# USD per million tokens for MODEL at global inference, from Anthropic's published
# pricing (checked 1 Oct 2026). Used to compute the cost of every call.
PRICE_INPUT = 2.00
PRICE_OUTPUT = 10.00
PRICE_CACHE_READ = 0.20
PRICE_CACHE_WRITE = 2.50

_MENTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "page": {"type": "integer"},
        "quote": {"type": "string"},
        "raw": {"type": "string"},
    },
    "required": ["page", "quote", "raw"],
    "additionalProperties": False,
}
# One list of mentions per contract field, every field present. Caps (mentions per
# field, quote length) are enforced when the answer is verified, not in the schema.
OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {key: {"type": "array", "items": _MENTION_SCHEMA} for key in FIELDS},
    "required": list(FIELDS),
    "additionalProperties": False,
}

Status = Literal["ok", "refused", "truncated", "invalid", "unavailable"]


@dataclass(frozen=True)
class ProviderResponse:
    """What one call returned; `text` only when `status` is `ok`."""

    status: Status
    text: str | None
    model: str | None
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    latency_seconds: float
    error: str | None = None  # an exception class name, never a message

    @property
    def cost_usd(self) -> float:
        return call_cost(
            self.input_tokens, self.output_tokens, self.cache_read_tokens, self.cache_write_tokens
        )


class Proposer(Protocol):
    def propose(self, request: dict[str, Any]) -> ProviderResponse: ...


def build_request(
    system_prompt: str, requested: list[FieldKey], pages: dict[int, str]
) -> dict[str, Any]:
    """The exact Messages API request: document text travels as JSON data only."""
    document = {
        "requested_fields": requested,
        "pages": [{"page": number, "text": text} for number, text in sorted(pages.items())],
    }
    return {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "inference_geo": INFERENCE_GEO,
        "output_config": {
            "effort": EFFORT,
            "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
        },
        "system": system_prompt,
        "messages": [
            {"role": "user", "content": json.dumps(document, ensure_ascii=False, indent=1)}
        ],
    }


def request_sha256(request: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(request)).hexdigest()


def configuration() -> dict[str, Any]:
    """Everything besides the prompt that decides what is sent (hashed into the version)."""
    return {
        "model": MODEL,
        "effort": EFFORT,
        "max_tokens": MAX_TOKENS,
        "inference_geo": INFERENCE_GEO,
        "sampling": "default",
        "output_schema_sha256": hashlib.sha256(_canonical(OUTPUT_SCHEMA)).hexdigest(),
        "sdk": f"anthropic {package_version('anthropic')}",
    }


def call_cost(
    input_tokens: int, output_tokens: int, cache_read_tokens: int = 0, cache_write_tokens: int = 0
) -> float:
    return (
        input_tokens * PRICE_INPUT
        + output_tokens * PRICE_OUTPUT
        + cache_read_tokens * PRICE_CACHE_READ
        + cache_write_tokens * PRICE_CACHE_WRITE
    ) / 1_000_000


def worst_case_cost(request: dict[str, Any]) -> float:
    """An upper bound on one call's cost, before it is made.

    Input tokens are bounded by the request's UTF-8 size plus a fixed allowance for
    what the API adds (the structured-output instructions); output by max_tokens.
    """
    input_bound = len(_canonical(request)) + 2000
    return call_cost(input_bound, int(request["max_tokens"]))


class AnthropicProposer:
    """The live call. The API key is passed in; it is never read from or written to logs."""

    def __init__(self, api_key: str) -> None:
        self._client = anthropic.Anthropic(api_key=api_key, timeout=TIMEOUT_SECONDS, max_retries=0)

    def propose(self, request: dict[str, Any]) -> ProviderResponse:
        started = time.monotonic()
        try:
            message = self._client.messages.create(**request)
        except anthropic.AnthropicError as error:
            return ProviderResponse(
                status="unavailable",
                text=None,
                model=None,
                input_tokens=0,
                output_tokens=0,
                cache_read_tokens=0,
                cache_write_tokens=0,
                latency_seconds=round(time.monotonic() - started, 3),
                error=type(error).__name__,
            )
        latency = round(time.monotonic() - started, 3)
        texts = [block.text for block in message.content if block.type == "text"]
        status: Status
        if message.stop_reason == "refusal":
            status = "refused"
        elif message.stop_reason in ("max_tokens", "model_context_window_exceeded"):
            status = "truncated"
        elif message.stop_reason == "end_turn" and len(texts) == 1:
            status = "ok"
        else:
            status = "invalid"
        usage = message.usage
        return ProviderResponse(
            status=status,
            text=texts[0] if status == "ok" else None,
            model=message.model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_input_tokens or 0,
            cache_write_tokens=usage.cache_creation_input_tokens or 0,
            latency_seconds=latency,
        )


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
