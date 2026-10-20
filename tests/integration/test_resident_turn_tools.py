"""HTTP acceptance freezes host-tool authorization for the actual resident runtime."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from langchain_core.messages import AIMessage, SystemMessage, convert_to_messages

from octop.infra.agents.resident_identity import ResidentTextOnlyMiddleware
from octop.infra.projects import runtime as task_runtime
from octop.infra.projects.tasks import now_ms
from tests.support.auth import create_user, resolve_user_id
from tests.support.fakes import FakeHarnessAgent


def _key():
    return uuid4().hex


def _json(response: httpx.Response, status=200):
    assert response.status_code == status, response.text
    return response.json()


async def _settled(client, auth, base, request_id, expected="completed"):
    async with asyncio.timeout(10):
        while True:
            turn = _json(await client.get(f"{base}/requests/{request_id}", headers=auth))
            if turn["status"] == expected:
                return turn
            assert turn["status"] in {"queued", "running"}, turn
            await asyncio.sleep(0.01)


async def _call(guard, name, arguments=None, call_id=None):
    async def no_generic_handler(request):
        raise AssertionError("A resident tool must never reach a generic execution tool.")

    return await guard.awrap_tool_call(
        SimpleNamespace(tool_call={"name": name, "args": arguments or {}, "id": call_id or _key()}),
        no_generic_handler,
    )


def _content(message):
    assert isinstance(message.content, str)
    return json.loads(message.content)


@pytest.fixture(autouse=True)
def docs_enabled(monkeypatch):
    monkeypatch.setenv("OCTOP_ENABLE_API_DOCS", "true")


@pytest.fixture
async def tools_http(env_with_provider, monkeypatch):
    client, server, auth = env_with_provider
    actions = deque()
    observed = []
    results = []
    task_calls = []

    async def append_messages(self, thread_id, messages):
        previous = await self.aget_history(thread_id, limit=1000)
        indices = {message.id: index for index, message in enumerate(previous) if message.id}
        for message in messages:
            if message.id in indices:
                previous[indices[message.id]] = message
            else:
                previous.append(message)
                indices[message.id] = len(previous) - 1
        self.seed_thread_messages(thread_id, previous)
        return messages

    async def stream(self, request):
        binding = server.services.resident_conversation_repo.get_by_thread(request["thread_id"])
        guard = ResidentTextOnlyMiddleware("en", binding.owner_id, binding.thread_id)
        system = SystemMessage(content="Private assistant")
        prepared = guard._prepare(
            SimpleNamespace(
                system_message=system, override=lambda **updates: SimpleNamespace(**updates)
            )
        )
        names = [tool["function"]["name"] for tool in prepared.tools]
        previous = await self.aget_history(binding.thread_id, limit=1000)
        incoming = convert_to_messages(request["messages"])
        current = {"request": request, "tools": names, "previous": previous, "incoming": incoming}
        observed.append(current)
        action = actions.popleft() if actions else None
        messages = await action(guard, current) if action else []
        results.append(messages)
        answer = AIMessage(content="The host-tool turn completed.", id=_key())
        history = [*previous, *incoming]
        if messages:
            history.append(
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": message.name, "args": {}, "id": message.tool_call_id}
                        for message in messages
                    ],
                )
            )
            history.extend(messages)
        history.append(answer)
        self.seed_thread_messages(binding.thread_id, history)
        yield {"type": "token", "content": answer.content, "message_id": answer.id}
        yield {"type": "state_snapshot", "data": {"messages": history}}

    async def work(services, owner, turn, history):
        task_calls.append(turn)
        yield {"type": "token", "content": "Result from the real Task worker."}

    monkeypatch.setattr(FakeHarnessAgent, "stream", stream)
    monkeypatch.setattr(FakeHarnessAgent, "aappend_messages", append_messages, raising=False)
    monkeypatch.setattr(task_runtime, "run_turn", work)
    binding = _json(await client.put("/api/agents/resident-conversation", headers=auth, json={}))
    base = f"/api/agents/{binding['agent_id']}/threads/{binding['thread_id']}"
    return SimpleNamespace(
        client=client,
        server=server,
        auth=auth,
        binding=binding,
        base=base,
        actions=actions,
        observed=observed,
        results=results,
        task_calls=task_calls,
    )


async def _send(env, **fields):
    body = {"request_id": _key(), "prompt": "Use only my selected host capabilities.", **fields}
    accepted = _json(
        await env.client.post(f"{env.base}/messages", headers=env.auth, json=body), 202
    )
    return await _settled(env.client, env.auth, env.base, accepted["request_id"]), body


async def test_http_read_tools_default_closed_and_explicitly_enabled_for_one_turn(tools_http):
    env = tools_http
    memory = _json(
        await env.client.post(
            f"{env.base}/memory",
            headers=env.auth,
            json={"assertion": "My confirmed preference is concise writing."},
        ),
        201,
    )
    stranger = await create_user(env.client, env.auth, username="read-tool-stranger")
    other = _json(
        await env.client.put("/api/agents/resident-conversation", headers=stranger, json={})
    )
    foreign_base = f"/api/agents/{other['agent_id']}/threads/{other['thread_id']}"
    _json(
        await env.client.post(
            f"{foreign_base}/memory",
            headers=stranger,
            json={"assertion": "Another owner's private fact."},
        ),
        201,
    )

    async def default(guard, current):
        assert current["tools"] == []
        assert current["request"]["conversation_mode"] == "ask"
        return [
            await _call(guard, "resident_read_memory"),
            await _call(guard, "execute", {"command": "Forbidden"}),
        ]

    env.actions.append(default)
    first, _ = await _send(env)
    assert first["authorization"]["read_tools"] is False
    assert all(_content(result)["error_code"] == "FORBIDDEN" for result in env.results[-1])

    async def explicit(guard, current):
        assert set(current["tools"]) == {"resident_read_memory", "resident_read_tasks"}
        assert current["request"]["conversation_mode"] == "craft"
        return [
            await _call(guard, "resident_read_memory"),
            await _call(guard, "resident_create_task", {"brief": "Unauthorized work"}),
            await _call(guard, "resident_read_memory", {"owner_id": 999}),
        ]

    env.actions.append(explicit)
    second, _ = await _send(env, read_tools=True)
    assert second["authorization"]["read_tools"] is True
    read, denied, forged = env.results[-1]
    assert read.status == "success" and _content(read)["items"][0]["atom_id"] == memory["atom_id"]
    assert "Another owner's private fact" not in read.content
    assert _content(denied)["error_code"] == "FORBIDDEN"
    assert _content(forged)["error_code"] == "RESIDENT_TOOL_INPUT_INVALID"
    history = _json(await env.client.get(f"{env.base}/conversation/history", headers=env.auth))
    historical = next(
        item["turn"]
        for item in history["items"]
        if item["turn"] is not None and item["turn"]["turn_id"] == second["turn_id"]
    )
    assert historical["authorization"]["read_tools"] is True
    snapshot = _json(await env.client.get(f"{env.base}/conversation", headers=env.auth))
    current = next(item for item in snapshot["turns"] if item["turn_id"] == second["turn_id"])
    assert historical["authorization"] == current["authorization"]
    env.actions.append(default)
    await _send(env)
    assert not env.task_calls


async def test_http_task_context_freezes_options_and_one_task_returns_to_original_thread(
    tools_http,
):
    env = tools_http

    async def delegate(guard, current):
        assert current["tools"] == ["resident_create_task"]
        assert current["request"]["configurable"]["conversation_mode"] == "craft"
        return [
            await _call(guard, "resident_create_task", {"brief": "Write the authorized draft"}),
            await _call(guard, "resident_create_task", {"brief": "Write the authorized draft"}),
            await _call(guard, "resident_create_task", {"brief": "Send unrelated work"}),
        ]

    context = {
        "project_id": None,
        "message": {
            "request_id": _key(),
            "prompt": "Only write a draft. Do not send anything.",
            "options": {
                "model_ref": "openai/gpt-4o",
                "skills": [],
                "mcp_servers": [],
                "knowledge_base_ids": [],
                "memory_enabled": False,
                "team_enabled": False,
            },
        },
    }
    env.actions.append(delegate)
    turn, body = await _send(env, task_context=context)
    created, replay, conflict = env.results[-1]
    assert created.status == replay.status == "success"
    identity = _content(created)
    assert _content(replay)["task_id"] == identity["task_id"]
    assert _content(conflict)["error_code"] == "RESIDENT_REQUEST_CONFLICT"
    frozen = turn["authorization"]["task_context"]
    assert frozen["project_id"] is None
    assert frozen["message"]["prompt"] == context["message"]["prompt"]
    assert frozen["message"]["options"]["memory_enabled"] is False
    accepted = _json(
        await env.client.post(f"{env.base}/messages", headers=env.auth, json=body), 202
    )
    assert accepted["turn_id"] == turn["turn_id"]
    changed = {
        **body,
        "task_context": {
            **context,
            "message": {**context["message"], "prompt": "Broaden the original work"},
        },
    }
    assert (
        await env.client.post(f"{env.base}/messages", headers=env.auth, json=changed)
    ).status_code == 409
    async with asyncio.timeout(10):
        while True:
            linked = _json(
                await env.client.get(f"{env.base}/conversation/delegations", headers=env.auth)
            )["items"]
            if (
                linked
                and linked[0]["reports"]
                and linked[0]["reports"][0]["delivery_status"] == "delivered"
            ):
                break
            await asyncio.sleep(0.01)
    assert len(linked) == len(env.task_calls) == 1
    assert linked[0]["source_turn_id"] == turn["turn_id"]
    actual = env.task_calls[0]
    options = json.loads(actual.configuration_json)
    assert actual.model_ref == "openai/gpt-4o"
    assert "Only write a draft. Do not send anything." in actual.prompt
    assert "Write the authorized draft" in actual.prompt
    assert options["mcp_servers"] == options["skills"] == options["knowledge_base_ids"] == []
    assert options["memory_enabled"] is False and options["team_enabled"] is False
    await _send(env, prompt="Explain the completed task result.")
    assert "Result from the real Task worker." in str(env.observed[-1]["previous"])


@pytest.mark.parametrize("stop", ["cancel", "lease"])
async def test_http_cancel_or_lost_lease_refuses_further_delegation_in_old_scope(tools_http, stop):
    env = tools_http
    entered, release, refused = asyncio.Event(), asyncio.Event(), asyncio.Event()
    denials = []

    async def blocked(guard, current):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            denial = await _call(guard, "resident_create_task", {"brief": "Too late after cancel"})
            denials.append(denial)
            refused.set()
            raise
        denial = await _call(guard, "resident_create_task", {"brief": "Too late after lease loss"})
        denials.append(denial)
        refused.set()
        return [denial]

    env.actions.append(blocked)
    body = {
        "request_id": _key(),
        "prompt": "Wait for my decision.",
        "task_context": {
            "project_id": None,
            "message": {"request_id": _key(), "prompt": "Authorized draft"},
        },
    }
    turn = _json(await env.client.post(f"{env.base}/messages", headers=env.auth, json=body), 202)
    try:
        await asyncio.wait_for(entered.wait(), 10)
        if stop == "cancel":
            _json(
                await env.client.post(
                    f"{env.base}/cancel",
                    headers=env.auth,
                    json={"request_id": _key(), "turn_id": turn["turn_id"]},
                )
            )
        else:
            with env.server.services.db.transaction() as conn:
                conn.execute(
                    "UPDATE resident_requests SET lease_until=? WHERE turn_id=?",
                    (now_ms() - 1, turn["turn_id"]),
                )
            release.set()
        await asyncio.wait_for(refused.wait(), 10)
        assert _content(denials[0])["error_code"] == "FORBIDDEN"
        await _settled(
            env.client,
            env.auth,
            env.base,
            turn["request_id"],
            "cancelled" if stop == "cancel" else "interrupted",
        )
        assert not env.task_calls
        assert (
            _json(await env.client.get(f"{env.base}/conversation/delegations", headers=env.auth))[
                "items"
            ]
            == []
        )
    finally:
        release.set()


async def test_http_selected_knowledge_is_per_turn_and_revocation_does_not_break_receipt_replay(
    tools_http, monkeypatch
):
    env = tools_http
    other = await create_user(env.client, env.auth, username="knowledge-tool-owner")
    other_id = await resolve_user_id(env.client, env.auth, "knowledge-tool-owner")
    base = env.server.services.knowledge_repo.create_base(
        owner_user_id=other_id, name="Shared selected evidence", shared=True
    )
    retrieved = []

    async def retrieve(services, **arguments):
        retrieved.append(arguments)
        return "Evidence from the selected base only."

    monkeypatch.setattr("octop.infra.knowledge.retrieve.retrieve_context", retrieve)
    searched, release = asyncio.Event(), asyncio.Event()

    async def search(guard, current):
        assert current["tools"] == ["resident_search_knowledge"]
        first = await _call(guard, "resident_search_knowledge", {"query": "Selected evidence"})
        searched.set()
        await release.wait()
        denied = await _call(
            guard, "resident_search_knowledge", {"query": "After access revocation"}
        )
        return [first, denied]

    env.actions.append(search)
    body = {
        "request_id": _key(),
        "prompt": "Read my selected reference.",
        "knowledge_base_ids": [base.id],
    }
    accepted = _json(
        await env.client.post(f"{env.base}/messages", headers=env.auth, json=body), 202
    )
    try:
        await asyncio.wait_for(searched.wait(), 10)
        env.server.services.knowledge_repo.update_base(base.id, shared=False)
    finally:
        release.set()
    done = await _settled(env.client, env.auth, env.base, accepted["request_id"])
    assert done["authorization"]["knowledge_base_ids"] == [base.id]
    assert _content(env.results[-1][0])["context"] == "Evidence from the selected base only."
    assert _content(env.results[-1][1])["error_code"] == "FORBIDDEN"
    assert len(retrieved) == 1 and retrieved[0]["knowledge_base_ids"] == (base.id,)
    assert retrieved[0]["is_admin"] is False
    replay = _json(await env.client.post(f"{env.base}/messages", headers=env.auth, json=body), 202)
    assert replay["turn_id"] == done["turn_id"] and len(retrieved) == 1
    denied = await env.client.post(
        f"{env.base}/messages", headers=env.auth, json={**body, "request_id": _key()}
    )
    assert denied.status_code == 404
    await _send(env)
    assert env.observed[-1]["tools"] == []
    assert (await env.client.get(f"{env.base}/conversation", headers=other)).status_code == 404


async def test_http_capability_discovery_is_passive_authenticated_and_typed(tools_http):
    env = tools_http
    before = env.server.services.agent_repo.list_all()
    assert (await env.client.get("/api/agents/resident-capabilities")).status_code == 401
    advertised = _json(await env.client.get("/api/agents/resident-capabilities", headers=env.auth))
    assert advertised["turn_read_tools"] and advertised["selected_knowledge_search"]
    assert advertised["model_task_delegation"] == "one_per_explicit_turn_context"
    assert env.server.services.agent_repo.list_all() == before
    schema = _json(await env.client.get("/api/openapi.json", headers=env.auth))
    operation = schema["paths"]["/api/agents/resident-capabilities"]["get"]
    assert operation["summary"]
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ResidentCapabilities"
    }
