"""Automatic Task failover preserves admission choices and records the execution route."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain.agents.middleware import ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, HumanMessage

from octop.config import OctopConfig
from octop.infra.agents.middleware import model_failover as middleware_module
from octop.infra.agents.providers.health import ModelHealthRegistry
from octop.infra.agents.providers.store import ProviderStore
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.project_task_turns import ProjectTaskTurnRow
from octop.infra.db.services import RepoBundle, SharedServices
from octop.infra.projects import model_failover
from octop.infra.utils.paths import PathLayout


@pytest.fixture
def environment(tmp_path, monkeypatch):
    monkeypatch.setenv("OCTOP_HOME", str(tmp_path))
    db = SqlitePool(tmp_path / "failover.sqlite")
    run_migrations(db)
    repos = RepoBundle.from_pool(db)
    for name, kind, ids in (
        ("box", "box", ["local", "spare"]),
        ("cloud", "openai", ["remote"]),
    ):
        repos.provider_repo.create(
            name=name,
            kind=kind,
            api_key="" if kind == "box" else "test-secret",
            base_url=f"http://{name}.invalid/v1",
            models_json=json.dumps(
                [{"id": mid, "enabled": True, "context_window": 100_000} for mid in ids]
            ),
        )
    raw = {"execution_options": {}, "expert_profile": None}
    with db.transaction() as conn:
        conn.execute("INSERT INTO users(id,username,role,created_at) VALUES(1,'owner','user',1)")
        conn.execute(
            "INSERT INTO project_tasks(task_id,owner_id,title,request_id,active_turn_id,"
            "created_at,updated_at) VALUES('task-one',1,'Work','request-one','turn-one',100,100)"
        )
        repos.project_task_turn_repo.insert(
            conn,
            ProjectTaskTurnRow(
                turn_id="turn-one",
                task_id="task-one",
                request_id="turn-request",
                prompt="Continue",
                reply="",
                status="queued",
                error_code="",
                activity="",
                configuration_json=json.dumps(raw),
                model_ref="box/local",
                worker_id="",
                cancel_requested=False,
                created_at=100,
                updated_at=100,
            ),
        )
    turn = repos.project_task_turn_repo.claim("worker-one", 1_000)
    services = SharedServices(PathLayout(tmp_path), OctopConfig(), repos)
    models = {
        ref: SimpleNamespace(model=ref, profile={"max_input_tokens": 100_000})
        for ref in ("box/local", "box/spare", "cloud/remote")
    }
    health = ModelHealthRegistry()
    monkeypatch.setattr(model_failover, "model_health_for", lambda _repo: health)
    monkeypatch.setattr(
        model_failover,
        "OctopChatModelFactory",
        lambda _providers: SimpleNamespace(get_chat_model=models.__getitem__, aclose=AsyncMock()),
    )
    monkeypatch.setattr(middleware_module, "_estimated_input_tokens", lambda _request: 100)
    try:
        yield services, turn, raw, models
    finally:
        db.close()


@pytest.mark.parametrize(
    ("configuration", "expected"),
    [
        ({}, False),
        ({"execution_options": {}}, True),
        ({"execution_options": {"model_ref": "box/local"}}, False),
        (
            {
                "execution_options": {"model_ref": "cloud/text"},
                "capability_model_route": True,
            },
            True,
        ),
        (
            {
                "execution_options": {"model_ref": "cloud/text", "routing": {"mode": "box"}},
                "capability_model_route": True,
            },
            False,
        ),
        (
            {
                "execution_options": {"model_ref": "cloud/text"},
                "routing": {"target": "explicit"},
                "capability_model_route": True,
            },
            False,
        ),
        ({"execution_options": {}, "expert_profile": {"default_model": "box/local"}}, False),
        ({"execution_options": {}, "routing": {"target": "box"}}, True),
        ({"execution_options": {}, "routing": {"target": "explicit"}}, False),
        ({"execution_kind": "resident", "resident_configuration": {}}, False),
        (
            {
                "execution_kind": "resident",
                "resident_configuration": {"automatic_model_route": True},
            },
            True,
        ),
    ],
)
def test_only_recorded_automatic_routes_can_fail_over(configuration, expected):
    assert model_failover.automatic_route(configuration) is expected


@pytest.mark.asyncio
async def test_box_failover_stays_local_and_audits_without_mutating_snapshot(
    environment, monkeypatch
):
    services, turn, raw, models = environment
    middleware = await model_failover.task_failover_middleware(
        services, 1, turn, raw, ProviderStore(services.provider_repo).build_harness_configs()
    )
    middleware._retry.max_retries = 0
    configurable = {"model": "box/local", "octop_auto_route": True}
    monkeypatch.setattr(middleware_module, "_configurable", lambda _request: configurable)
    attempted = []

    async def handler(request):
        attempted.append(request.model.model)
        if request.model.model == "box/local":
            raise ConnectionError("offline")
        # The route must already be durable before the replacement model is called.
        assert services.audit_repo.query(action="task.model_failover")
        return ModelResponse(result=[AIMessage(content="recovered")])

    response = await middleware.awrap_model_call(
        ModelRequest(model=models["box/local"], messages=[HumanMessage(content="continue")]),
        handler,
    )
    assert response.result[0].content == "recovered"
    assert attempted == ["box/local", "box/spare"]
    persisted = services.project_task_turn_repo.get(turn.turn_id)
    assert persisted.model_ref == "box/local"
    assert persisted.configuration_json == turn.configuration_json
    audit = services.audit_repo.query(action="task.model_failover")
    assert audit[0].target == turn.turn_id
    assert json.loads(audit[0].payload)["to_model_ref"] == "box/spare"
    await middleware.aclose()
    middleware._close_models.assert_awaited_once()


@pytest.mark.asyncio
async def test_explicit_task_failure_is_raised_without_another_model(environment, monkeypatch):
    services, turn, raw, models = environment
    middleware = await model_failover.task_failover_middleware(
        services, 1, turn, raw, ProviderStore(services.provider_repo).build_harness_configs()
    )
    middleware._retry.max_retries = 0
    monkeypatch.setattr(
        middleware_module,
        "_configurable",
        lambda _request: {"model": "box/local", "octop_auto_route": False},
    )
    attempted = []

    async def handler(request):
        attempted.append(request.model.model)
        raise ConnectionError("offline")

    with pytest.raises(ConnectionError, match="offline"):
        await middleware.awrap_model_call(
            ModelRequest(model=models["box/local"], messages=[HumanMessage(content="continue")]),
            handler,
        )
    assert attempted == ["box/local"]
    assert not services.audit_repo.query(action="task.model_failover")
