"""Durable resident-conversation identities, receipt polling, and turn isolation."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
import pytest
from langchain_core.messages import AIMessage, convert_to_messages

from tests.support.app import octop_client
from tests.support.auth import auth_header, bootstrap_admin, create_user, seed_openai_provider
from tests.support.fakes import FakeHarnessAgent


def _request_id() -> str:
    return str(uuid4())


def _base(binding: dict[str, Any]) -> str:
    return f"/api/agents/{binding['agent_id']}/threads/{binding['thread_id']}"


@pytest.fixture
async def resident_docs_env(tmp_octop_home, monkeypatch):
    monkeypatch.setenv("OCTOP_ENABLE_API_DOCS", "true")
    async with octop_client(tmp_octop_home) as (client, server):
        await bootstrap_admin(client, tmp_octop_home)
        auth = await auth_header(client)
        await seed_openai_provider(client, auth)
        yield client, server, auth


async def _binding(client: httpx.AsyncClient, auth: dict[str, str]) -> dict[str, Any]:
    response = await client.put("/api/agents/resident-conversation", headers=auth, json={})
    assert response.status_code in {200, 201, 202}, response.text
    async with asyncio.timeout(10):
        while True:
            response = await client.get("/api/agents/resident-conversation", headers=auth)
            assert response.status_code == 200, response.text
            binding = response.json()
            if binding["status"] == "ready":
                assert binding["agent_id"] and binding["thread_id"]
                return binding
            assert not binding.get("error_code"), response.text
            await asyncio.sleep(0.01)


async def _turn(
    client: httpx.AsyncClient,
    auth: dict[str, str],
    base: str,
    request_id: str,
    status: str,
) -> dict[str, Any]:
    async with asyncio.timeout(10):
        while True:
            response = await client.get(f"{base}/requests/{request_id}", headers=auth)
            assert response.status_code == 200, response.text
            turn = response.json()
            if turn["status"] == status:
                return turn
            assert turn["status"] not in {
                "completed",
                "failed",
                "cancelled",
                "interrupted",
            }, turn
            await asyncio.sleep(0.01)


def _assert_turn_contract(turn: dict[str, Any]) -> None:
    for key in ("turn_id", "request_id", "user_message_id", "assistant_message_id"):
        assert isinstance(turn[key], str) and turn[key]
    assert turn["user_message_id"] != turn["assistant_message_id"]
    for key in ("created_at", "updated_at"):
        timestamp = datetime.fromisoformat(turn[key].replace("Z", "+00:00"))
        assert timestamp.utcoffset() == timedelta(0)
    assert "error_code" in turn and "error_message" in turn


async def test_server_prepares_binding_before_client_read_and_repair_is_idempotent(
    env_with_provider,
):
    client, server, auth = env_with_provider
    before = server.services.agent_repo.list_all()
    initial = await client.get("/api/agents/resident-conversation", headers=auth)
    assert initial.status_code == 200, initial.text
    assert initial.json()["status"] == "ready"
    assert initial.json()["agent_id"]
    assert server.services.agent_repo.list_all() == before

    first, second = await asyncio.gather(_binding(client, auth), _binding(client, auth))
    assert (first["agent_id"], first["thread_id"]) == (
        second["agent_id"],
        second["thread_id"],
    )
    assert first["timezone"] == server.config.default_timezone
    assert first["name"]
    assert len(server.services.agent_repo.list_all()) == len(before)
    assert first["agent_id"] == initial.json()["agent_id"]
    assert first["thread_id"] == initial.json()["thread_id"]
    agent = server.services.agent_repo.get(first["agent_id"])
    assert agent is not None and not agent.is_shared
    thread = server.app_runtime.gateway.thread_registry.get_thread(first["thread_id"])
    assert thread is not None and thread.agent_id == agent.agent_id
    assert thread.user_id == agent.user_id

    refreshed = await client.get("/api/agents/resident-conversation", headers=auth)
    assert refreshed.json()["thread_id"] == first["thread_id"]
    assert refreshed.headers["cache-control"] == "no-store"


async def test_new_user_can_send_using_get_binding_without_initialization_request(
    env_with_provider, monkeypatch
):
    client, server, auth = env_with_provider
    other_auth = await create_user(client, auth, username="automatic-steward-owner")
    response = await client.get("/api/agents/resident-conversation", headers=other_auth)
    assert response.status_code == 200
    binding = response.json()
    assert binding["status"] == "ready"
    owner = server.services.agent_repo.get(binding["agent_id"]).user_id
    assert server.services.resident_conversation_repo.get(owner).thread_id == binding["thread_id"]

    async def stream(message):
        yield {"type": "token", "content": "Ready to help."}
        yield {"type": "done"}

    monkeypatch.setattr(server.app_runtime.gateway.processor, "iter_turn_chunks", stream)
    request_id = _request_id()
    base = _base(binding)
    sent = await client.post(
        f"{base}/messages",
        headers=other_auth,
        json={"request_id": request_id, "prompt": "Help me plan my day."},
    )
    assert sent.status_code == 202
    completed = await _turn(client, other_auth, base, request_id, "completed")
    assert completed["reply"] == "Ready to help."


async def test_server_start_prepares_existing_users_without_replacing_ready_identity(
    tmp_octop_home,
):
    async with octop_client(tmp_octop_home) as (client, server):
        await bootstrap_admin(client, tmp_octop_home)
        auth = await auth_header(client)
        original = (await client.get("/api/agents/resident-conversation", headers=auth)).json()
        legacy_owner = server.services.user_repo.create(
            username="offline-existing-owner", password_hash="unused", role="user"
        )
        disabled_owner = server.services.user_repo.create(
            username="disabled-offline-owner", password_hash="unused", role="user"
        )
        server.services.user_repo.set_disabled(disabled_owner, True)
        assert server.services.resident_conversation_repo.get(legacy_owner) is None
    async with octop_client(tmp_octop_home) as (client, server):
        prepared = server.services.resident_conversation_repo.get(legacy_owner)
        assert prepared is not None and prepared.status == "ready"
        assert server.services.resident_conversation_repo.get(disabled_owner) is None
        auth = await auth_header(client)
        current = (await client.get("/api/agents/resident-conversation", headers=auth)).json()
        assert current["agent_id"] == original["agent_id"]
        assert current["thread_id"] == original["thread_id"]
        assert current["status"] == "ready"


async def test_resident_message_acceptance_is_idempotent_and_does_not_accept_changed_prompt(
    env_with_provider, monkeypatch
):
    client, server, auth = env_with_provider
    binding = await _binding(client, auth)
    base = _base(binding)
    entered, release = asyncio.Event(), asyncio.Event()
    invocations = []

    async def stream(message):
        invocations.append(message)
        entered.set()
        yield {"type": "token", "content": "First "}
        await release.wait()
        yield {"type": "token", "content": "answer."}
        yield {"type": "done"}

    monkeypatch.setattr(server.app_runtime.gateway.processor, "iter_turn_chunks", stream)
    request_id = _request_id()
    body = {"request_id": request_id, "prompt": "Remember this conversation."}
    try:
        accepted, concurrent_replay = await asyncio.gather(
            client.post(f"{base}/messages", headers=auth, json=body),
            client.post(f"{base}/messages", headers=auth, json=body),
        )
        assert accepted.status_code == 202, accepted.text
        original = accepted.json()
        _assert_turn_contract(original)
        assert concurrent_replay.status_code in {200, 202}, concurrent_replay.text
        assert concurrent_replay.json()["turn_id"] == original["turn_id"]
        await asyncio.wait_for(entered.wait(), 5)

        duplicate = await client.post(f"{base}/messages", headers=auth, json=body)
        assert duplicate.status_code in {200, 202}, duplicate.text
        assert duplicate.json()["turn_id"] == original["turn_id"]
        assert duplicate.json()["user_message_id"] == original["user_message_id"]
        changed = await client.post(
            f"{base}/messages", headers=auth, json={**body, "prompt": "Different work."}
        )
        assert changed.status_code == 409, changed.text
        busy = await client.post(
            f"{base}/messages",
            headers=auth,
            json={"request_id": _request_id(), "prompt": "Overlapping work."},
        )
        assert busy.status_code == 409, busy.text
    finally:
        release.set()
    completed = await _turn(client, auth, base, request_id, "completed")
    assert completed["reply"] == "First answer."
    assert completed["prompt"] == body["prompt"]
    assert len(invocations) == 1
    assert invocations[0].metadata["thread_id"] == binding["thread_id"]
    assert completed["error_code"] is None and completed["error_message"] is None
    after = await client.post(f"{base}/messages", headers=auth, json=body)
    assert after.json()["turn_id"] == completed["turn_id"]
    assert len(invocations) == 1


async def test_closing_the_http_client_after_acceptance_keeps_the_resident_turn_running(
    env_with_provider, monkeypatch
):
    client, server, auth = env_with_provider
    base = _base(await _binding(client, auth))
    entered, release, stream_cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    invocations = []

    async def stream(message):
        invocations.append(message)
        entered.set()
        yield {"type": "token", "content": "Saved "}
        try:
            await release.wait()
        except asyncio.CancelledError:
            stream_cancelled.set()
            raise
        yield {"type": "token", "content": "after disconnect."}
        yield {"type": "done"}

    monkeypatch.setattr(server.app_runtime.gateway.processor, "iter_turn_chunks", stream)
    request_id = _request_id()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=client._octop_app), base_url="http://testserver"
        ) as separate_client:
            response = await separate_client.post(
                f"{base}/messages",
                headers=auth,
                json={"request_id": request_id, "prompt": "Continue independently."},
            )
            assert response.status_code == 202, response.text
            await asyncio.wait_for(entered.wait(), 5)
        running = await client.get(f"{base}/requests/{request_id}", headers=auth)
        assert running.status_code == 200
        assert running.json()["status"] == "running"
        assert not stream_cancelled.is_set()
    finally:
        release.set()
    result = await _turn(client, auth, base, request_id, "completed")
    assert result["reply"] == "Saved after disconnect."
    assert len(invocations) == 1 and not stream_cancelled.is_set()


async def test_resident_followups_keep_the_thread_and_snapshot_only_returns_the_requested_tail(
    env_with_provider, monkeypatch
):
    client, server, auth = env_with_provider
    binding = await _binding(client, auth)
    base = _base(binding)
    invocations = []

    async def stream(message):
        invocations.append(message)
        yield {"type": "token", "content": f"Reply to {message.text}"}
        yield {"type": "done"}

    monkeypatch.setattr(server.app_runtime.gateway.processor, "iter_turn_chunks", stream)
    turns = []
    for prompt in ("First topic", "A followup", "Third question"):
        request_id = _request_id()
        response = await client.post(
            f"{base}/messages", headers=auth, json={"request_id": request_id, "prompt": prompt}
        )
        assert response.status_code == 202, response.text
        turns.append(await _turn(client, auth, base, request_id, "completed"))
    snapshot_response = await client.get(f"{base}/conversation", headers=auth, params={"limit": 2})
    assert snapshot_response.status_code == 200, snapshot_response.text
    snapshot = snapshot_response.json()
    assert snapshot["agent_id"] == binding["agent_id"]
    assert snapshot["thread_id"] == binding["thread_id"]
    assert snapshot["active_turn_id"] is None
    assert snapshot["has_earlier"] is True
    assert snapshot["revision"] >= 1
    assert [turn["turn_id"] for turn in snapshot["turns"]] == [
        turn["turn_id"] for turn in turns[-2:]
    ]
    assert all(message.metadata["thread_id"] == binding["thread_id"] for message in invocations)
    assert len({turn["user_message_id"] for turn in turns}) == 3
    assert len({turn["assistant_message_id"] for turn in turns}) == 3
    assert snapshot_response.headers["cache-control"] == "no-store"
    first = await client.get(f"{base}/requests/{turns[0]['request_id']}", headers=auth)
    assert first.json()["reply"] == "Reply to First topic"


async def test_resident_conversation_and_request_endpoints_hide_other_users_resources(
    env_with_provider, monkeypatch
):
    client, server, auth = env_with_provider
    binding = await _binding(client, auth)
    base = _base(binding)

    async def stream(_message):
        yield {"type": "token", "content": "Private reply"}
        yield {"type": "done"}

    monkeypatch.setattr(server.app_runtime.gateway.processor, "iter_turn_chunks", stream)
    request_id = _request_id()
    created = await client.post(
        f"{base}/messages",
        headers=auth,
        json={"request_id": request_id, "prompt": "Private prompt"},
    )
    assert created.status_code == 202, created.text
    turn = await _turn(client, auth, base, request_id, "completed")
    alice = await create_user(client, auth, username="resident-alice")
    for method, path, body in (
        ("GET", f"{base}/conversation", None),
        ("GET", f"{base}/requests/{request_id}", None),
        ("POST", f"{base}/messages", {"request_id": _request_id(), "prompt": "Take over"}),
        ("POST", f"{base}/cancel", {"request_id": _request_id(), "turn_id": turn["turn_id"]}),
    ):
        response = await client.request(method, path, headers=alice, json=body)
        assert response.status_code == 404, (path, response.text)
        assert "Private prompt" not in response.text and "Private reply" not in response.text
    alice_binding = await _binding(client, alice)
    assert alice_binding["agent_id"] != binding["agent_id"]
    assert alice_binding["thread_id"] != binding["thread_id"]


async def test_old_cancel_receipt_cannot_cancel_a_followup_turn(env_with_provider, monkeypatch):
    client, server, auth = env_with_provider
    base = _base(await _binding(client, auth))
    entered = [asyncio.Event(), asyncio.Event()]
    release = [asyncio.Event(), asyncio.Event()]
    invocations = []

    async def stream(message):
        index = len(invocations)
        invocations.append(message)
        entered[index].set()
        yield {"type": "token", "content": f"Partial {index}"}
        await release[index].wait()
        yield {"type": "done"}

    monkeypatch.setattr(server.app_runtime.gateway.processor, "iter_turn_chunks", stream)
    first_request = _request_id()
    cancel_request = _request_id()
    try:
        first = await client.post(
            f"{base}/messages",
            headers=auth,
            json={"request_id": first_request, "prompt": "First work"},
        )
        assert first.status_code == 202, first.text
        await asyncio.wait_for(entered[0].wait(), 5)
        cancel_body = {"request_id": cancel_request, "turn_id": first.json()["turn_id"]}
        stopped = await client.post(f"{base}/cancel", headers=auth, json=cancel_body)
        assert stopped.status_code in {200, 202}, stopped.text
        first_turn = await _turn(client, auth, base, first_request, "cancelled")

        second_request = _request_id()
        second = await client.post(
            f"{base}/messages",
            headers=auth,
            json={"request_id": second_request, "prompt": "Followup work"},
        )
        assert second.status_code == 202, second.text
        await asyncio.wait_for(entered[1].wait(), 5)
        replay = await client.post(f"{base}/cancel", headers=auth, json=cancel_body)
        assert replay.status_code in {200, 202}, replay.text
        assert replay.json()["turn_id"] == first_turn["turn_id"]
        assert replay.json()["status"] == "cancelled"
        conflict = await client.post(
            f"{base}/cancel",
            headers=auth,
            json={"request_id": cancel_request, "turn_id": second.json()["turn_id"]},
        )
        assert conflict.status_code == 409, conflict.text
        current = await client.get(f"{base}/requests/{second_request}", headers=auth)
        assert current.json()["status"] == "running"
    finally:
        for event in release:
            event.set()
    result = await _turn(client, auth, base, second_request, "completed")
    assert result["turn_id"] != first_turn["turn_id"]
    assert len(invocations) == 2


async def test_resident_error_followed_by_done_is_failed_and_keeps_partial_reply(
    env_with_provider, monkeypatch
):
    client, server, auth = env_with_provider
    base = _base(await _binding(client, auth))

    async def stream(_message):
        yield {"type": "token", "content": "Partial answer"}
        yield {
            "type": "error",
            "message": "Model unavailable",
            "error_code": "TASK_MODEL_UNAVAILABLE",
        }
        yield {"type": "done"}

    monkeypatch.setattr(server.app_runtime.gateway.processor, "iter_turn_chunks", stream)
    request_id = _request_id()
    accepted = await client.post(
        f"{base}/messages", headers=auth, json={"request_id": request_id, "prompt": "Some work"}
    )
    assert accepted.status_code == 202, accepted.text
    failed = await _turn(client, auth, base, request_id, "failed")
    assert failed["reply"] == "Partial answer"
    assert failed["error_code"]
    assert failed["error_message"]


@pytest.mark.parametrize("versioned_history", [False, True])
async def test_resident_gateway_preserves_context_and_public_history_message_identity(
    tmp_octop_home, monkeypatch, versioned_history
):
    monkeypatch.setenv("OCTOP_HISTORY_V2_ENABLED", str(versioned_history).lower())
    observed = []

    async def contextual_stream(self, request):
        thread_id = request["thread_id"]
        previous = await self.aget_history(thread_id, limit=50)
        incoming = convert_to_messages(request["messages"])
        observed.append((request, previous, incoming))
        if previous:
            earlier_users = [message.content for message in previous if message.type == "human"]
            answer = "ORCHID" if "My code is ORCHID." in earlier_users else "I do not know."
        else:
            answer = "I will remember ORCHID."
        assistant = AIMessage(content=answer, id=f"model-answer:{uuid4()}")
        messages = [*previous, *incoming, assistant]
        self.seed_thread_messages(thread_id, messages)
        yield {
            "type": "token",
            "node": "agent",
            "message_id": assistant.id,
            "content": answer,
        }
        yield {"type": "state_snapshot", "data": {"messages": messages}}

    monkeypatch.setattr(FakeHarnessAgent, "stream", contextual_stream)
    async with octop_client(tmp_octop_home) as (client, server):
        await bootstrap_admin(client, tmp_octop_home)
        auth = await auth_header(client)
        await seed_openai_provider(client, auth)
        binding = await _binding(client, auth)
        base = _base(binding)
        completed = []
        for prompt in ("My code is ORCHID.", "What code did I give you?"):
            request_id = _request_id()
            accepted = await client.post(
                f"{base}/messages", headers=auth, json={"request_id": request_id, "prompt": prompt}
            )
            assert accepted.status_code == 202, accepted.text
            completed.append(await _turn(client, auth, base, request_id, "completed"))

        assert completed[0]["reply"] == "I will remember ORCHID."
        assert completed[1]["reply"] == "ORCHID"
        assert len(observed) == 2
        assert observed[0][1] == []
        assert [message.content for message in observed[1][1]] == [
            "My code is ORCHID.",
            "I will remember ORCHID.",
        ]
        for turn, (request, _previous, incoming) in zip(completed, observed, strict=True):
            assert request["thread_id"] == binding["thread_id"]
            assert incoming[0].id == turn["user_message_id"]
            assert incoming[0].additional_kwargs["resident_request_id"] == turn["request_id"]

        async with asyncio.timeout(10):
            while True:
                response = await client.get(f"{base}/history", headers=auth, params={"limit": 10})
                assert response.status_code == 200, response.text
                history = response.json()
                if not history["history_loading"]:
                    break
                await asyncio.sleep(0.01)
        assert history["thread_id"] == binding["thread_id"]
        assert not history["has_more"]
        assert [message["role"] for message in history["messages"]] == [
            "user",
            "assistant",
            "user",
            "assistant",
        ]
        assert [message["id"] for message in history["messages"] if message["role"] == "user"] == [
            turn["user_message_id"] for turn in completed
        ]
        texts = [
            "".join(block.get("text", "") for block in message["content"])
            for message in history["messages"]
        ]
        assert texts == [
            "My code is ORCHID.",
            "I will remember ORCHID.",
            "What code did I give you?",
            "ORCHID",
        ]
        if versioned_history:
            archive = server.app_runtime.history_archive
            assert archive is not None
            segments = archive.store.segments(binding["thread_id"])
            assert any(segment["format"] == "v2" for segment in segments)


async def test_resident_routes_require_authentication_and_expose_typed_openapi(resident_docs_env):
    client, _server, auth = resident_docs_env
    base = _base(await _binding(client, auth))
    for method, path, body in (
        ("GET", "/api/agents/resident-conversation", None),
        ("PUT", "/api/agents/resident-conversation", {}),
        ("GET", f"{base}/conversation", None),
        ("GET", f"{base}/requests/{_request_id()}", None),
        ("POST", f"{base}/messages", {"request_id": _request_id(), "prompt": "Anonymous"}),
        ("POST", f"{base}/cancel", {"request_id": _request_id(), "turn_id": "unknown-turn"}),
    ):
        response = await client.request(method, path, json=body)
        assert response.status_code in {401, 403}, (method, path, response.text)

    docs = await client.get("/api/docs", headers=auth)
    assert docs.status_code == 200 and "/api/openapi.json" in docs.text
    response = await client.get("/api/openapi.json", headers=auth)
    assert response.status_code == 200, response.text
    schema = response.json()
    template = "/api/agents/{agent_id}/threads/{thread_id}"
    for path, method, status, model in (
        ("/api/agents/resident-conversation", "get", "200", "ResidentBinding"),
        ("/api/agents/resident-conversation", "put", "200", "ResidentBinding"),
        (f"{template}/conversation", "get", "200", "ResidentSnapshot"),
        (f"{template}/messages", "post", "202", "ResidentTurn"),
        (f"{template}/requests/{{request_id}}", "get", "200", "ResidentTurn"),
        (f"{template}/cancel", "post", "200", "ResidentTurn"),
    ):
        operation = schema["paths"][path][method]
        assert operation["summary"]
        response_schema = operation["responses"][status]["content"]["application/json"]["schema"]
        assert response_schema["$ref"] == f"#/components/schemas/{model}"
    message_operation = schema["paths"][f"{template}/messages"]["post"]
    request_schema = message_operation["requestBody"]["content"]["application/json"]["schema"]
    message_model = schema["components"]["schemas"][request_schema["$ref"].rsplit("/", 1)[-1]]
    assert set(message_model["required"]) == {"request_id", "prompt"}
    assert message_model["additionalProperties"] is False
