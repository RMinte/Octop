"""Offline trusted Run cancellation through the real harness streaming boundary."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import suppress
from typing import Any
from uuid import uuid4

import httpx
import pytest
from langchain_core.tools import tool
from octop_harness.agent import HarnessAgent
from octop_harness.config import HarnessAgentConfig
from octop_harness.llm import factory as harness_factory

from octop.infra.agentcomputer import dispatch
from octop.infra.agentcomputer.cancellations import CancelRunReceipt
from octop.infra.agentcomputer.capabilities import CapabilityStatus, get_capabilities
from octop.infra.agentcomputer.commands import admit_new_message
from octop.infra.agentcomputer.engine import EngineEntryMiddleware, invoke_engine_entry
from octop.infra.agentcomputer.models import NewMessageCommand
from octop.infra.agents.providers.store import ProviderStore
from tests.support.agentcomputer import register_ready_task
from tests.support.agentcomputer_history import open_history_archive
from tests.support.auth import resolve_user_id

PREFIX = "/api/agentcomputer/v1"


@pytest.mark.parametrize("stop_server", [False, True], ids=["cancel-command", "server-stop"])
async def test_trusted_run_stream_closes_on_cancel_or_server_stop(
    env, monkeypatch: pytest.MonkeyPatch, stop_server: bool
) -> None:
    client, server, auth = env
    owner_id = await resolve_user_id(client, auth, "admin")
    task = await register_ready_task(client, server, auth, owner_id)
    assert server.services is not None
    services = server.services
    history_archive = open_history_archive(services)
    assert get_capabilities(services).capabilities.runs.available is False
    services.provider_repo.create(
        name="not-configured",
        kind="openai",
        base_url="https://wire.invalid/v1",
        api_key="fixture-local-key",
        models_json=json.dumps([{"id": "no-model", "enabled": True}]),
    )
    providers = ProviderStore(services.provider_repo).build_harness_configs()
    assert len(providers) == 1
    original = await asyncio.to_thread(
        admit_new_message,
        services,
        owner_id=owner_id,
        task_id=task.taskID,
        command=NewMessageCommand(
            commandID=uuid4(),
            idempotencyKey=str(uuid4()),
            expectedRestoreEpoch=1,
            contextMode="newMessage",
            userMessageID=uuid4(),
            expectedProjectRevision=1,
            prompt="Call synthetic_echo with value ping.",
            permissionMode="askForApproval",
        ),
    )
    profile = get_capabilities(services)
    worker_profile = profile.model_copy(
        update={
            "capabilities": profile.capabilities.model_copy(
                update={"runs": CapabilityStatus(supported=True, available=True)}
            ),
            "supportedPermissionModes": ["askForApproval"],
        }
    )
    monkeypatch.setattr(dispatch, "get_capabilities", lambda _: worker_profile)
    claim = await asyncio.to_thread(
        dispatch.claim_run,
        services,
        owner_id=owner_id,
        run_id=original.runID,
        claimant_id=uuid4(),
        expected_revision=1,
    )

    blocked = asyncio.Event()
    released = asyncio.Event()
    closed = asyncio.Event()
    requests: list[httpx.Request] = []
    tool_inputs: list[str] = []
    cancellation_at_signal: list[int] = []
    agents: list[HarnessAgent] = []

    class BlockedToolCompletion(httpx.AsyncByteStream):
        async def __aiter__(self):
            chunk = {
                "id": "chatcmpl-run-cancel-fixture",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "no-model",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call-run-cancel-fixture",
                                    "type": "function",
                                    "function": {
                                        "name": "synthetic_echo",
                                        "arguments": '{"value":"ping"}',
                                    },
                                }
                            ],
                        },
                        "finish_reason": None,
                    }
                ],
            }
            yield f"data: {json.dumps(chunk)}\n\n".encode()
            blocked.set()
            await released.wait()
            yield b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}\n\n'
            yield b"data: [DONE]\n\n"

        async def aclose(self) -> None:
            closed.set()

    def answer(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url == "https://wire.invalid/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer fixture-local-key"
        body = json.loads(request.content)
        assert body["stream"] is True and body["model"] == "no-model"
        assert any(item["function"]["name"] == "synthetic_echo" for item in body["tools"])
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=BlockedToolCompletion(),
        )

    sync_http = httpx.Client(transport=httpx.MockTransport(answer))
    async_http = httpx.AsyncClient(transport=httpx.MockTransport(answer))
    original_get_class = harness_factory._get_reasoning_aware_chat_openai

    def transport_class(base_class):
        actual_class = original_get_class(base_class)

        def construct(**kwargs):
            return actual_class(http_client=sync_http, http_async_client=async_http, **kwargs)

        return construct

    monkeypatch.setattr(harness_factory, "_get_reasoning_aware_chat_openai", transport_class)

    @tool
    def synthetic_echo(value: str) -> str:
        """Record any execution after a partial tool-call delta."""
        tool_inputs.append(value)
        return "pong"

    async def invoke(invocation) -> None:
        snapshot = invocation.intent.snapshot
        row = await asyncio.to_thread(services.agent_repo.get, snapshot.agentID)
        assert row is not None and row.config_json is not None
        workspace_config: dict[str, Any] = json.loads(row.config_json)
        config = HarnessAgentConfig(
            name="trusted-run-cancel-fixture",
            workspace_dir=workspace_config["workspace_dir"],
            system_files_path=workspace_config["system_files_path"],
            backend=workspace_config["backend"],
            providers=providers,
            default_model=snapshot.projectConfiguration.modelRef,
            system_prompt=snapshot.projectConfiguration.instructions,
            middleware=[EngineEntryMiddleware(services, invocation, history_archive)],
            tools=[synthetic_echo],
            checkpointer=False,
            bootstrap_enabled=False,
            memory_enabled=False,
            memory_jsonl_enabled=False,
            memory=(),
            session_log_enabled=False,
            subagents=[],
            subagents_auto_load=False,
            todos_enabled=False,
            team_enabled=False,
            ask_user_enabled=False,
            media_offload_enabled=False,
            model_retry_enabled=False,
            web_search_tools=False,
            mcp_server_configs={},
            acp_delegate_enabled=False,
        )
        agent = await asyncio.to_thread(HarnessAgent, config, agent_id=snapshot.agentID)
        agents.append(agent)
        try:
            await invoke_engine_entry(
                agent,
                invocation,
                active_runs=server.agentcomputer_active_runs,
                history_archive=history_archive,
            )
        except asyncio.CancelledError:
            with services.db.connect() as conn:
                cancellation_at_signal.append(
                    conn.execute(
                        "SELECT COUNT(*) FROM ac_run_cancellations WHERE run_id=?",
                        (str(original.runID),),
                    ).fetchone()[0]
                )
            raise

    handoff = asyncio.create_task(
        dispatch.dispatch_run(
            services, claim=claim, invoke=invoke, active_runs=server.agentcomputer_active_runs
        )
    )
    try:
        await asyncio.wait_for(blocked.wait(), timeout=10)
        running = await client.get(f"{PREFIX}/runs/{original.runID}", headers=auth)
        assert running.status_code == 200
        assert running.json()["dispatchPhase"] == "engineAccepted"
        if stop_server:
            await server.stop()
        else:
            response = await client.post(
                f"{PREFIX}/runs/{original.runID}/cancel",
                headers=auth,
                json={
                    "commandID": str(uuid4()),
                    "idempotencyKey": str(uuid4()),
                    "expectedRestoreEpoch": 1,
                    "expectedRevision": running.json()["revision"],
                },
            )
            assert response.status_code == 201
            receipt = CancelRunReceipt.model_validate(response.json())
            assert receipt.runID == original.runID
            assert receipt.executionEpoch == claim.intent.executionEpoch
            assert receipt.status == "cancelRequested" and not receipt.replayed
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(handoff), timeout=5)
        await asyncio.wait_for(closed.wait(), timeout=5)
        history_turn = history_archive.store.turn(claim.intent.snapshot.octopThreadID)
        assert history_turn is not None
        assert history_turn["id"] == str(original.turnID)
        assert history_turn["status"] == "interrupted"
        history_messages = history_archive.store.documents(
            claim.intent.snapshot.octopThreadID,
            "message",
            turn_id=str(original.turnID),
        )
        assert history_messages[0]["value"]["data"]["content"] == (
            "Call synthetic_echo with value ping."
        )
        if stop_server:
            with sqlite3.connect(services.paths.db) as conn:
                run = conn.execute(
                    "SELECT state, dispatch_phase, reconciliation_required FROM ac_runs WHERE run_id=?",
                    (str(original.runID),),
                ).fetchone()
                assert run == ("interrupted", "engineAccepted", 1)
                assert (
                    conn.execute(
                        "SELECT COUNT(*) FROM ac_run_cancellations WHERE run_id=?",
                        (str(original.runID),),
                    ).fetchone()[0]
                    == 0
                )
                task_row = conn.execute(
                    "SELECT project_id, active_run_id FROM ac_tasks WHERE task_id=?",
                    (str(task.taskID),),
                ).fetchone()
                assert task_row is not None and task_row[1] == str(original.runID)
                project_row = conn.execute(
                    "SELECT active_run_id FROM ac_projects WHERE project_id=?",
                    (task_row[0],),
                ).fetchone()
                assert project_row is not None and project_row[0] == str(original.runID)
        else:
            current = await client.get(f"{PREFIX}/runs/{original.runID}", headers=auth)
            assert current.status_code == 200
            assert current.json()["state"] == "interrupted"
            assert current.json()["reconciliationRequired"] is True
            assert current.json()["cancelRequested"] is True
            task_row = services.ac_task_repo.get(str(task.taskID), owner_id)
            assert task_row is not None and task_row.active_run_id == str(original.runID)
            project_row = services.ac_project_repo.get(task_row.project_id, owner_id)
            assert project_row is not None and project_row.active_run_id == str(original.runID)
    finally:
        released.set()
        if not handoff.done():
            handoff.cancel()
            with suppress(asyncio.CancelledError):
                await handoff
        for agent in agents:
            await agent.aclose()
        await async_http.aclose()
        sync_http.close()
        history_archive.store.close()

    assert len(requests) == 1
    assert cancellation_at_signal == [0 if stop_server else 1]
    assert tool_inputs == []
    assert get_capabilities(services).capabilities.runs.available is False
