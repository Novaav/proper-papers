"""A scripted `Proposer` for tests: no network, no key."""

import json
from typing import Any

from api.extraction.proposer import ProviderResponse, Status


def response(
    answer: dict[str, Any] | str | None = None,
    *,
    status: Status = "ok",
    input_tokens: int = 1000,
    output_tokens: int = 200,
    latency: float = 1.5,
    model: str = "claude-sonnet-5-5",
    error: str | None = None,
) -> ProviderResponse:
    """A provider response; `answer` is a dict (sent as JSON) or raw text."""
    text = json.dumps(answer) if isinstance(answer, dict) else answer
    return ProviderResponse(
        status=status,
        text=text if status == "ok" else None,
        model=None if status == "unavailable" else model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=0,
        cache_write_tokens=0,
        latency_seconds=latency,
        error=error,
    )


def answer(**mentions: list[dict[str, Any]]) -> dict[str, Any]:
    """A complete answer: every field present, empty unless given."""
    fields = (
        "provider_name",
        "customer_name",
        "effective_date",
        "end_date",
        "governing_law",
        "contract_value",
        "notice_period_days",
    )
    return {key: mentions.get(key, []) for key in fields}


class FakeProposer:
    """Returns the given responses in order and keeps every request it was sent."""

    def __init__(self, *responses: ProviderResponse) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    def propose(self, request: dict[str, Any]) -> ProviderResponse:
        self.requests.append(request)
        return self.responses.pop(0)
