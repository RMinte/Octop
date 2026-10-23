"""Unified expert selection uses the same durable task and project control plane."""

from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from octop.infra.projects import runtime
from octop.infra.projects.expert_profiles import _workspace
from tests.support.auth import resolve_user_id
from tests.support.fakes import FakeHarnessAgent


async def finished(client, auth, task_id):
    for _ in range(300):
        response = await client.get(f"/api/project-tasks/{task_id}", headers=auth)
        assert response.status_code == 200, response.text
        detail = response.json()
        if detail["active_turn_id"] is None:
            return detail
        await asyncio.sleep(0.01)
    pytest.fail("unified task did not finish")


@pytest.fixture
async def unified_env(env_with_provider, monkeypatch, tmp_path):
    client, server, auth = env_with_provider
    owner = await resolve_user_id(client, auth, "admin")
    config = {"workspace_dir": str(tmp_path / "expert-state")}
    server.services.agent_repo.create(
        agent_id="unified-expert",
        user_id=owner,
        name="Financial expert",
        system_prompt="Use a professional financial method.",
        config_json=json.dumps(config),
    )
    row = server.services.agent_repo.get("unified-expert")
    _workspace(server.services, row, config).upload_bytes(
        "SOUL.md", b"Be precise about financial assumptions."
    )
    calls = []

    async def run(services, user_id, turn, history):
        calls.append((turn, history))
        yield {"type": "token", "content": f"Response {len(calls)}"}

    monkeypatch.setattr(runtime, "run_turn", run)
    return client, server, auth, owner, calls


@pytest.mark.parametrize("with_project", [False, True])
@pytest.mark.parametrize("with_expert", [False, True])
async def test_four_combinations_preserve_identity_and_directory(
    unified_env, tmp_path, with_project, with_expert
):
    client, server, auth, owner, calls = unified_env
    directory = tmp_path / "project-work"
    directory.mkdir()
    project = None
    if with_project:
        response = await client.post(
            "/api/projects",
            headers={**auth, "Idempotency-Key": str(uuid4())},
            json={"name": "Unified project", "directory": str(directory)},
        )
        assert response.status_code == 201, response.text
        project = response.json()
    before = len(server.services.agent_repo.list_all())
    message = {
        "request_id": "unified-create",
        "prompt": "Analyze the report",
        "project_id": project["project_id"] if project else None,
        "selected_expert_id": "unified-expert" if with_expert else None,
    }
    response = await client.post("/api/project-tasks", headers=auth, json=message)
    assert response.status_code == 202, response.text
    task = await finished(client, auth, response.json()["task_id"])
    saved = json.loads(calls[0][0].configuration_json)
    assert task["selected_expert_id"] == message["selected_expert_id"]
    assert task["configuration_revision"] == 1
    assert saved["directory"] == (str(directory) if with_project else None)
    assert task["turns"][0]["server_directory"] == saved["directory"]
    assert task["turns"][0]["selected_expert_id"] == message["selected_expert_id"]
    assert len(server.services.agent_repo.list_all()) == before
    if with_expert:
        assert saved["expert_profile"]["name"] == "Financial expert"
        assert (
            saved["expert_profile"]["profile_files"][0]["content"]
            == "Be precise about financial assumptions."
        )
        assert saved["directory"] != str(tmp_path / "expert-state")
    else:
        assert saved["expert_profile"] is None
    replay = await client.post("/api/project-tasks", headers=auth, json=message)
    assert replay.status_code == 202 and replay.json()["task_id"] == task["task_id"]
    assert len(calls) == 1
    listed = (await client.get("/api/project-tasks?scope=all", headers=auth)).json()
    assert any(item["task_id"] == task["task_id"] for item in listed["items"])


async def test_expert_switch_preserves_past_identity_and_rejects_stale_queue(unified_env):
    client, server, auth, owner, calls = unified_env
    original = {
        "request_id": "switch-create",
        "prompt": "First request",
        "selected_expert_id": "unified-expert",
    }
    response = await client.post("/api/project-tasks", headers=auth, json=original)
    assert response.status_code == 202, response.text
    task = await finished(client, auth, response.json()["task_id"])
    response = await client.patch(
        f"/api/project-tasks/{task['task_id']}",
        headers=auth,
        json={
            "expected_revision": task["list_revision"],
            "expected_configuration_revision": 1,
            "selected_expert_id": None,
            "action_mode": "plan",
            "title": "Stable title",
        },
    )
    assert response.status_code == 200, response.text
    changed = response.json()
    assert changed["configuration_revision"] == 2
    assert changed["configuration_events"][0]["previous_expert_id"] == "unified-expert"
    assert changed["turns"][0]["expert_name"] == "Financial expert"
    replay = await client.post("/api/project-tasks", headers=auth, json=original)
    assert replay.status_code == 202 and len(calls) == 1
    url = f"/api/project-tasks/{task['task_id']}/messages"
    stale = await client.post(
        url,
        headers=auth,
        json={
            "request_id": "stale-queue",
            "prompt": "Old draft",
            "expected_configuration_revision": 1,
        },
    )
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "TASK_REVISION_CONFLICT"
    next_turn = await client.post(
        url,
        headers=auth,
        json={
            "request_id": "fresh-queue",
            "prompt": "Continue planning",
            "expected_configuration_revision": 2,
        },
    )
    assert next_turn.status_code == 202, next_turn.text
    done = await finished(client, auth, task["task_id"])
    assert done["turns"][-1]["selected_expert_id"] is None
    assert done["turns"][-1]["action_mode"] == "plan"
    assert len(calls) == 2


async def test_global_filters_sync_and_reference_delete_protection(unified_env, tmp_path):
    client, server, auth, owner, calls = unified_env
    project = (
        await client.post(
            "/api/projects",
            headers={**auth, "Idempotency-Key": str(uuid4())},
            json={"name": "Keep project"},
        )
    ).json()
    created = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={
            "request_id": "global-project",
            "prompt": "Mercury report",
            "project_id": project["project_id"],
            "selected_expert_id": "unified-expert",
        },
    )
    assert created.status_code == 202, created.text
    task = await finished(client, auth, created.json()["task_id"])
    page = (
        await client.get("/api/project-tasks?scope=all&expert_id=unified-expert", headers=auth)
    ).json()
    assert [item["task_id"] for item in page["items"]] == [task["task_id"]]
    assert (await client.get("/api/project-tasks", headers=auth)).json()["items"] == []
    updated = await client.patch(
        f"/api/project-tasks/{task['task_id']}",
        headers=auth,
        json={
            "expected_revision": task["list_revision"],
            "pinned": True,
            "title": "Pinned Mercury",
        },
    )
    assert updated.status_code == 200, updated.text
    delta = await client.get(
        "/api/project-tasks/sync",
        headers=auth,
        params={"scope": "all", "expert_id": "unified-expert", "sync_cursor": page["sync_cursor"]},
    )
    assert delta.status_code == 200, delta.text
    assert delta.json()["changes"][0]["data"]["pinned"] is True
    deletion = await client.delete(f"/api/projects/{project['project_id']}", headers=auth)
    assert deletion.status_code == 409 and deletion.json()["error"]["code"] == "PROJECT_HAS_TASKS"
    expert_deletion = await client.delete("/api/agents/unified-expert", headers=auth)
    assert expert_deletion.status_code == 409, expert_deletion.text
    assert expert_deletion.json()["error"]["code"] == "EXPERT_IN_USE"
    assert server.services.agent_repo.get("unified-expert") is not None
    assert (tmp_path / "expert-state").is_dir()
    assert (
        await client.get(f"/api/project-tasks/{task['task_id']}", headers=auth)
    ).status_code == 200


async def test_branch_continuation_is_idempotent_after_expert_switch(unified_env):
    client, server, auth, owner, calls = unified_env
    created = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={"request_id": "branch-source", "prompt": "Original question"},
    )
    source = await finished(client, auth, created.json()["task_id"])
    url = f"/api/project-tasks/{source['task_id']}/forks"
    request = {
        "request_id": "branch-continue",
        "operation": "fork",
        "prompt": "Explore another approach",
        "selected_expert_id": "unified-expert",
    }
    created_branch = await client.post(url, headers=auth, json=request)
    assert created_branch.status_code == 202, created_branch.text
    branch = await finished(client, auth, created_branch.json()["task_id"])
    assert branch["task_id"] != source["task_id"]
    assert len(branch["turns"]) == 2
    assert branch["turns"][0]["history_only"] is True
    assert branch["turns"][0]["source_turn_id"] == source["turns"][0]["turn_id"]
    assert branch["turns"][1]["prompt"] == request["prompt"]
    assert branch["turns"][1]["expert_name"] == "Financial expert"
    changed = await client.patch(
        f"/api/project-tasks/{branch['task_id']}",
        headers=auth,
        json={
            "expected_revision": branch["list_revision"],
            "expected_configuration_revision": 1,
            "selected_expert_id": None,
        },
    )
    assert changed.status_code == 200, changed.text
    replay = await client.post(url, headers=auth, json=request)
    assert replay.status_code == 202, replay.text
    assert replay.json()["task_id"] == branch["task_id"]
    assert len(calls) == 2
    assert (await client.get(f"/api/project-tasks/{source['task_id']}", headers=auth)).json()[
        "turns"
    ] == source["turns"]


async def test_expert_memory_http_save_scope_and_policy_cas(unified_env):
    client, server, auth, owner, calls = unified_env
    created = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={
            "request_id": "memory-task",
            "prompt": "Remember my reporting preferences",
            "selected_expert_id": "unified-expert",
        },
    )
    task = await finished(client, auth, created.json()["task_id"])
    url = f"/api/project-tasks/{task['task_id']}/expert-memory"
    request = {
        "source": "scope",
        "request_id": "explicit-memory",
        "content": "Use quarterly comparison tables.",
        "expected_expert_id": "unified-expert",
    }
    saved = await client.post(url, headers=auth, json=request)
    assert saved.status_code == 200, saved.text
    assert saved.json()["status"] == "completed"
    replay = await client.post(url, headers=auth, json=request)
    assert replay.status_code == 200 and replay.json()["replayed"] is True
    entries = await client.get(url, headers=auth)
    assert entries.status_code == 200, entries.text
    assert any(item["content"] == request["content"] for item in entries.json()["entries"])
    assert (await client.get(url + "?source=general", headers=auth)).json()["entries"] == []
    assert (await client.get(url + "?source=legacy", headers=auth)).status_code == 422
    policy = (await client.get(url + "/policy", headers=auth)).json()
    patch = {
        "expected_revision": policy["config_revision"],
        "legacy_recall_enabled": True,
        "expected_expert_id": "unified-expert",
    }
    changed = await client.patch(url + "/policy", headers=auth, json=patch)
    assert changed.status_code == 200, changed.text
    assert changed.json()["legacy_recall_enabled"] is True
    assert changed.json()["config_revision"] == policy["config_revision"] + 1
    assert (await client.patch(url + "/policy", headers=auth, json=patch)).status_code == 409


async def test_explicit_legacy_continuation_keeps_source_immutable(unified_env, monkeypatch):
    client, server, auth, owner, calls = unified_env
    thread_id = "legacy-unified-source"
    server.services.thread_repo.insert(
        thread_id=thread_id,
        agent_id="unified-expert",
        user_id=owner,
        channel_type="dashboard",
        session_key="legacy-session",
        title="Prior analysis",
    )
    server.services.thread_message_repo.mark_projection(thread_id, "pending")
    harness = FakeHarnessAgent()
    messages = [HumanMessage(content="Prior question"), AIMessage(content="Prior answer")]
    harness.seed_thread_messages(thread_id, messages)
    monkeypatch.setattr(server.app_runtime, "history_archive", None)
    monkeypatch.setattr(server.app_runtime.agent_registry, "get_agent", lambda agent_id: harness)
    before = server.services.thread_repo.get(thread_id)
    request = {
        "request_id": "legacy-explicit",
        "agent_id": "unified-expert",
        "thread_id": thread_id,
        "project_id": None,
    }
    response = await client.post("/api/project-tasks/from-thread", headers=auth, json=request)
    assert response.status_code == 201, response.text
    task = response.json()
    assert task["selected_expert_id"] == "unified-expert"
    assert task["active_turn_id"] is None and len(calls) == 0
    assert task["turns"][0]["history_only"] is True
    assert task["turns"][0]["source_thread_id"] == thread_id
    assert task["turns"][0]["reply"] == "Prior answer"
    replay = await client.post("/api/project-tasks/from-thread", headers=auth, json=request)
    assert replay.status_code == 201 and replay.json()["task_id"] == task["task_id"]
    assert server.services.thread_repo.get(thread_id) == before
    assert (await harness.graph.aget_state({"configurable": {"thread_id": thread_id}})).values[
        "messages"
    ] == messages
    sent = await client.post(
        f"/api/project-tasks/{task['task_id']}/messages",
        headers=auth,
        json={"request_id": "legacy-new-turn", "prompt": "Continue here"},
    )
    assert sent.status_code == 202, sent.text
    completed = await finished(client, auth, task["task_id"])
    assert len(completed["turns"]) == 2 and len(calls) == 1
    assert server.services.thread_repo.get(thread_id) == before


async def test_expert_auto_model_inherits_available_default(unified_env):
    client, server, auth, owner, calls = unified_env
    server.services.agent_repo.update_config("unified-expert", default_model=" aUtO ")
    response = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={
            "request_id": "auto-expert-model",
            "prompt": "Use the current default model",
            "selected_expert_id": "unified-expert",
        },
    )
    assert response.status_code == 202, response.text
    task = await finished(client, auth, response.json()["task_id"])
    assert task["turns"][0]["model_ref"]
    assert json.loads(calls[0][0].configuration_json)["expert_profile"]["default_model"] is None


async def test_branch_does_not_admit_work_when_worker_is_unavailable(unified_env, monkeypatch):
    from octop.api.routers import projects
    from octop.infra.errors import ErrorCode, OctopError

    client, server, auth, owner, calls = unified_env
    response = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={"request_id": "closed-branch-source", "prompt": "Original work"},
    )
    source = await finished(client, auth, response.json()["task_id"])
    before = (await client.get("/api/project-tasks?scope=all", headers=auth)).json()["items"]

    def unavailable(server):
        raise OctopError.localized(ErrorCode.CONTROL_PLANE_BUSY)

    monkeypatch.setattr(projects, "_task_worker", unavailable)
    forked = await client.post(
        f"/api/project-tasks/{source['task_id']}/forks",
        headers=auth,
        json={"request_id": "closed-branch-work", "prompt": "Continue in a branch"},
    )
    assert forked.status_code == 409, forked.text
    assert forked.json()["error"]["code"] == "CONTROL_PLANE_BUSY"
    after = (await client.get("/api/project-tasks?scope=all", headers=auth)).json()["items"]
    assert after == before and len(calls) == 1
