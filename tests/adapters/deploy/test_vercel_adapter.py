from __future__ import annotations

import json

import httpx
import pytest

from forge.adapters.deploy.vercel_adapter import (
    DeploymentFailedError,
    DeploymentFile,
    DeploymentTimeoutError,
    MissingVercelTokenError,
    VercelAPIError,
    VercelDeployAdapter,
)


class _FakeSleep:
    """Records sleep calls instead of actually sleeping, for deterministic tests."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def _make_adapter(handler, **kwargs) -> tuple[VercelDeployAdapter, _FakeSleep]:
    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(transport=transport)
    fake_sleep = _FakeSleep()
    adapter = VercelDeployAdapter(
        token="fake-token",
        http_client=http_client,
        sleep=fake_sleep,
        **kwargs,
    )
    return adapter, fake_sleep


def test_successful_deploy_polls_until_ready():
    call_count = {"n": 0}
    ready_states = ["QUEUED", "BUILDING", "READY"]

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        index = min(call_count["n"] - 1, len(ready_states) - 1)
        return httpx.Response(
            200,
            json={"id": "dpl_123", "url": "my-app.vercel.app", "readyState": ready_states[index]},
        )

    adapter, fake_sleep = _make_adapter(handler, poll_interval_seconds=1.0)

    result = adapter.deploy(
        project_name="my-app",
        files=[DeploymentFile(path="index.html", content="<h1>hi</h1>")],
    )

    assert result.deployment_id == "dpl_123"
    assert result.url == "my-app.vercel.app"
    assert result.ready_state == "READY"
    # 1 create call + 2 poll calls (BUILDING, then READY).
    assert call_count["n"] == 3
    assert fake_sleep.calls == [1.0, 1.0]


def test_missing_token_raises_before_any_http_call():
    invoked = {"called": False}

    def handler(request: httpx.Request) -> httpx.Response:
        invoked["called"] = True
        return httpx.Response(200, json={})

    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(transport=transport)
    adapter = VercelDeployAdapter(token=None, http_client=http_client, sleep=_FakeSleep())

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.delenv("VERCEL_TOKEN", raising=False)
        with pytest.raises(MissingVercelTokenError):
            adapter.deploy(
                project_name="my-app",
                files=[DeploymentFile(path="index.html", content="<h1>hi</h1>")],
            )

    assert invoked["called"] is False


def test_non_2xx_create_response_raises_vercel_api_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden: invalid token")

    adapter, _ = _make_adapter(handler)

    with pytest.raises(VercelAPIError) as exc_info:
        adapter.deploy(
            project_name="my-app",
            files=[DeploymentFile(path="index.html", content="<h1>hi</h1>")],
        )

    assert exc_info.value.status_code == 403
    assert "forbidden" in exc_info.value.body


def test_error_ready_state_raises_deployment_failed_error():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "id": "dpl_err",
                    "url": "my-app.vercel.app",
                    "readyState": "ERROR",
                    "error": {"message": "build failed: missing package.json"},
                },
            )
        raise AssertionError("no poll call expected when create response is already ERROR")

    adapter, _ = _make_adapter(handler)

    with pytest.raises(DeploymentFailedError) as exc_info:
        adapter.deploy(
            project_name="my-app",
            files=[DeploymentFile(path="index.html", content="<h1>hi</h1>")],
        )

    assert exc_info.value.deployment_id == "dpl_err"
    assert "missing package.json" in exc_info.value.detail


def test_polling_timeout_raises_deployment_timeout_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"id": "dpl_slow", "url": "my-app.vercel.app", "readyState": "BUILDING"}
        )

    adapter, fake_sleep = _make_adapter(
        handler, poll_interval_seconds=2.0, poll_timeout_seconds=3.0
    )

    with pytest.raises(DeploymentTimeoutError) as exc_info:
        adapter.deploy(
            project_name="my-app",
            files=[DeploymentFile(path="index.html", content="<h1>hi</h1>")],
        )

    assert exc_info.value.deployment_id == "dpl_slow"
    assert exc_info.value.poll_timeout_seconds == 3.0
    # The fake sleep means this test runs instantly, not for real seconds.
    assert len(fake_sleep.calls) >= 1
    assert all(call == 2.0 for call in fake_sleep.calls)


def test_create_request_body_shape():
    captured_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(
            200, json={"id": "dpl_shape", "url": "my-app.vercel.app", "readyState": "READY"}
        )

    adapter, _ = _make_adapter(handler)

    files = [
        DeploymentFile(path="index.html", content="<h1>hi</h1>"),
        DeploymentFile(path="api/hello.py", content="def handler(): return 'hi'"),
    ]
    adapter.deploy(project_name="my-app", files=files, target="production")

    assert len(captured_requests) == 1
    request = captured_requests[0]
    assert request.method == "POST"
    assert request.url == httpx.URL("https://api.vercel.com/v13/deployments")
    assert request.headers["authorization"] == "Bearer fake-token"

    body = json.loads(request.content)
    assert body == {
        "name": "my-app",
        "target": "production",
        "files": [
            {"file": "index.html", "data": "<h1>hi</h1>"},
            {"file": "api/hello.py", "data": "def handler(): return 'hi'"},
        ],
    }


def test_poll_request_uses_deployment_id_and_bearer_auth():
    captured_poll_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200, json={"id": "dpl_poll", "url": "my-app.vercel.app", "readyState": "BUILDING"}
            )
        captured_poll_requests.append(request)
        return httpx.Response(
            200, json={"id": "dpl_poll", "url": "my-app.vercel.app", "readyState": "READY"}
        )

    adapter, _ = _make_adapter(handler, poll_interval_seconds=0.5)
    adapter.deploy(
        project_name="my-app",
        files=[DeploymentFile(path="index.html", content="<h1>hi</h1>")],
    )

    assert len(captured_poll_requests) == 1
    poll_request = captured_poll_requests[0]
    assert poll_request.method == "GET"
    assert poll_request.url == httpx.URL("https://api.vercel.com/v13/deployments/dpl_poll")
    assert poll_request.headers["authorization"] == "Bearer fake-token"


def test_adapter_only_closes_http_client_it_owns():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    injected_client = httpx.Client(transport=transport)
    adapter = VercelDeployAdapter(token="fake-token", http_client=injected_client)

    adapter.close()

    assert injected_client.is_closed is False
    injected_client.close()

    owned_adapter = VercelDeployAdapter(token="fake-token")
    owned_adapter.close()
    assert owned_adapter._http_client.is_closed is True
