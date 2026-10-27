"""Real task workers bridge native engines without borrowing their execution authority."""
# ruff: noqa: F811

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from acp.schema import LoadSessionRequest, NewSessionRequest

from octop.infra.connectors.service import ConnectorService
from octop.infra.errors import OctopError
from octop.infra.projects import branches, engine_catalog, interactions, runtime, service, tasks
from octop.infra.projects.engines import runtime as engine_runtime
from octop.infra.projects.engines.acp import ACPTaskError, SessionInfo
from octop.infra.projects.expert_memory import ExpertMemory
from octop.infra.projects.interaction_models import InteractionResponse
from octop.infra.projects.models import ProjectFields
from tests.unit.projects.test_execution_engines import ready
from tests.unit.projects.test_expert_execution import services  # noqa: F401
from tests.unit.projects.test_unified_ingress import message


@pytest.fixture
def native(monkeypatch):
    class Session:
        instances = []
        supports_load = True
        restore_failed = False
        permissions = False
        fail_prompt = False
        mcp_capabilities = {}

        def __init__(self, launch, env, cwd, mcp_servers=None, on_permission=None):
            self.launch, self.env, self.cwd = launch, env, cwd
            self.mcp_servers, self.on_permission = mcp_servers, on_permission
            self.requested_session = None
            self.prompts = []
            self.decisions = []
            self.closed = False
            self.instances.append(self)

        async def open(self, session_id=None):
            self.requested_session = session_id
            resumed = bool(session_id and self.supports_load and not self.restore_failed)
            self.info = SessionInfo(
                session_id if resumed else f"native-session-{len(self.instances)}",
                resumed,
                {"loadSession": self.supports_load, "mcpCapabilities": self.mcp_capabilities},
            )
            return self.info

        async def stream(self, prompt, *, model_id=None, mode_id=None):
            self.prompts.append(prompt)
            if self.fail_prompt:
                raise ACPTaskError("prompt")
            if self.permissions:
                for index in range(2):
                    self.decisions.append(
                        await self.on_permission(
                            self.info.session_id,
                            {
                                "toolCallId": f"native-command-{index}",
                                "kind": "execute",
                                "title": "Inspect project",
                                "rawInput": {"command": "inspect-project"},
                            },
                            [
                                {"optionId": "once", "kind": "allow_once"},
                                {"optionId": "always", "kind": "allow_always"},
                                {"optionId": "deny", "kind": "reject_once"},
                            ],
                        )
                    )
            yield {"type": "token", "content": f"native answer {len(self.instances)}"}
            yield {"type": "usage", "input_tokens": 90, "output_tokens": 20}

        async def close(self):
            self.closed = True

    monkeypatch.setattr(engine_runtime, "ACPTaskSession", Session)
    return Session


def create(services, selection, prompt="first-native-request", **kwargs):
    options = kwargs.pop("options", {"action_timeline_enabled": False})
    return tasks.send(
        services,
        1,
        tasks.TaskCreate(
            request_id="create-" + prompt,
            prompt=prompt,
            engine=selection,
            options=options,
            **kwargs,
        ),
        project_id=kwargs.get("project_id"),
    )


async def execute(services, task, worker=None):
    worker = worker or runtime.ProjectTaskWorker(services)
    turn = services.project_task_turn_repo.claim(worker.worker_id, tasks.now_ms())
    assert turn is not None and turn.task_id == task.task_id
    await worker._execute(turn)
    return tasks.detail(services, 1, task.task_id)


def followup(services, task, prompt):
    return tasks.send(
        services,
        1,
        tasks.TaskMessage(
            request_id="followup-" + prompt,
            prompt=prompt,
            expected_configuration_revision=task.configuration_revision,
            options={"action_timeline_enabled": False},
        ),
        task_id=task.task_id,
    )


def switch(services, task, selection):
    return tasks.patch(
        services,
        1,
        task.task_id,
        tasks.TaskPatch(
            expected_revision=task.list_revision,
            expected_configuration_revision=task.configuration_revision,
            engine=selection,
        ),
    )


async def pending(services, turn_id, count):
    async with asyncio.timeout(3):
        while True:
            turn = services.project_task_turn_repo.get(turn_id)
            items = interactions.load(turn)
            if len(items) == count and items[-1].status == "pending":
                return turn, items[-1]
            await asyncio.sleep(0.01)


def respond(services, task_id, turn_id, item, decision, owner=1):
    interactions.respond(
        services,
        owner,
        task_id,
        turn_id,
        item.interaction_id,
        InteractionResponse(
            request_id="response-" + item.interaction_id,
            expected_revision=item.revision,
            response={
                "kind": "approval",
                "decision": decision,
                "action_digest": item.payload.action_digest,
            },
        ),
    )


async def test_switch_back_loads_native_session_and_sends_only_other_engine_delta(
    services, monkeypatch, native
):
    selection, _ = ready(services, monkeypatch)
    task = await execute(services, create(services, selection))
    assert task.status == "completed" and native.instances[0].closed
    first = native.instances[0]
    message(services)
    octop_history = []

    async def octop(services, owner, turn, history):
        octop_history.extend(history)
        yield {"type": "token", "content": "octop-middle-answer"}

    monkeypatch.setattr(runtime, "_run_turn", octop)
    task = switch(services, task, engine_catalog.EngineSelection())
    task = await execute(services, followup(services, task, "octop-middle-request"))
    assert "first-native-request" in json.dumps(octop_history)
    assert "native answer 1" in json.dumps(octop_history)
    task = switch(services, task, selection)
    task = await execute(services, followup(services, task, "back-to-native"))
    second = native.instances[1]
    text = second.prompts[0][0]["text"]
    assert second.requested_session == first.info.session_id and second.info.resumed
    assert "octop-middle-request" in text and "octop-middle-answer" in text
    assert "first-native-request" not in text and "native answer 1" not in text
    assert "Updates since this engine last worked" in text and "back-to-native" in text
    assert second.cwd == first.cwd
    assert task.status == "completed" and all(session.closed for session in native.instances)
    assert [turn.execution_engine.engine_id for turn in task.turns] == ["acp", "octop", "acp"]
    assert first.info.session_id not in task.model_dump_json()
    row = services.project_task_repo.get_internal(task.task_id, 1)
    binding = next(iter(json.loads(row.execution_json)["sessions"].values()))
    assert binding["session_id"] == first.info.session_id
    assert binding["status"] == "completed" and binding["watermark"] == task.turns[-1].sequence


@pytest.mark.parametrize("reason", ["load_failed", "load_unsupported"])
async def test_failed_or_unsupported_native_resume_creates_fresh_session_with_full_history(
    services, monkeypatch, native, reason
):
    selection, _ = ready(services, monkeypatch)
    task = await execute(services, create(services, selection))
    native.restore_failed = reason == "load_failed"
    native.supports_load = reason != "load_unsupported"
    task = await execute(services, followup(services, task, "fresh-followup"))
    session = native.instances[-1]
    assert session.requested_session == native.instances[0].info.session_id
    assert not session.info.resumed
    assert session.info.session_id != native.instances[0].info.session_id
    text = session.prompts[0][0]["text"]
    assert "Task history" in text and "Updates since this engine" not in text
    assert "first-native-request" in text and "native answer 1" in text
    assert "fresh-followup" in text and len(session.prompts) == 1
    assert task.status == "completed"


async def test_branch_inherits_visible_history_but_never_native_session(
    services, monkeypatch, native
):
    selection, _ = ready(services, monkeypatch)
    original = await execute(services, create(services, selection))
    fork = branches.create(
        services, 1, original.task_id, branches.TaskFork(request_id="fork-native-history")
    )
    forked = tasks.detail(services, 1, fork.task_id)
    stored = services.project_task_repo.get_internal(fork.task_id, 1)
    assert not json.loads(stored.execution_json).get("sessions")
    forked = await execute(services, followup(services, forked, "branch-request"))
    session = native.instances[-1]
    assert session.requested_session is None and not session.info.resumed
    assert session.cwd != native.instances[0].cwd
    assert "first-native-request" in session.prompts[0][0]["text"]
    assert forked.status == "completed" and forked.turns[0].history_only
    assert services.project_task_repo.get_internal(original.task_id, 1).execution_json != "{}"


async def test_project_cwd_identity_and_expert_memory_scope_are_independent(
    services, monkeypatch, native, tmp_path
):
    selection, _ = ready(services, monkeypatch)
    work = tmp_path / "project-working-directory"
    work.mkdir()
    project = service.create_project(
        services,
        1,
        ProjectFields(name="Ledger", directory=str(work), instructions="Project accounting rules"),
        "native-context-project",
    )
    recalled, captured = [], []

    async def recall(self, prompt):
        recalled.append(self)
        return "Scoped recalled evidence " + self.namespaces["scope"] if self.enabled else ""

    async def capture(self, prompt, reply):
        if self.capture_enabled:
            captured.append((self, prompt, reply))

    monkeypatch.setattr(ExpertMemory, "prompt", recall)
    monkeypatch.setattr(ExpertMemory, "capture", capture)
    task = create(
        services,
        selection,
        selected_expert_id="expert-one",
        project_id=project.project_id,
    )
    task = await execute(services, task)
    assert task.status == "completed"
    text = native.instances[0].prompts[0][0]["text"]
    assert Path(native.instances[0].cwd) == work
    assert all(
        value in text
        for value in [
            "Financial method",
            "Expert identity",
            "Personal preferences",
            "Project accounting rules",
            "Scoped recalled evidence",
        ]
    )
    assert str(tmp_path / "expert") not in text
    assert recalled[0].project_id == project.project_id and recalled[0].owner_id == 1
    assert captured[0][0] is recalled[0] and captured[0][2] == "native answer 1"
    skill = (
        work
        / ".octop"
        / "tasks"
        / task.task_id
        / task.turns[0].turn_id
        / "skills"
        / "local-method"
        / "SKILL.md"
    )
    assert "local-method/SKILL.md" in text
    assert skill.read_text() == "Local method"
    history = next(
        server for server in native.instances[0].mcp_servers if server.name == "octop_task_history"
    )
    history_path = Path(history.args[-1])
    assert task.task_id in history_path.parts and task.turns[0].turn_id in history_path.parts
    standalone = await execute(
        services,
        create(services, selection, "standalone-expert", selected_expert_id="expert-one"),
    )
    assert (
        Path(native.instances[1].cwd)
        == services.paths.root / "project-tasks" / standalone.task_id / "workspace"
    )
    assert recalled[1].project_id is None
    assert recalled[1].namespaces["scope"] != recalled[0].namespaces["scope"]
    assert recalled[1].namespaces["general"] == recalled[0].namespaces["general"]
    disabled = tasks.send(
        services,
        1,
        tasks.TaskMessage(
            request_id="native-explicit-no-memory-skills",
            prompt="Use no saved memory or default skills",
            expected_configuration_revision=task.configuration_revision,
            options={
                "skills": [],
                "expert_memory_enabled": False,
                "action_timeline_enabled": False,
            },
        ),
        task_id=task.task_id,
    )
    disabled = await execute(services, disabled)
    assert disabled.status == "completed" and not recalled[2].enabled
    assert "Scoped recalled evidence" not in native.instances[2].prompts[0][0]["text"]
    assert "local-method/SKILL.md" not in native.instances[2].prompts[0][0]["text"]
    assert len(captured) == 2


async def test_live_native_permissions_allow_once_then_require_new_decision_without_restart(
    services, monkeypatch, native
):
    selection, _ = ready(services, monkeypatch)
    native.permissions = True
    task = create(services, selection, "native-permissions")
    worker = runtime.ProjectTaskWorker(services)
    turn = services.project_task_turn_repo.claim(worker.worker_id, tasks.now_ms())
    running = asyncio.create_task(worker._execute(turn))
    try:
        waiting, first = await pending(services, turn.turn_id, 1)
        assert waiting.status == "running" and waiting.worker_id == worker.worker_id
        assert waiting.wait_kind == "engine_approval" and not waiting.checkpoint_id
        assert not running.done() and not native.instances[0].closed
        assert tasks.detail(services, 1, task.task_id).status == "waiting_approval"
        with pytest.raises(OctopError):
            respond(services, task.task_id, turn.turn_id, first, "allow_once", owner=2)
        respond(services, task.task_id, turn.turn_id, first, "allow_once")
        waiting, second = await pending(services, turn.turn_id, 2)
        assert second.interaction_id != first.interaction_id
        assert native.instances[0].decisions == ["once"]
        assert waiting.worker_id == worker.worker_id and not running.done()
        respond(services, task.task_id, turn.turn_id, second, "deny")
        await asyncio.wait_for(running, 3)
    finally:
        if not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
    result = tasks.detail(services, 1, task.task_id)
    assert result.status == "completed" and len(native.instances) == 1
    assert native.instances[0].decisions == ["once", "deny"] and native.instances[0].closed
    stored = services.project_task_turn_repo.get(turn.turn_id)
    assert stored.wait_kind == "" and stored.worker_id == worker.worker_id
    assert [item.resolution.outcome for item in interactions.load(stored)] == ["allow_once", "deny"]


async def test_cancel_during_native_permission_closes_session_and_worker_can_run_next_task(
    services, monkeypatch, native
):
    selection, _ = ready(services, monkeypatch)
    native.permissions = True
    task = create(services, selection, "cancel-native-permission")
    worker = runtime.ProjectTaskWorker(services)
    turn = services.project_task_turn_repo.claim(worker.worker_id, tasks.now_ms())
    running = asyncio.create_task(worker._execute(turn))
    try:
        _, item = await pending(services, turn.turn_id, 1)
        tasks.cancel(services, 1, task.task_id, turn.turn_id)
        await asyncio.wait_for(running, 3)
    finally:
        if not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
    assert tasks.detail(services, 1, task.task_id).status == "cancelled"
    assert native.instances[0].closed and native.instances[0].decisions == []
    with pytest.raises(OctopError):
        respond(services, task.task_id, turn.turn_id, item, "allow_once")
    native.permissions = False
    following = await execute(services, create(services, selection, "after-cancel"), worker)
    assert following.status == "completed" and not worker.closing


async def test_prompt_transport_failure_is_not_replayed_and_binding_stays_uncommitted(
    services, monkeypatch, native
):
    selection, _ = ready(services, monkeypatch)
    native.fail_prompt = True
    task = await execute(services, create(services, selection, "native-prompt-error"))
    assert len(native.instances) == 1 and len(native.instances[0].prompts) == 1
    assert native.instances[0].closed and task.status == "interrupted"
    row = services.project_task_repo.get_internal(task.task_id, 1)
    binding = next(iter(json.loads(row.execution_json)["sessions"].values()))
    assert binding["status"] == "inflight" and binding["watermark"] == 0


@pytest.mark.parametrize("transport", ["http", "sse"])
def test_mcp_http_and_sse_use_sdk_new_and_load_session_wire_types(tmp_path, transport):
    servers = engine_runtime._mcp_servers(
        {
            "selected": {
                "transport": transport,
                "url": "http://127.0.0.1:9/mcp",
                "headers": {"X-Acceptance": "local-only"},
            }
        }
    )
    for request in (
        NewSessionRequest(cwd=str(tmp_path), mcp_servers=servers),
        LoadSessionRequest(cwd=str(tmp_path), session_id="saved", mcp_servers=servers),
    ):
        assert request.model_dump(mode="json", by_alias=True, exclude_none=True)["mcpServers"] == [
            {
                "name": "selected",
                "url": "http://127.0.0.1:9/mcp",
                "headers": [{"name": "X-Acceptance", "value": "local-only"}],
                "type": transport,
            }
        ]


@pytest.mark.parametrize("supported", [False, True])
async def test_native_mcp_transport_requires_actual_advertised_support(
    services, monkeypatch, native, supported
):
    selection, _ = ready(services, monkeypatch)
    connectors = ConnectorService(
        repo=services.connector_repo,
        secret_repo=services.secret_repo,
        settings_repo=services.settings_repo,
        config=services.config,
    )
    connectors.put_custom_servers(
        1,
        {
            "selected": {
                "transport": "streamable_http",
                "url": "http://127.0.0.1:9/mcp",
            }
        },
    )
    project = service.create_project(
        services,
        1,
        ProjectFields(name="MCP acceptance", mcp_servers=["selected"]),
        "native-mcp-project",
    )
    native.mcp_capabilities = {"http": supported}
    task = await execute(services, create(services, selection, project_id=project.project_id))
    session = native.instances[0]
    server = next(item for item in session.mcp_servers if item.name == "selected")
    assert server.type == "http" and session.closed
    if supported:
        assert task.status == "completed" and len(session.prompts) == 1
    else:
        assert task.status == "failed" and task.turns[0].error_code == "TASK_SETTINGS_INVALID"
        assert session.prompts == []
