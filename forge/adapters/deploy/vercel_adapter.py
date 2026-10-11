from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any, Callable

import httpx

VERCEL_API_BASE_URL = "https://api.vercel.com"
VERCEL_DEPLOYMENTS_URL = f"{VERCEL_API_BASE_URL}/v13/deployments"
VERCEL_TOKEN_ENV_VAR = "VERCEL_TOKEN"

# `readyState` values that mean the deployment has finished, one way or
# another. Anything else (QUEUED, BUILDING, INITIALIZING, ANALYZING, and any
# value Vercel adds later) is treated as still in progress, so unrecognized
# in-progress states never get misclassified as an error.
_READY_STATE_SUCCESS = "READY"
_READY_STATE_ERROR = "ERROR"


@dataclass(frozen=True)
class DeploymentFile:
    path: str
    content: str


@dataclass(frozen=True)
class DeploymentResult:
    deployment_id: str
    url: str
    ready_state: str


class VercelDeploymentError(Exception):
    """Base class for every failure this adapter can raise."""


class VercelCredentialError(VercelDeploymentError):
    """
    Base class for failures caused by the credential itself, as opposed to
    the request or the deployment. A caller that wants to stop retrying
    rather than repeat an unrecoverable failure (e.g. Forge's `deploy_exec`
    runner) can catch this base rather than every concrete subclass.
    """


class MissingVercelTokenError(VercelCredentialError):
    """
    Raised when `deploy` is invoked but no API token is available — neither
    passed to the constructor nor present in `VERCEL_TOKEN` at call time.
    Raised before any HTTP request is attempted.
    """


class VercelAuthenticationError(VercelCredentialError):
    """
    Raised when the Vercel API itself rejects the token — a 401 or 403
    response from either the create-deployment or the poll-deployment
    endpoint. Unlike a generic `VercelAPIError`, retrying with the exact
    same token is guaranteed to fail the same way again; only a different
    (or fixed) credential can change the outcome.
    """

    def __init__(self, status_code: int, body: str) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(f"Vercel rejected the API token (status {status_code}): {body[:2000]}")


class VercelAPIError(VercelDeploymentError):
    """
    Raised for any other non-2xx response from either the create-deployment
    or the poll-deployment endpoint (i.e. not a 401/403 — see
    `VercelAuthenticationError` for those). This is a request failure, not
    a deployment outcome.
    """

    def __init__(self, status_code: int, body: str) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(f"Vercel API returned status {status_code}: {body[:2000]}")


class DeploymentFailedError(VercelDeploymentError):
    """
    Raised when polling observes `readyState == "ERROR"`. The request to
    create and then poll the deployment succeeded; the deployment itself did
    not. Distinct from `VercelAPIError`, which means the HTTP call failed.
    """

    def __init__(self, deployment_id: str, detail: str) -> None:
        self.deployment_id = deployment_id
        self.detail = detail
        super().__init__(f"Deployment {deployment_id} reached readyState=ERROR: {detail[:2000]}")


class DeploymentTimeoutError(VercelDeploymentError):
    """
    Raised when polling exceeded `poll_timeout_seconds` without the
    deployment reaching a terminal state (READY or ERROR).
    """

    def __init__(
        self, deployment_id: str, poll_timeout_seconds: float, last_ready_state: str
    ) -> None:
        self.deployment_id = deployment_id
        self.poll_timeout_seconds = poll_timeout_seconds
        self.last_ready_state = last_ready_state
        super().__init__(
            f"Deployment {deployment_id} did not reach a terminal state within "
            f"{poll_timeout_seconds}s (last readyState: {last_ready_state})"
        )


class VercelDeployAdapter:
    """
    Creates a Vercel deployment from in-memory files and polls it to
    completion over the Vercel REST API.

    This is called from a synchronous dispatch path, so it uses
    `httpx.Client` and a plain `time.sleep`-style poll loop rather than
    anything async.
    """

    def __init__(
        self,
        token: str | None = None,
        http_client: httpx.Client | None = None,
        poll_interval_seconds: float = 2.0,
        poll_timeout_seconds: float = 120.0,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        # Stored as given — resolution against the environment, and the
        # missing-token check, happen lazily in `deploy` so that
        # constructing this adapter never touches the environment or the
        # network.
        self._explicit_token = token
        self._owns_http_client = http_client is None
        self._http_client = http_client if http_client is not None else httpx.Client()
        self._poll_interval_seconds = poll_interval_seconds
        self._poll_timeout_seconds = poll_timeout_seconds
        self._sleep = sleep if sleep is not None else time.sleep

    def close(self) -> None:
        """Closes the underlying HTTP client, but only if this adapter created it itself."""
        if self._owns_http_client:
            self._http_client.close()

    def _resolve_token(self) -> str:
        token = self._explicit_token or os.environ.get(VERCEL_TOKEN_ENV_VAR)
        if not token:
            raise MissingVercelTokenError(
                "No Vercel API token available: none was passed to "
                "VercelDeployAdapter(token=...) and "
                f"${VERCEL_TOKEN_ENV_VAR} is not set."
            )
        return token

    @staticmethod
    def _build_request_body(
        *, project_name: str, files: list[DeploymentFile], target: str
    ) -> dict[str, Any]:
        return {
            "name": project_name,
            "target": target,
            "files": [{"file": f.path, "data": f.content} for f in files],
        }

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.status_code in (401, 403):
            raise VercelAuthenticationError(response.status_code, response.text)
        if response.status_code >= 300:
            raise VercelAPIError(response.status_code, response.text)

    @staticmethod
    def _extract_error_detail(payload: dict[str, Any], raw_text: str) -> str:
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if message:
                return str(message)
        return raw_text

    def deploy(
        self,
        *,
        project_name: str,
        files: list[DeploymentFile],
        target: str = "production",
    ) -> DeploymentResult:
        token = self._resolve_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        body = self._build_request_body(project_name=project_name, files=files, target=target)
        # Required for a brand-new project's first deployment: without it,
        # Vercel rejects the request with `missing_project_settings` since
        # it has no prior framework-detection answer on file for a project
        # that doesn't exist yet. This adapter always deploys a fresh,
        # disposable project name (see deploy_exec.py), so this is always
        # the first deployment for that project — accepting Vercel's own
        # automatic framework detection is the right default every time.
        create_response = self._http_client.post(
            VERCEL_DEPLOYMENTS_URL,
            json=body,
            headers=headers,
            params={"skipAutoDetectionConfirmation": "1"},
        )
        self._raise_for_status(create_response)

        current_payload = create_response.json()
        deployment_id = current_payload["id"]
        url = current_payload.get("url", "")
        ready_state = current_payload.get("readyState", "")

        elapsed_seconds = 0.0
        while True:
            if ready_state == _READY_STATE_SUCCESS:
                return DeploymentResult(
                    deployment_id=deployment_id, url=url, ready_state=ready_state
                )
            if ready_state == _READY_STATE_ERROR:
                raise DeploymentFailedError(
                    deployment_id,
                    self._extract_error_detail(current_payload, json.dumps(current_payload)),
                )

            if elapsed_seconds >= self._poll_timeout_seconds:
                raise DeploymentTimeoutError(deployment_id, self._poll_timeout_seconds, ready_state)

            self._sleep(self._poll_interval_seconds)
            elapsed_seconds += self._poll_interval_seconds

            poll_response = self._http_client.get(
                f"{VERCEL_DEPLOYMENTS_URL}/{deployment_id}", headers=headers
            )
            self._raise_for_status(poll_response)

            current_payload = poll_response.json()
            url = current_payload.get("url", url)
            ready_state = current_payload.get("readyState", "")
