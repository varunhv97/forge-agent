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
    VercelAuthenticationError,
    VercelCredentialError,
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
    poll_count = {"n": 0}
    ready_states = ["QUEUED", "BUILDING", "READY"]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                json={"id": "dpl_123", "url": "my-app.vercel.app", "readyState": ready_states[0]},
            )
        if request.method == "PATCH":
            return httpx.Response(200, json={})
        poll_count["n"] += 1
        index = min(poll_count["n"], len(ready_states) - 1)
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
    # 2 poll calls (BUILDING, then READY) after the create+disable-protection calls.
    assert poll_count["n"] == 2
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
        return httpx.Response(400, text="bad request: malformed files payload")

    adapter, _ = _make_adapter(handler)

    with pytest.raises(VercelAPIError) as exc_info:
        adapter.deploy(
            project_name="my-app",
            files=[DeploymentFile(path="index.html", content="<h1>hi</h1>")],
        )

    assert exc_info.value.status_code == 400
    assert "bad request" in exc_info.value.body


@pytest.mark.parametrize("status_code", [401, 403])
def test_401_or_403_create_response_raises_vercel_authentication_error(status_code):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"error": {"invalidToken": True}})

    adapter, _ = _make_adapter(handler)

    with pytest.raises(VercelAuthenticationError) as exc_info:
        adapter.deploy(
            project_name="my-app",
            files=[DeploymentFile(path="index.html", content="<h1>hi</h1>")],
        )

    assert exc_info.value.status_code == status_code
    assert isinstance(exc_info.value, VercelCredentialError)


def test_401_or_403_poll_response_raises_vercel_authentication_error():
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if request.method == "POST":
            return httpx.Response(
                200, json={"id": "dpl_123", "url": "my-app.vercel.app", "readyState": "BUILDING"}
            )
        return httpx.Response(403, json={"error": {"invalidToken": True}})

    adapter, _ = _make_adapter(handler)

    with pytest.raises(VercelAuthenticationError):
        adapter.deploy(
            project_name="my-app",
            files=[DeploymentFile(path="index.html", content="<h1>hi</h1>")],
        )


def test_missing_vercel_token_error_is_a_credential_error():
    assert issubclass(MissingVercelTokenError, VercelCredentialError)


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
        if request.method == "PATCH":
            return httpx.Response(200, json={})
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

    create_requests = [r for r in captured_requests if r.method == "POST"]
    assert len(create_requests) == 1
    request = create_requests[0]
    assert request.url == httpx.URL(
        "https://api.vercel.com/v13/deployments?skipAutoDetectionConfirmation=1"
    )
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
        if request.method == "PATCH":
            return httpx.Response(200, json={})
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


def test_disable_deployment_protection_sends_correct_patch_request():
    captured_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        if request.method == "POST":
            return httpx.Response(
                200, json={"id": "dpl_prot", "url": "my-app.vercel.app", "readyState": "READY"}
            )
        return httpx.Response(200, json={})

    adapter, _ = _make_adapter(handler)
    adapter.deploy(
        project_name="my-app",
        files=[DeploymentFile(path="index.html", content="<h1>hi</h1>")],
    )

    patch_requests = [r for r in captured_requests if r.method == "PATCH"]
    assert len(patch_requests) == 1
    request = patch_requests[0]
    assert request.url == httpx.URL("https://api.vercel.com/v9/projects/my-app")
    assert request.headers["authorization"] == "Bearer fake-token"
    assert json.loads(request.content) == {"ssoProtection": None}


def test_disable_deployment_protection_failure_raises_before_polling():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200, json={"id": "dpl_prot", "url": "my-app.vercel.app", "readyState": "BUILDING"}
            )
        if request.method == "PATCH":
            return httpx.Response(500, text="internal error")
        raise AssertionError("no poll call expected when disabling protection already failed")

    adapter, _ = _make_adapter(handler)

    with pytest.raises(VercelAPIError) as exc_info:
        adapter.deploy(
            project_name="my-app",
            files=[DeploymentFile(path="index.html", content="<h1>hi</h1>")],
        )

    assert exc_info.value.status_code == 500


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
