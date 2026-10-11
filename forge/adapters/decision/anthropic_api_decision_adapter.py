from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Awaitable, Callable, Optional

import httpx
from pydantic import BaseModel, ValidationError

ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_API_VERSION = "2023-06-01"
ANTHROPIC_API_KEY_ENV_VAR = "ANTHROPIC_API_KEY"

# HTTP statuses treated as transient — worth a retry with backoff rather than
# an immediate failure. Anything else (400/401/403/404/...) is a request or
# auth problem that another attempt cannot fix, so it is raised immediately.
_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 529})

_BASE_BACKOFF_SECONDS = 0.5  # yields 0.5s, 1s, 2s, 4s, ... between attempts


class DecisionAdapterError(Exception):
    """Base class for every failure this adapter can raise."""


class MissingAPIKeyError(DecisionAdapterError):
    """
    Raised when `call_structured` is invoked but no API key is available —
    neither passed to the constructor nor present in `ANTHROPIC_API_KEY` at
    call time. Raised before any HTTP request is attempted.
    """


class MalformedDecisionOutputError(DecisionAdapterError):
    """
    Raised when a successful HTTP response can't be turned into
    `output_schema`: no `tool_use` content block in the response, or its
    `input` fails `output_schema.model_validate`. The adapter never falls
    back to a default or partially-constructed instance in either case.
    """


class AnthropicAPIStatusError(DecisionAdapterError):
    """
    Raised immediately for a non-retryable HTTP status (anything outside
    `_RETRYABLE_STATUS_CODES`, e.g. 400/401/403/404) — one more attempt
    would fail the same way, so this is not retried.
    """

    def __init__(self, status_code: int, body: str) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(
            f"Anthropic API returned non-retryable status {status_code}: {body[:2000]}"
        )


class DecisionAdapterRetriesExhaustedError(DecisionAdapterError):
    """
    Raised when every attempt (up to `max_retries`) failed with a transient
    error — either a retryable HTTP status or a network-level timeout/
    connect failure. Carries a description of what was tried and the last
    failure so the caller can debug without re-running the request.
    """

    def __init__(self, max_retries: int, model: str, last_error: str) -> None:
        self.max_retries = max_retries
        self.last_error = last_error
        super().__init__(
            f"Anthropic API call for model '{model}' failed after {max_retries} "
            f"attempt(s); last failure: {last_error}"
        )


def _is_transient_status(status_code: int) -> bool:
    return status_code in _RETRYABLE_STATUS_CODES


def _backoff_seconds(attempt_number: int) -> float:
    """`attempt_number` is 1-based: attempt 1's backoff is 0.5s, attempt 2's is 1s, etc."""
    return _BASE_BACKOFF_SECONDS * (2 ** (attempt_number - 1))


class AnthropicAPIDecisionAdapter:
    """
    Calls the Anthropic Messages API directly (no SDK) with forced
    structured output: the model is given exactly one tool shaped by a
    caller-supplied Pydantic schema, `tool_choice` forces that tool, and the
    resulting `tool_use.input` is parsed straight back into that schema.

    This class is intentionally generic — it has no knowledge of any
    particular output schema (e.g. `DecisionProposal`). `output_schema` is a
    parameter of `call_structured` precisely so any typed contract can be
    the target; callers that need decision-node output pass
    `output_schema=DecisionProposal` themselves.
    """

    def __init__(
        self,
        api_key: str | None = None,
        http_client: httpx.AsyncClient | None = None,
        max_retries: int = 3,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        # Stored as given — resolution against the environment, and the
        # missing-key check, happen lazily in `call_structured` so that
        # constructing this adapter never touches the environment or the
        # network.
        self._explicit_api_key = api_key
        self._owns_http_client = http_client is None
        self._http_client = http_client if http_client is not None else httpx.AsyncClient()
        self._max_retries = max_retries
        self._sleep = sleep if sleep is not None else asyncio.sleep

    async def aclose(self) -> None:
        """Closes the underlying HTTP client, but only if this adapter created it itself."""
        if self._owns_http_client:
            await self._http_client.aclose()

    def _resolve_api_key(self) -> str:
        api_key = self._explicit_api_key or os.environ.get(ANTHROPIC_API_KEY_ENV_VAR)
        if not api_key:
            raise MissingAPIKeyError(
                "No Anthropic API key available: none was passed to "
                "AnthropicAPIDecisionAdapter(api_key=...) and "
                f"${ANTHROPIC_API_KEY_ENV_VAR} is not set."
            )
        return api_key

    @staticmethod
    def _build_request_body(
        *,
        model: str,
        system_prompt: str,
        user_prompt: str,
        output_schema: type[BaseModel],
        tool_name: str,
        max_tokens: int,
    ) -> dict[str, Any]:
        input_schema = output_schema.model_json_schema()
        return {
            "model": model,
            "max_tokens": max_tokens,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
            "tools": [
                {
                    "name": tool_name,
                    "description": (
                        f"Submit the structured response. Arguments must conform exactly "
                        f"to the provided schema for '{output_schema.__name__}'."
                    ),
                    "input_schema": input_schema,
                }
            ],
            "tool_choice": {"type": "tool", "name": tool_name},
        }

    @staticmethod
    def _parse_tool_use_response(
        response: httpx.Response, output_schema: type[BaseModel], tool_name: str
    ) -> BaseModel:
        raw_text = response.text
        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise MalformedDecisionOutputError(
                f"Anthropic API response was not valid JSON: {exc}. "
                f"Raw response (truncated): {raw_text[:2000]}"
            ) from exc

        content_blocks = payload.get("content", [])
        tool_use_block = next(
            (block for block in content_blocks if block.get("type") == "tool_use"),
            None,
        )
        if tool_use_block is None:
            raise MalformedDecisionOutputError(
                f"No tool_use content block found in Anthropic API response for forced "
                f"tool '{tool_name}'. Raw response (truncated): {raw_text[:2000]}"
            )

        try:
            return output_schema.model_validate(tool_use_block.get("input"))
        except ValidationError as exc:
            raise MalformedDecisionOutputError(
                f"tool_use.input for tool '{tool_name}' failed validation against "
                f"{output_schema.__name__}: {exc}. Raw tool_use block: "
                f"{json.dumps(tool_use_block)[:2000]}"
            ) from exc

    async def call_structured(
        self,
        *,
        model: str,
        system_prompt: str,
        user_prompt: str,
        output_schema: type[BaseModel],
        tool_name: str = "respond",
        max_tokens: int = 1024,
    ) -> BaseModel:
        """
        Calls the Anthropic Messages API with a single forced tool shaped by
        `output_schema`, and returns the parsed, validated model instance.

        Retries up to `max_retries` attempts total on transient failures
        (HTTP 429/500/502/503/529, or a connect/timeout error), with
        exponential backoff between attempts (`self._sleep` is awaited, not
        real time, so callers can inject a fast/no-op sleep in tests). Any
        other HTTP error status is raised immediately, without retrying.
        """
        api_key = self._resolve_api_key()
        body = self._build_request_body(
            model=model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            output_schema=output_schema,
            tool_name=tool_name,
            max_tokens=max_tokens,
        )
        headers = {
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_API_VERSION,
            "content-type": "application/json",
        }

        last_error: str = "no attempt was made"
        for attempt_number in range(1, self._max_retries + 1):
            try:
                response = await self._http_client.post(
                    ANTHROPIC_MESSAGES_URL, json=body, headers=headers
                )
            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                last_error = f"attempt {attempt_number}: network error: {exc!r}"
            else:
                if _is_transient_status(response.status_code):
                    last_error = (
                        f"attempt {attempt_number}: HTTP {response.status_code}: "
                        f"{response.text[:2000]}"
                    )
                elif response.status_code >= 400:
                    raise AnthropicAPIStatusError(response.status_code, response.text)
                else:
                    return self._parse_tool_use_response(response, output_schema, tool_name)

            if attempt_number < self._max_retries:
                await self._sleep(_backoff_seconds(attempt_number))

        raise DecisionAdapterRetriesExhaustedError(
            max_retries=self._max_retries, model=model, last_error=last_error
        )
