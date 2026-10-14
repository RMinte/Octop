"""Only bounded document-read facts leave the private tool journal."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from langchain_core.messages import AIMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from tests.unit.agentcomputer.test_effect_runner import effect as effect
from tests.unit.agentcomputer.test_engine_entry import _open_fixture_gate
from tests.unit.agentcomputer.test_engine_entry import entry as entry
from tests.unit.agentcomputer.test_submission_runs import control as control

from octop.api.deps import current_bearer_user, get_server
from octop.api.routers.agentcomputer_activity import router as activity_router
from octop.infra.agentcomputer import dispatch, effects
from octop.infra.agentcomputer.commands import StorageDataError, canonical_json, content_digest
from octop.infra.agentcomputer.http_operations import HttpOperationRegistry
from octop.infra.agentcomputer.project_document_tool import (
    CAPABILITY_VERSION,
    POLICY_VERSION,
    TOOL_NAME,
    read_project_document,
)
from octop.infra.agentcomputer.tool_activity import get_run_tool_activity
from octop.infra.agents.manager import AgentManager
from octop.infra.users.identity import User


async def test_completed_tool_activity_redacts_document_and_receipt(entry, tmp_path, monkeypatch):
    monkeypatch.setattr(effects, "get_capabilities", _open_fixture_gate)
    intent = entry.claim.intent
    entry.registry = AgentManager(repos=entry.services.repos, paths=entry.services.paths)
    workspace = entry.registry.workspace_for_project_preparation(
        intent.snapshot.agentID, str(intent.projectID)
    )
    workspace.write_text("documents/private.md", "秘密代号：海星。", force=True)
    entry.tools = [read_project_document]
    entry.model.responses = [
        AIMessage(
            content="",
            id=str(uuid4()),
            tool_calls=[{"name": TOOL_NAME, "args": {"path": "private.md"}, "id": "read-private"}],
        ),
        AIMessage(content="代号已确认。", id=str(intent.assistantMessageID)),
    ]
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "activity.sqlite")) as saver:
        entry.checkpointer = saver
        await dispatch.dispatch_run(
            entry.services, claim=entry.claim, invoke=entry.invoke, active_runs=entry.active_runs
        )
    activity = get_run_tool_activity(entry.services, owner_id=1, run_id=intent.runID)
    assert activity is not None
    assert activity.model_dump(mode="json") == {
        "runID": str(intent.runID),
        "items": [
            {
                "toolName": TOOL_NAME,
                "path": "documents/private.md",
                "status": "ok",
                "outcome": "succeeded",
            }
        ],
    }
    assert "秘密" not in activity.model_dump_json()
    assert "海星" not in activity.model_dump_json()
    assert "resultJSON" not in activity.model_dump_json()
    registry = HttpOperationRegistry()
    app = FastAPI()
    app.include_router(activity_router, prefix="/api/agentcomputer/v1")
    app.dependency_overrides[get_server] = lambda: SimpleNamespace(
        services=entry.services, database_bound=True, agentcomputer_http_operations=registry
    )
    app.dependency_overrides[current_bearer_user] = lambda: User(1, "owner", "admin", None)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.get(f"/api/agentcomputer/v1/runs/{intent.runID}/tool-activity")
        assert response.status_code == 200
        assert response.json() == activity.model_dump(mode="json")
        assert response.headers["cache-control"] == "no-store"
        assert "秘密" not in response.text and "海星" not in response.text
    finally:
        await registry.drain()


def _document_action(effect):
    arguments = {"path": "brief.md"}
    return effect.action.model_copy(
        update={
            "toolName": TOOL_NAME,
            "argumentsJSON": canonical_json(arguments),
            "argumentsDigest": content_digest(arguments),
            "capabilityVersion": CAPABILITY_VERSION,
            "policyVersion": POLICY_VERSION,
        }
    )


async def test_pending_and_unknown_activity_do_not_claim_a_document_read(effect):
    action = _document_action(effect)
    prepared = effects.prepare_effect(effect.services, effect.invocation, action).record
    run_id = effect.invocation.intent.runID
    pending = get_run_tool_activity(effect.services, owner_id=1, run_id=run_id)
    assert pending is not None
    assert pending.items[0].status == pending.items[0].outcome == "pending"
    effects.interrupt_effect(effect.services, owner_id=1, effect_id=prepared.intent.effectID)
    unknown = get_run_tool_activity(effect.services, owner_id=1, run_id=run_id)
    assert unknown is not None
    assert unknown.items[0].status == unknown.items[0].outcome == "unknown"


async def test_rejected_read_is_unavailable_and_corrupt_event_fails_closed(effect):
    action = _document_action(effect)
    prepared = effects.prepare_effect(effect.services, effect.invocation, action).record
    effects.reject_effect(
        effect.services, effect.invocation, prepared.intent.effectID, "authorizationDenied"
    )
    activity = get_run_tool_activity(
        effect.services, owner_id=1, run_id=effect.invocation.intent.runID
    )
    assert activity is not None
    assert activity.items[0].status == "unavailable"
    assert activity.items[0].outcome == "notDispatched"
    with effect.services.db.transaction() as conn:
        conn.execute("DELETE FROM ac_events WHERE event_type='tool.terminal'")
    with pytest.raises(StorageDataError):
        get_run_tool_activity(effect.services, owner_id=1, run_id=effect.invocation.intent.runID)


async def test_unrecognized_journal_tool_cannot_be_projected_as_document_activity(effect):
    effects.prepare_effect(effect.services, effect.invocation, effect.action)
    with pytest.raises(StorageDataError, match="unsupported project document tool"):
        get_run_tool_activity(effect.services, owner_id=1, run_id=effect.invocation.intent.runID)
