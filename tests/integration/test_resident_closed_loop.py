"""Private resident context, explicit Task delegation and durable result return."""

from __future__ import annotations

import asyncio
import json
import threading
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import httpx
import pytest
from langchain_core.messages import AIMessage, SystemMessage, convert_to_messages

from octop.infra.agents.resident_identity import ResidentTextOnlyMiddleware
from octop.infra.projects import runtime as task_runtime
from octop.infra.projects.recovery import TaskInterruption, TaskRecovery
from tests.support.auth import create_user, resolve_user_id
from tests.support.fakes import FakeHarnessAgent


def _key() -> str:
    return uuid4().hex


@pytest.fixture(autouse=True)
def resident_history_mode(request, monkeypatch):
    params = request.node.callspec.params if hasattr(request.node, "callspec") else {}
    enabled = params.get("report_history_v2", False) or "archive_transition" in params
    monkeypatch.setenv("OCTOP_HISTORY_V2_ENABLED", str(enabled).lower())
    monkeypatch.setenv("OCTOP_ENABLE_API_DOCS", "true")


async def _json(response: httpx.Response, status: int = 200) -> dict[str, Any]:
    assert response.status_code == status, response.text
    return response.json()


async def _resident_turn(client, auth, base, request_id, status="completed"):
    async with asyncio.timeout(10):
        while True:
            turn = await _json(await client.get(f"{base}/requests/{request_id}", headers=auth))
            if turn["status"] == status:
                return turn
            assert turn["status"] in {"queued", "running"}, turn
            await asyncio.sleep(0.01)


async def _send_resident(client, auth, base, prompt):
    request_id = _key()
    accepted = await _json(
        await client.post(
            f"{base}/messages", headers=auth, json={"request_id": request_id, "prompt": prompt}
        ),
        202,
    )
    return await _resident_turn(client, auth, base, accepted["request_id"])


async def _task(client, auth, task_id, status="completed"):
    async with asyncio.timeout(10):
        while True:
            task = await _json(await client.get(f"/api/project-tasks/{task_id}", headers=auth))
            if task["status"] == status:
                return task
            assert task["status"] not in {"failed", "completed", "cancelled"}, task
            await asyncio.sleep(0.01)


async def _delegation(client, auth, base, delegation_id, reports=1, delivered=True):
    async with asyncio.timeout(10):
        while True:
            result = await _json(
                await client.get(f"{base}/conversation/delegations/{delegation_id}", headers=auth)
            )
            if len(result["reports"]) == reports and (
                not delivered
                or all(report["delivery_status"] == "delivered" for report in result["reports"])
            ):
                return result
            await asyncio.sleep(0.01)


def _delegate_body(source_turn, *, prompt="Prepare the requested result.", project_id=None):
    return {
        "request_id": _key(),
        "source_turn_id": source_turn["turn_id"],
        "project_id": project_id,
        "message": {"request_id": _key(), "prompt": prompt},
    }


@pytest.fixture
async def closed_loop(env_with_provider, monkeypatch):
    client, server, auth = env_with_provider
    requests: list[dict[str, Any]] = []
    task_calls: list[tuple[Any, Any]] = []

    async def append_messages(self, thread_id, messages):
        previous = await self.aget_history(thread_id, limit=100_000)
        positions = {message.id: index for index, message in enumerate(previous) if message.id}
        appended = []
        for message in messages:
            extra = dict(message.additional_kwargs)
            extra.setdefault("checkpoint_ts", int(datetime.now(UTC).timestamp() * 1000))
            prepared = message.model_copy(update={"additional_kwargs": extra})
            appended.append(prepared)
            if prepared.id in positions:
                previous[positions[prepared.id]] = prepared
            else:
                positions[prepared.id] = len(previous)
                previous.append(prepared)
        self.seed_thread_messages(thread_id, previous)
        return appended

    async def resident_stream(self, request):
        previous = await self.aget_history(request["thread_id"], limit=100_000)
        incoming = convert_to_messages(request["messages"])
        row = server.services.agent_repo.get(binding["agent_id"])
        guard = ResidentTextOnlyMiddleware("en", row.user_id, binding["thread_id"])
        system = SystemMessage(content="Resident baseline.")
        model_request = SimpleNamespace(
            system_message=system,
            override=lambda **changes: SimpleNamespace(
                **{"system_message": system, "tools": [], **changes}
            ),
        )
        prepared = guard._prepare(model_request)
        requests.append(
            {
                "request": request,
                "previous": previous,
                "incoming": incoming,
                "model_system": prepared.system_message.content,
            }
        )
        answer = AIMessage(content="Resident answer.", id=f"resident-model:{_key()}")
        self.seed_thread_messages(request["thread_id"], [*previous, *incoming, answer])
        yield {"type": "token", "content": answer.content, "message_id": answer.id}
        yield {
            "type": "state_snapshot",
            "data": {"messages": [*previous, *incoming, answer]},
        }

    async def task_run(services, owner, turn, history):
        task_calls.append((turn, history))
        yield {"type": "token", "content": "Delegated result."}

    monkeypatch.setattr(FakeHarnessAgent, "aappend_messages", append_messages, raising=False)
    monkeypatch.setattr(FakeHarnessAgent, "stream", resident_stream)
    monkeypatch.setattr(task_runtime, "run_turn", task_run)
    binding = await _json(
        await client.put("/api/agents/resident-conversation", headers=auth, json={})
    )
    base = f"/api/agents/{binding['agent_id']}/threads/{binding['thread_id']}"
    source = await _send_resident(client, auth, base, "Please help me plan this work.")
    return client, server, auth, binding, base, source, requests, task_calls


async def test_delegation_is_explicit_idempotent_and_owner_scoped(closed_loop):
    client, server, auth, binding, base, source, _requests, calls = closed_loop
    before_agents = server.services.agent_repo.list_all()
    url = f"{base}/conversation/delegations"
    body = _delegate_body(source)
    responses = await asyncio.gather(
        client.post(url, headers=auth, json=body),
        client.post(url, headers=auth, json=body),
    )
    created, replay = [await _json(response, 202) for response in responses]
    assert replay["delegation_id"] == created["delegation_id"]
    assert replay["task_id"] == created["task_id"]
    task = await _task(client, auth, created["task_id"])
    assert task["project_id"] is None
    assert task["turns"][0]["turn_id"] == created["initial_turn_id"]
    assert len(calls) == 1
    assert server.services.agent_repo.list_all() == before_agents
    changed = {**body, "message": {**body["message"], "prompt": "Different work."}}
    assert (await client.post(url, headers=auth, json=changed)).status_code == 409
    implicit = {key: value for key, value in _delegate_body(source).items() if key != "project_id"}
    assert (await client.post(url, headers=auth, json=implicit)).status_code == 422
    stranger = await create_user(client, auth, username="delegate-stranger")
    for path in (url, f"{url}/{created['delegation_id']}"):
        assert (await client.get(path, headers=stranger)).status_code == 404
    assert (await client.post(url, headers=stranger, json=body)).status_code == 404
    assert binding["thread_id"] != created["task_id"]


async def test_explicit_memory_management_is_private_and_keeps_superseded_audit(closed_loop):
    client, _server, auth, _binding, base, _source, requests, _calls = closed_loop
    created = await _json(
        await client.post(
            f"{base}/memory",
            headers=auth,
            json={"assertion": "Use concise explanations.", "kind": "Preference"},
        ),
        201,
    )
    atom_id = created["atom_id"]
    assert created["source_event_ids"] and created["quote_event_id"]
    await _send_resident(client, auth, base, "Respond using my current preferences.")
    assert "Use concise explanations." in requests[-1]["model_system"]
    assert "Use concise explanations." not in json.dumps(requests[-1]["request"], default=str)
    stranger = await create_user(client, auth, username="memory-stranger")
    assert (await client.get(f"{base}/memory", headers=stranger)).status_code == 404
    for suffix, body in (
        ("", {"assertion": "Steal private memory.", "kind": "Fact"}),
        (f"/{atom_id}:replace", {"assertion": "Alter private memory."}),
        (f"/{atom_id}:deprecate", {}),
    ):
        response = await client.post(f"{base}/memory{suffix}", headers=stranger, json=body)
        assert response.status_code == 404, response.text
    replacement = await _json(
        await client.post(
            f"{base}/memory/{atom_id}:replace",
            headers=auth,
            json={"assertion": "Use detailed explanations when requested."},
        )
    )
    assert replacement["atom_id"] != atom_id
    active = await _json(await client.get(f"{base}/memory", headers=auth))
    assert [item["atom_id"] for item in active["items"]] == [replacement["atom_id"]]
    await _send_resident(client, auth, base, "Respond after my correction.")
    assert "Use detailed explanations when requested." in requests[-1]["model_system"]
    assert "Use concise explanations." not in requests[-1]["model_system"]
    for _ in range(2):
        removed = await _json(
            await client.post(
                f"{base}/memory/{replacement['atom_id']}:deprecate",
                headers=auth,
                json={"reason": "No longer preferred."},
            )
        )
        assert removed["deprecated_at"]
    assert (await _json(await client.get(f"{base}/memory", headers=auth)))["items"] == []
    await _send_resident(client, auth, base, "Respond after I removed the preference.")
    assert "Use detailed explanations when requested." not in requests[-1]["model_system"]
    assert "Use concise explanations." not in requests[-1]["model_system"]
    audit = await _json(
        await client.get(f"{base}/memory", headers=auth, params={"include_deprecated": True})
    )
    assert {item["atom_id"] for item in audit["items"]} == {atom_id, replacement["atom_id"]}
    original = next(item for item in audit["items"] if item["atom_id"] == atom_id)
    assert original["superseded_by"] == replacement["atom_id"]


async def test_delegation_rejects_foreign_project_and_existing_task_identity_atomically(
    closed_loop,
):
    client, _server, auth, _binding, base, source, _requests, calls = closed_loop
    other = await create_user(client, auth, username="project-stranger")
    project = await _json(
        await client.post(
            "/api/projects",
            headers={**other, "Idempotency-Key": _key()},
            json={"name": "Other user's project"},
        ),
        201,
    )
    foreign = await client.post(
        f"{base}/conversation/delegations",
        headers=auth,
        json=_delegate_body(source, project_id=project["project_id"]),
    )
    assert foreign.status_code == 404, foreign.text
    original_request_id = _key()
    ordinary = await _json(
        await client.post(
            "/api/project-tasks",
            headers=auth,
            json={"request_id": original_request_id, "prompt": "Existing independent task."},
        ),
        202,
    )
    await _task(client, auth, ordinary["task_id"])
    body = _delegate_body(source)
    body["message"]["request_id"] = original_request_id
    collision = await client.post(f"{base}/conversation/delegations", headers=auth, json=body)
    assert collision.status_code == 409, collision.text
    listing = await _json(await client.get(f"{base}/conversation/delegations", headers=auth))
    assert listing["items"] == []
    tasks = await _json(await client.get("/api/project-tasks", headers=auth))
    assert len(tasks["items"]) == 1 and len(calls) == 1


async def test_task_receives_only_explicit_brief_and_result_is_next_turn_context(closed_loop):
    client, server, auth, binding, base, source, requests, calls = closed_loop
    secret = "PRIVATE-ORCHID-8872"
    memory = await _json(
        await client.post(
            f"{base}/memory", headers=auth, json={"kind": "Preference", "assertion": secret}
        ),
        201,
    )
    assert memory
    await _send_resident(client, auth, base, "This private discussion stays with the assistant.")
    body = _delegate_body(source, prompt="Write a concise checklist.")
    delegated = await _json(
        await client.post(f"{base}/conversation/delegations", headers=auth, json=body), 202
    )
    await _task(client, auth, delegated["task_id"])
    result = await _delegation(client, auth, base, delegated["delegation_id"])
    assert len(calls) == 1
    turn, history = calls[0]
    assert turn.prompt == "Write a concise checklist."
    assert history == []
    assert secret not in turn.prompt + turn.configuration_json + json.dumps(history)
    assert binding["agent_id"] not in turn.configuration_json
    report = result["reports"][0]
    assert report["status"] == "completed"
    assert report["text"] == "Delegated result."
    await _send_resident(client, auth, base, "What did the delegated task return?")
    prior = requests[-1]["previous"]
    report_messages = [message for message in prior if message.id == report["message_id"]]
    assert len(report_messages) == 1
    assert report_messages[0].type == "ai"
    assert "Delegated result." in report_messages[0].content
    assert report_messages[0].additional_kwargs["task_report"]["task_id"] == delegated["task_id"]


async def test_failed_retry_reports_each_attempt_but_plain_followup_does_not(
    closed_loop, monkeypatch
):
    client, _server, auth, _binding, base, source, _requests, calls = closed_loop

    async def fail(services, owner, turn, history):
        calls.append((turn, history))
        yield {"type": "token", "content": "Saved partial result."}
        raise RuntimeError("internal-secret-must-not-be-a-report")

    monkeypatch.setattr(task_runtime, "run_turn", fail)
    delegated = await _json(
        await client.post(
            f"{base}/conversation/delegations", headers=auth, json=_delegate_body(source)
        ),
        202,
    )
    failed = await _task(client, auth, delegated["task_id"], "failed")
    initial = await _delegation(client, auth, base, delegated["delegation_id"])
    assert initial["reports"][0]["status"] == "failed"
    assert "internal-secret" not in json.dumps(initial)

    async def succeed(services, owner, turn, history):
        calls.append((turn, history))
        yield {"type": "token", "content": "Retry succeeded."}

    monkeypatch.setattr(task_runtime, "run_turn", succeed)
    retry_id = _key()
    retry_path = (
        f"/api/project-tasks/{delegated['task_id']}/turns/{failed['turns'][0]['turn_id']}/retry"
    )
    retry = await _json(
        await client.post(retry_path, headers=auth, json={"request_id": retry_id}), 202
    )
    await _task(client, auth, delegated["task_id"])
    reported = await _delegation(client, auth, base, delegated["delegation_id"], reports=2)
    assert [report["status"] for report in reported["reports"]] == ["failed", "completed"]
    assert len({report["message_id"] for report in reported["reports"]}) == 2
    assert reported["reports"][0] == initial["reports"][0]
    assert retry["task_id"] == delegated["task_id"]
    await _json(await client.post(retry_path, headers=auth, json={"request_id": retry_id}), 202)
    await _json(
        await client.post(
            f"/api/project-tasks/{delegated['task_id']}/messages",
            headers=auth,
            json={"request_id": _key(), "prompt": "A separate ordinary followup."},
        ),
        202,
    )
    await _task(client, auth, delegated["task_id"])
    # A complete worker/report polling interval confirms this was not added to the retry chain.
    await asyncio.sleep(0.3)
    unchanged = await _delegation(client, auth, base, delegated["delegation_id"], reports=2)
    assert unchanged["reports"] == reported["reports"]
    assert len(calls) == 3


async def test_cancelling_resident_turn_does_not_cancel_delegated_task(closed_loop, monkeypatch):
    client, server, auth, _binding, base, source, _requests, _calls = closed_loop
    task_entered, task_release = asyncio.Event(), asyncio.Event()
    resident_entered = asyncio.Event()

    async def blocked_task(*args):
        task_entered.set()
        yield {"type": "token", "content": "Task partial."}
        await task_release.wait()
        yield {"type": "token", "content": " Task finished."}

    async def blocked_resident(_message):
        resident_entered.set()
        yield {"type": "token", "content": "Resident partial."}
        await asyncio.Event().wait()

    monkeypatch.setattr(task_runtime, "run_turn", blocked_task)
    delegated = await _json(
        await client.post(
            f"{base}/conversation/delegations", headers=auth, json=_delegate_body(source)
        ),
        202,
    )
    await asyncio.wait_for(task_entered.wait(), 5)
    monkeypatch.setattr(server.app_runtime.gateway.processor, "iter_turn_chunks", blocked_resident)
    resident = await _json(
        await client.post(
            f"{base}/messages", headers=auth, json={"request_id": _key(), "prompt": "Wait."}
        ),
        202,
    )
    try:
        await asyncio.wait_for(resident_entered.wait(), 5)
        await _json(
            await client.post(
                f"{base}/cancel",
                headers=auth,
                json={"request_id": _key(), "turn_id": resident["turn_id"]},
            )
        )
        await _resident_turn(client, auth, base, resident["request_id"], "cancelled")
        task = await _json(
            await client.get(f"/api/project-tasks/{delegated['task_id']}", headers=auth)
        )
        assert task["status"] == "running" and not task["turns"][0]["cancel_requested"]
    finally:
        task_release.set()
    await _task(client, auth, delegated["task_id"])
    returned = await _delegation(client, auth, base, delegated["delegation_id"])
    assert returned["reports"][0]["status"] == "completed"


async def test_queued_task_cancellation_returns_a_single_cancelled_report(closed_loop, monkeypatch):
    client, server, auth, _binding, base, source, _requests, calls = closed_loop
    monkeypatch.setattr(server.services.project_task_turn_repo, "claim", lambda *_args: None)
    delegated = await _json(
        await client.post(
            f"{base}/conversation/delegations", headers=auth, json=_delegate_body(source)
        ),
        202,
    )
    await _json(
        await client.post(
            f"/api/project-tasks/{delegated['task_id']}/cancel",
            headers=auth,
            json={"turn_id": delegated["initial_turn_id"]},
        )
    )
    result = await _delegation(client, auth, base, delegated["delegation_id"])
    assert result["reports"][0]["status"] == "cancelled"
    assert not calls


async def test_recoverable_task_interruption_is_not_a_false_terminal_report(
    closed_loop, monkeypatch
):
    client, _server, auth, _binding, base, source, _requests, _calls = closed_loop

    async def interrupted(*args):
        yield {"type": "token", "content": "Retained work."}
        raise TaskInterruption(
            TaskRecovery(
                can_resume=True,
                phase="model",
                reason_code="TASK_INTERRUPTED",
                cause_type="ConnectionError",
                diagnostic_id="diag_closed_loop",
            ),
            "prepare",
        )

    monkeypatch.setattr(task_runtime, "run_turn", interrupted)
    delegated = await _json(
        await client.post(
            f"{base}/conversation/delegations", headers=auth, json=_delegate_body(source)
        ),
        202,
    )
    await _task(client, auth, delegated["task_id"], "interrupted")
    await asyncio.sleep(0.3)
    result = await _delegation(client, auth, base, delegated["delegation_id"], reports=0)
    assert not result["reports"]


@pytest.mark.parametrize("report_history_v2", [False, True])
async def test_report_checkpoint_retry_after_shutdown_is_idempotent(
    closed_loop, monkeypatch, report_history_v2
):
    client, server, auth, binding, base, source, requests, _calls = closed_loop
    assert (server.app_runtime.history_archive is not None) is report_history_v2
    original = FakeHarnessAgent.aappend_messages
    checkpoint_written = asyncio.Event()

    async def append_then_lose_receipt(self, thread_id, messages):
        appended = await original(self, thread_id, messages)
        if any(message.additional_kwargs.get("task_report") for message in messages):
            checkpoint_written.set()
            raise asyncio.CancelledError
        return appended

    monkeypatch.setattr(FakeHarnessAgent, "aappend_messages", append_then_lose_receipt)
    delegated = await _json(
        await client.post(
            f"{base}/conversation/delegations", headers=auth, json=_delegate_body(source)
        ),
        202,
    )
    await _task(client, auth, delegated["task_id"])
    runtime = server.app_runtime.resident_conversations
    try:
        await asyncio.wait_for(checkpoint_written.wait(), 5)
    finally:
        await runtime.close()
    pending = await _delegation(client, auth, base, delegated["delegation_id"], delivered=False)
    assert pending["reports"][0]["delivery_status"] == "pending"
    monkeypatch.setattr(FakeHarnessAgent, "aappend_messages", original)
    await runtime.start()
    delivered = await _delegation(client, auth, base, delegated["delegation_id"])
    report = delivered["reports"][0]
    await _send_resident(client, auth, base, "Use the returned result.")
    assert sum(message.id == report["message_id"] for message in requests[-1]["previous"]) == 1
    async with asyncio.timeout(10):
        while True:
            canonical = await _json(await client.get(f"{base}/history", headers=auth))
            if not canonical["history_loading"]:
                break
            await asyncio.sleep(0.01)
    assert canonical["thread_id"] == binding["thread_id"]
    assert sum(message["id"] == report["message_id"] for message in canonical["messages"]) == 1
    reports = server.services.resident_task_report_repo.pending()
    assert not reports


async def test_history_search_paging_and_report_survive_task_deletion(closed_loop):
    client, _server, auth, binding, base, source, _requests, _calls = closed_loop
    await _send_resident(client, auth, base, "Find the literal %_ marker.")
    await _send_resident(client, auth, base, "An unrelated later turn.")
    delegated = await _json(
        await client.post(
            f"{base}/conversation/delegations", headers=auth, json=_delegate_body(source)
        ),
        202,
    )
    await _task(client, auth, delegated["task_id"])
    reported = await _delegation(client, auth, base, delegated["delegation_id"])
    history_base = f"{base}/conversation/history"
    days = await _json(await client.get(f"{history_base}/days", headers=auth))
    assert days["days"][0]["item_count"] == 4
    assert days["days"][0]["timezones"]
    first = await _json(await client.get(history_base, headers=auth, params={"limit": 2}))
    assert len(first["items"]) == 2 and first["next_cursor"]
    second = await _json(
        await client.get(
            history_base, headers=auth, params={"limit": 2, "cursor": first["next_cursor"]}
        )
    )
    assert len(second["items"]) == 2 and second["next_cursor"] is None
    combined = [*second["items"], *first["items"]]
    assert len({item["item_id"] for item in combined}) == 4
    report_item = next(item for item in combined if item["kind"] == "task_report")
    located = await _json(
        await client.get(f"{history_base}/items/{report_item['item_id']}", headers=auth)
    )
    assert located["anchor_item_id"] == report_item["item_id"]
    assert sum(item["item_id"] == report_item["item_id"] for item in located["items"]) == 1
    search = await _json(
        await client.get(f"{history_base}/search", headers=auth, params={"q": "%_"})
    )
    assert len(search["items"]) == 1
    assert search["items"][0]["turn"]["prompt"] == "Find the literal %_ marker."
    wrong_use = await client.get(
        f"{history_base}/search",
        headers=auth,
        params={"q": "literal", "cursor": first["next_cursor"]},
    )
    assert wrong_use.status_code == 409
    assert binding["thread_id"] == first["thread_id"]
    deleted = await client.delete(f"/api/project-tasks/{delegated['task_id']}", headers=auth)
    assert deleted.status_code == 204, deleted.text
    retained = await _delegation(client, auth, base, delegated["delegation_id"])
    assert retained["reports"] == reported["reports"]
    retained_history = await _json(
        await client.get(f"{history_base}/items/{report_item['item_id']}", headers=auth)
    )
    assert any(item["kind"] == "task_report" for item in retained_history["items"])


async def test_shutdown_drains_an_admitted_report_write_before_releasing_thread(
    closed_loop, monkeypatch
):
    client, server, auth, _binding, base, source, requests, _calls = closed_loop
    original = FakeHarnessAgent.aappend_messages
    admitted, release = asyncio.Event(), asyncio.Event()

    async def admitted_append(self, thread_id, messages):
        appended = await original(self, thread_id, messages)
        if any(message.additional_kwargs.get("task_report") for message in messages):
            admitted.set()
            await release.wait()
        return appended

    monkeypatch.setattr(FakeHarnessAgent, "aappend_messages", admitted_append)
    delegated = await _json(
        await client.post(
            f"{base}/conversation/delegations", headers=auth, json=_delegate_body(source)
        ),
        202,
    )
    runtime = server.app_runtime.resident_conversations
    shutdown = None
    try:
        await asyncio.wait_for(admitted.wait(), 5)
        shutdown = asyncio.create_task(runtime.close())
        await asyncio.sleep(0.05)
        assert not shutdown.done()
    finally:
        release.set()
        if shutdown is not None:
            await asyncio.wait_for(shutdown, 5)
    monkeypatch.setattr(FakeHarnessAgent, "aappend_messages", original)
    await runtime.start()
    returned = await _delegation(client, auth, base, delegated["delegation_id"])
    report = returned["reports"][0]
    await _send_resident(client, auth, base, "Continue after the server stopped.")
    assert sum(message.id == report["message_id"] for message in requests[-1]["previous"]) == 1


@pytest.mark.parametrize("archive_transition", [False, True])
async def test_archive_retry_across_history_format_change_keeps_one_report(
    closed_loop, monkeypatch, archive_transition
):
    client, server, auth, _binding, base, source, _requests, _calls = closed_loop
    archive = server.app_runtime.history_archive
    assert archive is not None
    archive.enabled = archive_transition
    repo = server.services.resident_task_report_repo
    original = repo.mark_archived
    committed = threading.Event()

    def lose_archive_receipt(*args):
        committed.set()
        raise OSError("lost the archive acknowledgement")

    monkeypatch.setattr(repo, "mark_archived", lose_archive_receipt)
    delegated = await _json(
        await client.post(
            f"{base}/conversation/delegations", headers=auth, json=_delegate_body(source)
        ),
        202,
    )
    runtime = server.app_runtime.resident_conversations
    try:
        assert await asyncio.to_thread(committed.wait, 5)
    finally:
        await runtime.close()
    monkeypatch.setattr(repo, "mark_archived", original)
    archive.enabled = not archive_transition
    await runtime.start()
    returned = await _delegation(client, auth, base, delegated["delegation_id"])
    report = returned["reports"][0]
    async with asyncio.timeout(10):
        while True:
            canonical = await _json(await client.get(f"{base}/history", headers=auth))
            if not canonical["history_loading"]:
                break
            await asyncio.sleep(0.01)
    assert sum(message["id"] == report["message_id"] for message in canonical["messages"]) == 1


async def test_closed_loop_routes_require_auth_and_publish_typed_schemas(closed_loop):
    client, _server, auth, _binding, base, source, _requests, _calls = closed_loop
    for method, suffix, body in (
        ("GET", "/conversation/history", None),
        ("GET", "/conversation/history/days", None),
        ("GET", "/conversation/history/search?q=work", None),
        ("GET", f"/conversation/history/items/turn:{source['turn_id']}", None),
        ("GET", "/memory", None),
        ("POST", "/memory", {"assertion": "Remember this.", "kind": "Fact"}),
        ("POST", "/memory/unknown:replace", {"assertion": "Replacement."}),
        ("POST", "/memory/unknown:deprecate", {}),
        ("GET", "/conversation/delegations", None),
        ("POST", "/conversation/delegations", _delegate_body(source)),
        ("GET", "/conversation/delegations/unknown", None),
    ):
        response = await client.request(method, base + suffix, json=body)
        assert response.status_code in {401, 403}, (method, suffix, response.text)
    docs = await client.get("/api/docs", headers=auth)
    assert docs.status_code == 200 and "/api/openapi.json" in docs.text
    schema = await _json(await client.get("/api/openapi.json", headers=auth))
    template = "/api/agents/{agent_id}/threads/{thread_id}"
    for suffix, method, status in (
        ("/conversation/history", "get", "200"),
        ("/conversation/history/days", "get", "200"),
        ("/conversation/history/search", "get", "200"),
        ("/conversation/history/items/{item_id}", "get", "200"),
        ("/memory", "get", "200"),
        ("/memory", "post", "201"),
        ("/memory/{atom_id}:replace", "post", "200"),
        ("/memory/{atom_id}:deprecate", "post", "200"),
        ("/conversation/delegations", "get", "200"),
        ("/conversation/delegations", "post", "202"),
        ("/conversation/delegations/{delegation_id}", "get", "200"),
    ):
        operation = schema["paths"][template + suffix][method]
        assert operation["summary"]
        response_schema = operation["responses"][status]["content"]["application/json"]["schema"]
        assert response_schema["$ref"].startswith("#/components/schemas/")


async def test_admin_cannot_read_another_users_private_data_through_legacy_routes(
    env_with_provider,
):
    client, _server, admin = env_with_provider
    alice = await create_user(client, admin, username="private-alice")
    alice_id = await resolve_user_id(client, admin, "private-alice")
    binding = await _json(
        await client.put("/api/agents/resident-conversation", headers=alice, json={})
    )
    base = f"/api/agents/{binding['agent_id']}/threads/{binding['thread_id']}"
    secret = "Alice-only private assertion 1827."
    memory = await _json(
        await client.post(
            f"{base}/memory", headers=alice, json={"assertion": secret, "kind": "Fact"}
        ),
        201,
    )
    await _send_resident(client, alice, base, "Alice-only private conversation 4182.")
    memory_path = f"/api/agents/{binding['agent_id']}/memory/atoms/list"
    owned_memories = await _json(await client.post(memory_path, headers=alice, json={}))
    assert secret in json.dumps(owned_memories)
    owned_history = await _json(await client.get(f"{base}/history", headers=alice))
    assert "Alice-only private conversation 4182." in json.dumps(owned_history)
    for params in ({}, {"as_user": alice_id}):
        denied_memory = await client.post(memory_path, headers=admin, params=params, json={})
        assert denied_memory.status_code == 404, denied_memory.text
        denied_atom = await client.get(
            f"/api/agents/{binding['agent_id']}/memory/atoms/{memory['atom_id']}",
            headers=admin,
            params=params,
        )
        assert denied_atom.status_code == 404, denied_atom.text
        denied_history = await client.get(f"{base}/history", headers=admin, params=params)
        assert denied_history.status_code == 404, denied_history.text
