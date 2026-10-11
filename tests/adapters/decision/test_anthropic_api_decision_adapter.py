from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Callable

import httpx
import pytest
from pydantic import BaseModel

from forge.adapters.decision.anthropic_api_decision_adapter import (
    AnthropicAPIDecisionAdapter,
    AnthropicAPIStatusError,
    DecisionAdapterRetriesExhaustedError,
    MalformedDecisionOutputError,
    MissingAPIKeyError,
)
from forge.models.decision import DecisionAction, DecisionProposal, GraphRevisionProposal


class Verdict(BaseModel):
    approved: bool
    reason: str


class FakeSleep:
    """Records every backoff duration it was awaited with, instead of actually sleeping."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


class CountingHandler:
    """Wraps a list of per-call httpx.Response factories, one per expected request."""

    def __init__(self, responses: list[Callable[[httpx.Request], httpx.Response]]) -> None:
        self._responses = responses
        self.call_count = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        index = min(self.call_count, len(self._responses) - 1)
        self.call_count += 1
        return self._responses[index](request)


def _tool_use_response(tool_name: str, input_obj: dict, status_code: int = 200) -> httpx.Response:
    body = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-5",
        "content": [{"type": "tool_use", "id": "toolu_1", "name": tool_name, "input": input_obj}],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 10, "output_tokens": 10},
    }
    return httpx.Response(status_code, json=body)


def _make_adapter(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    api_key: str | None = "test-key",
    max_retries: int = 3,
    sleep: FakeSleep | None = None,
) -> tuple[AnthropicAPIDecisionAdapter, httpx.AsyncClient]:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    adapter = AnthropicAPIDecisionAdapter(
        api_key=api_key,
        http_client=client,
        max_retries=max_retries,
        sleep=sleep,
    )
    return adapter, client


def _run(coro):
    return asyncio.run(coro)


# --- success path -----------------------------------------------------------


def test_successful_call_parses_structured_output():
    handler = CountingHandler(
        [lambda req: _tool_use_response("respond", {"approved": True, "reason": "looks good"})]
    )
    adapter, _ = _make_adapter(handler)

    result = _run(
        adapter.call_structured(
            model="claude-sonnet-5",
            system_prompt="sys",
            user_prompt="user",
            output_schema=Verdict,
        )
    )

    assert isinstance(result, Verdict)
    assert result.approved is True
    assert result.reason == "looks good"
    assert handler.call_count == 1


# --- missing API key: no request attempted ----------------------------------


def test_missing_api_key_raises_before_any_request(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    handler = CountingHandler(
        [lambda req: _tool_use_response("respond", {"approved": True, "reason": "x"})]
    )
    adapter, _ = _make_adapter(handler, api_key=None)

    with pytest.raises(MissingAPIKeyError):
        _run(
            adapter.call_structured(
                model="claude-sonnet-5",
                system_prompt="sys",
                user_prompt="user",
                output_schema=Verdict,
            )
        )

    assert handler.call_count == 0


# --- malformed output: no tool_use block -------------------------------------


def test_no_tool_use_block_raises_malformed_output_error():
    def handler_fn(req: httpx.Request) -> httpx.Response:
        body = {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "I decided not to call a tool."}],
            "stop_reason": "end_turn",
        }
        return httpx.Response(200, json=body)

    handler = CountingHandler([handler_fn])
    adapter, _ = _make_adapter(handler)

    with pytest.raises(MalformedDecisionOutputError) as exc_info:
        _run(
            adapter.call_structured(
                model="claude-sonnet-5",
                system_prompt="sys",
                user_prompt="user",
                output_schema=Verdict,
            )
        )

    assert "tool_use" in str(exc_info.value)
    assert "I decided not to call a tool" in str(exc_info.value)
    assert handler.call_count == 1


# --- malformed output: schema validation failure -----------------------------


def test_tool_use_input_failing_validation_raises_malformed_output_error():
    handler = CountingHandler([lambda req: _tool_use_response("respond", {"approved": True})])
    adapter, _ = _make_adapter(handler)

    with pytest.raises(MalformedDecisionOutputError) as exc_info:
        _run(
            adapter.call_structured(
                model="claude-sonnet-5",
                system_prompt="sys",
                user_prompt="user",
                output_schema=Verdict,
            )
        )

    assert "reason" in str(exc_info.value)
    assert handler.call_count == 1


# --- retry on transient failure, then succeed --------------------------------


def test_transient_429_then_success_retries_exactly_once():
    handler = CountingHandler(
        [
            lambda req: httpx.Response(429, json={"error": {"message": "rate limited"}}),
            lambda req: _tool_use_response("respond", {"approved": False, "reason": "no"}),
        ]
    )
    sleep = FakeSleep()
    adapter, _ = _make_adapter(handler, sleep=sleep)

    result = _run(
        adapter.call_structured(
            model="claude-sonnet-5",
            system_prompt="sys",
            user_prompt="user",
            output_schema=Verdict,
        )
    )

    assert result == Verdict(approved=False, reason="no")
    assert handler.call_count == 2
    assert len(sleep.calls) == 1
    assert sleep.calls[0] == pytest.approx(0.5)


# --- retries exhausted --------------------------------------------------------


def test_all_attempts_retryable_status_exhausts_retries():
    handler = CountingHandler(
        [lambda req: httpx.Response(503, json={"error": {"message": "overloaded"}})]
    )
    sleep = FakeSleep()
    adapter, _ = _make_adapter(handler, max_retries=3, sleep=sleep)

    with pytest.raises(DecisionAdapterRetriesExhaustedError) as exc_info:
        _run(
            adapter.call_structured(
                model="claude-sonnet-5",
                system_prompt="sys",
                user_prompt="user",
                output_schema=Verdict,
            )
        )

    assert handler.call_count == 3
    assert len(sleep.calls) == 2
    assert "503" in str(exc_info.value) or "overloaded" in str(exc_info.value)


# --- non-retryable failure raises immediately --------------------------------


def test_non_retryable_400_raises_immediately():
    handler = CountingHandler(
        [lambda req: httpx.Response(400, json={"error": {"message": "bad request: missing field"}})]
    )
    sleep = FakeSleep()
    adapter, _ = _make_adapter(handler, sleep=sleep)

    with pytest.raises(AnthropicAPIStatusError) as exc_info:
        _run(
            adapter.call_structured(
                model="claude-sonnet-5",
                system_prompt="sys",
                user_prompt="user",
                output_schema=Verdict,
            )
        )

    assert handler.call_count == 1
    assert len(sleep.calls) == 0
    assert "400" in str(exc_info.value)


# --- end-to-end with the real DecisionProposal schema ------------------------


def test_decision_proposal_round_trips_through_schema_and_back():
    proposal = DecisionProposal(
        id="dp-1",
        decision_node_type="retry_decision",
        action=DecisionAction.PROPOSE_GRAPH_REVISION,
        target_task_ids=["t1", "t2"],
        retry_feedback=None,
        escalate_to_model="claude-opus-5",
        graph_revision=GraphRevisionProposal(
            base_graph_version_id="gv-1",
            add_tasks=[],
            add_edges=[],
            remove_task_ids=[],
            rationale="upstream evidence invalidated by new information",
            invalidated_evidence_ids=["ev-0"],
        ),
        user_question=None,
        confidence=0.87,
        evidence_refs=["ev-1", "ev-2"],
        model_used="claude-sonnet-5",
        created_at=datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
    )
    dumped_input = proposal.model_dump(mode="json")

    handler = CountingHandler([lambda req: _tool_use_response("submit_decision", dumped_input)])
    adapter, _ = _make_adapter(handler)

    result = _run(
        adapter.call_structured(
            model="claude-sonnet-5",
            system_prompt="You are a decision node.",
            user_prompt="Decide what to do next.",
            output_schema=DecisionProposal,
            tool_name="submit_decision",
        )
    )

    assert isinstance(result, DecisionProposal)
    assert result == proposal
    assert handler.call_count == 1


# --- request shape sanity (headers, forced tool_choice) ----------------------


def test_request_sends_forced_tool_choice_and_required_headers():
    captured: dict = {}

    def handler_fn(req: httpx.Request) -> httpx.Response:
        captured["headers"] = req.headers
        captured["body"] = json.loads(req.content)
        return _tool_use_response("respond", {"approved": True, "reason": "ok"})

    handler = CountingHandler([handler_fn])
    adapter, _ = _make_adapter(handler, api_key="my-secret-key")

    _run(
        adapter.call_structured(
            model="claude-sonnet-5",
            system_prompt="sys",
            user_prompt="user",
            output_schema=Verdict,
            tool_name="respond",
            max_tokens=512,
        )
    )

    assert captured["headers"]["x-api-key"] == "my-secret-key"
    assert captured["headers"]["anthropic-version"] == "2023-06-01"
    assert captured["headers"]["content-type"] == "application/json"
    body = captured["body"]
    assert body["model"] == "claude-sonnet-5"
    assert body["max_tokens"] == 512
    assert body["system"] == "sys"
    assert body["messages"] == [{"role": "user", "content": "user"}]
    assert body["tool_choice"] == {"type": "tool", "name": "respond"}
    assert body["tools"][0]["name"] == "respond"
    assert "input_schema" in body["tools"][0]
