"""ACP host setup must be explicit, typed, and restricted to administrators."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock

import httpx
import pytest

from octop.api.app import build_app
from octop.infra.agents.acp_setup import ACPRunnerSetup, ACPSetupService
from octop.infra.agents.acp_setup_jobs import ACPSetupJob, ACPSetupJobs
from tests.support.auth import create_user


@pytest.fixture
async def env(env_acp_agent):
    yield env_acp_agent


async def test_setup_view_does_not_start_or_download_runners(env, monkeypatch) -> None:
    client, _server, auth, _agent_id = env

    async def unexpected_process(*args, **kwargs):
        pytest.fail("Loading setup status must not start a process or download packages")

    monkeypatch.setattr("asyncio.create_subprocess_exec", unexpected_process)
    response = await client.get("/api/acp/setup", headers={**auth, "Accept-Language": "zh"})
    assert response.status_code == 200
    result = response.json()
    assert result["host"]
    assert "codex" in result["runners"]
    assert all(row["connection"] != "ready" for row in result["runners"].values())
    assert all(row["checked_at"] is None for row in result["runners"].values())


@pytest.mark.parametrize("operation", ["setup", "codex/install", "codex/check", "codex/login"])
async def test_setup_requires_admin(env, operation) -> None:
    client, _server, admin_auth, _agent_id = env
    user_auth = await create_user(client, admin_auth, username="acp-member")
    method = client.get if operation == "setup" else client.post
    response = await method(f"/api/acp/{operation}", headers=user_auth)
    assert response.status_code == 403


async def test_install_returns_a_background_job_without_waiting_for_download(
    env, monkeypatch
) -> None:
    client, _server, auth, _agent_id = env
    job = ACPSetupJob(
        job_id="setup-test-job", runner="codex", kind="install", status="running", phase="preparing"
    )
    start = AsyncMock(return_value=job)
    monkeypatch.setattr(ACPSetupJobs, "start", start)
    response = await client.post("/api/acp/codex/install", headers=auth)
    assert response.status_code == 202
    assert response.json()["job_id"] == "setup-test-job"
    assert response.json()["status"] == "running"
    start.assert_awaited_once()


async def test_credentials_response_does_not_echo_key(env) -> None:
    client, _server, auth, _agent_id = env
    response = await client.post(
        "/api/acp/pi/credentials",
        headers=auth,
        json={"provider": "google", "api_key": "test-private-provider-key"},
    )
    assert response.status_code == 200
    assert "test-private-provider-key" not in response.text
    assert response.json()["connection"] == "unchecked"
    assert response.json()["enabled"] is False


async def test_rejected_credential_does_not_echo_key_in_validation_error(env) -> None:
    client, _server, auth, _agent_id = env
    secret = "private-key-value" * 600
    response = await client.post(
        "/api/acp/pi/credentials",
        headers=auth,
        json={"provider": "google", "api_key": secret},
    )
    assert response.status_code == 400
    assert "private-key-value" not in response.text


async def test_check_returns_explicit_auth_required_status(env, monkeypatch) -> None:
    client, _server, auth, _agent_id = env
    result = ACPRunnerSetup(
        runner="codex",
        installation="installed",
        connection="auth_required",
        enabled=False,
        can_install=False,
        install_command=None,
        login_command="codex-acp cli login --device-auth",
        docs_url="https://github.com/agentclientprotocol/codex-acp",
        message="请在运行 Octop 的服务账号下登录。",
        checked_at=1,
    )
    check = AsyncMock(return_value=result)
    monkeypatch.setattr(ACPSetupService, "check", check)
    response = await client.post("/api/acp/codex/check", headers=auth)
    assert response.status_code == 200
    assert response.json()["connection"] == "auth_required"
    assert response.json()["enabled"] is False
    assert response.json()["login_command"]
    check.assert_awaited_once()


async def test_setup_openapi_has_response_schemas(env, monkeypatch) -> None:
    _client, server, _auth, _agent_id = env
    monkeypatch.setattr(
        server,
        "services",
        replace(server.services, config=replace(server.services.config, enable_api_docs=True)),
    )
    app = build_app(server)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as docs_client:
        response = await docs_client.get("/api/openapi.json")
        docs = await docs_client.get("/api/docs")
    assert response.status_code == 200
    paths = response.json()["paths"]
    for path, method, status, model in (
        ("/api/acp/setup", "get", "200", "ACPHostSetupView"),
        ("/api/acp/{runner_name}/install", "post", "202", "ACPSetupJob"),
        ("/api/acp/{runner_name}/login", "post", "202", "ACPSetupJob"),
        ("/api/acp/{runner_name}/check", "post", "200", "ACPRunnerSetup"),
    ):
        operation = paths[path][method]
        assert operation["summary"]
        assert operation["description"]
        assert operation["responses"][status]["content"]["application/json"]["schema"][
            "$ref"
        ].endswith(f"/{model}")
    assert docs.status_code == 200
    assert "scalar" in docs.text.lower()
