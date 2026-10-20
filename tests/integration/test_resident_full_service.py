"""Cross-feature acceptance for private professional instances and follow-ups."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import httpx
import pytest
from langchain_core.messages import AIMessage, convert_to_messages
from octop_memory.pipeline.extractor import CandidateExtractor, ExtractionResult
from octop_memory.types import Candidate

from octop.infra.agents.experts.catalog import ExpertCatalog
from octop.infra.agents.resident_memory import _memory
from tests.support.auth import create_user
from tests.support.fakes import FakeHarnessAgent


def _key() -> str:
    return uuid4().hex


def _base(binding: dict[str, Any]) -> str:
    return f"/api/agents/{binding['agent_id']}/threads/{binding['thread_id']}"


def _json(response: httpx.Response, status: int = 200) -> dict[str, Any]:
    assert response.status_code == status, response.text
    return response.json()


@pytest.fixture(autouse=True)
def enable_api_docs(monkeypatch):
    monkeypatch.setenv("OCTOP_ENABLE_API_DOCS", "true")


async def _send(client, auth, binding, text):
    request_id = _key()
    base = _base(binding)
    _json(
        await client.post(
            f"{base}/messages", headers=auth, json={"request_id": request_id, "prompt": text}
        ),
        202,
    )
    async with asyncio.timeout(10):
        while True:
            turn = _json(await client.get(f"{base}/requests/{request_id}", headers=auth))
            if turn["status"] == "completed":
                return turn
            assert turn["status"] in {"queued", "running"}, turn
            await asyncio.sleep(0.01)


@pytest.fixture
async def full_service(env_with_provider, monkeypatch, tmp_path):
    client, server, auth = env_with_provider
    requests = []

    async def append_messages(self, thread_id, messages):
        previous = await self.aget_history(thread_id, limit=1000)
        by_id = {message.id: index for index, message in enumerate(previous) if message.id}
        for message in messages:
            if message.id in by_id:
                previous[by_id[message.id]] = message
            else:
                previous.append(message)
                by_id[message.id] = len(previous) - 1
        self.seed_thread_messages(thread_id, previous)
        return messages

    async def stream(self, request):
        previous = await self.aget_history(request["thread_id"], limit=1000)
        incoming = convert_to_messages(request["messages"])
        requests.append((request["thread_id"], previous, incoming))
        answer = AIMessage(content=f"Answer: {incoming[-1].content}", id=_key())
        self.seed_thread_messages(request["thread_id"], [*previous, *incoming, answer])
        yield {"type": "token", "content": answer.content, "message_id": answer.id}
        yield {"type": "state_snapshot", "data": {"messages": [*previous, *incoming, answer]}}

    monkeypatch.setattr(FakeHarnessAgent, "stream", stream)
    monkeypatch.setattr(FakeHarnessAgent, "aappend_messages", append_messages, raising=False)
    template = tmp_path / "templates" / "audit-writer"
    template.mkdir(parents=True)
    (template / "manifest.json").write_text(
        json.dumps(
            {
                "label": {"en": "Writer", "zh": "写作"},
                "description": {"en": "Writing", "zh": "写作"},
                "prompt_files": ["SOUL.md"],
            }
        ),
        encoding="utf-8",
    )
    (template / "SOUL.md").write_text("Write clearly with concrete examples.", encoding="utf-8")
    catalog = ExpertCatalog(template.parent)
    catalog.refresh()
    monkeypatch.setattr(server, "expert_catalog", catalog)
    monkeypatch.setattr(server.app_runtime.agent_registry, "_expert_catalog", catalog)
    primary = _json(await client.put("/api/agents/resident-conversation", headers=auth, json={}))
    return client, server, auth, primary, requests, template, catalog


async def _profile(client, auth, name):
    body = {
        "request_id": _key(),
        "name": name,
        "template_kind": "builtin",
        "template_id": "audit-writer",
    }
    return _json(
        await client.post("/api/agents/resident-profiles", headers=auth, json=body), 201
    ), body


async def test_professional_instances_have_real_private_history_and_memory(full_service):
    client, server, auth, primary, requests, _template, _catalog = full_service
    writer, body = await _profile(client, auth, "My writer")
    coach, _ = await _profile(client, auth, "My writing coach")
    identities = {(item["agent_id"], item["thread_id"]) for item in (primary, writer, coach)}
    assert len(identities) == 3
    assert writer["tools_enabled"] is False
    replay = _json(await client.post("/api/agents/resident-profiles", headers=auth, json=body), 201)
    assert replay["profile_id"] == writer["profile_id"]
    changed = await client.post(
        "/api/agents/resident-profiles", headers=auth, json={**body, "name": "Changed"}
    )
    assert changed.status_code == 409
    first = await _send(client, auth, writer, "Writer-specific discussion")
    await _send(client, auth, coach, "Coach-specific discussion")
    await _send(client, auth, writer, "Continue my writing discussion")
    assert requests[-1][0] == writer["thread_id"]
    assert "Writer-specific discussion" in str(requests[-1][1])
    assert "Coach-specific discussion" not in str(requests[-1][1])
    memory = _json(
        await client.post(
            f"{_base(writer)}/memory",
            headers=auth,
            json={"assertion": "My writing uses short sentences.", "kind": "Preference"},
        ),
        201,
    )
    assert (await client.get(f"{_base(coach)}/memory", headers=auth)).json()["items"] == []
    assert (await client.get(f"{_base(primary)}/memory", headers=auth)).json()["items"] == []
    writer_history = _json(await client.get(f"{_base(writer)}/conversation/history", headers=auth))
    assert any(
        item.get("turn", {}).get("turn_id") == first["turn_id"]
        for item in writer_history["items"]
        if item.get("turn")
    )
    assert memory["atom_id"]
    stranger = await create_user(client, auth, username="full-service-stranger")
    assert (
        await client.get(f"/api/agents/resident-profiles/{writer['profile_id']}", headers=stranger)
    ).status_code == 404
    assert (await client.get(f"{_base(writer)}/memory", headers=stranger)).status_code == 404
    assert (await client.get(f"{_base(writer)}/conversation", headers=stranger)).status_code == 404
    current_primary = _json(await client.get("/api/agents/resident-conversation", headers=auth))
    assert current_primary["thread_id"] == primary["thread_id"]
    assert not server.services.agent_repo.get(writer["agent_id"]).is_shared


async def test_template_diff_and_adoption_never_replace_private_history_or_memory(full_service):
    client, server, auth, _primary, _requests, template, catalog = full_service
    profile, _ = await _profile(client, auth, "Writer with private facts")
    source = await _send(client, auth, profile, "Keep this private discussion.")
    memory = _json(
        await client.post(
            f"{_base(profile)}/memory", headers=auth, json={"assertion": "I write for engineers."}
        ),
        201,
    )
    original = server.services.agent_repo.get(profile["agent_id"])
    original_config = original.config_json
    (template / "SOUL.md").write_text(
        "Write with clear evidence and short paragraphs.", encoding="utf-8"
    )
    catalog.refresh()
    url = f"/api/agents/resident-profiles/{profile['profile_id']}"
    diff = _json(await client.get(f"{url}/template-diff", headers=auth))
    assert diff["has_changes"] and diff["current_version"] != diff["available_version"]
    assert diff["changes"]
    assert server.services.agent_repo.get(profile["agent_id"]).config_json == original_config
    assert (
        server.services.agent_repo.get(profile["agent_id"]).system_prompt == original.system_prompt
    )
    stale = await client.post(
        f"{url}/adopt-template",
        headers=auth,
        json={
            "expected_revision": profile["revision"] + 1,
            "template_version": diff["available_version"],
        },
    )
    assert stale.status_code == 409
    adopted = _json(
        await client.post(
            f"{url}/adopt-template",
            headers=auth,
            json={
                "expected_revision": profile["revision"],
                "template_version": diff["available_version"],
            },
        )
    )
    assert adopted["revision"] > profile["revision"]
    assert (adopted["agent_id"], adopted["thread_id"]) == (
        profile["agent_id"],
        profile["thread_id"],
    )
    assert adopted["template_version"] == diff["available_version"]
    assert _json(await client.get(f"{url}/template-diff", headers=auth))["has_changes"] is False
    history = _json(await client.get(f"{_base(profile)}/conversation", headers=auth))
    assert history["turns"][0]["turn_id"] == source["turn_id"]
    assert (
        _json(await client.get(f"{_base(profile)}/memory", headers=auth))["items"][0]["atom_id"]
        == memory["atom_id"]
    )


async def test_memory_policy_requires_explicit_choice_and_evidence_is_profile_scoped(full_service):
    client, _server, auth, primary, _requests, _template, _catalog = full_service
    writer, _ = await _profile(client, auth, "Memory writer")
    source = await _send(client, auth, writer, "My text should be concise.")
    url = f"{_base(writer)}/memory"
    policy = _json(await client.get(f"{url}/policy", headers=auth))
    assert policy == {"capture_enabled": False, "revision": 1, "mode": "disabled"}
    enabled = _json(
        await client.put(
            f"{url}/policy",
            headers=auth,
            json={"capture_enabled": True, "expected_revision": policy["revision"]},
        )
    )
    assert enabled["capture_enabled"] is True and enabled["mode"] == "raw_capture_only"
    stale = await client.put(
        f"{url}/policy",
        headers=auth,
        json={"capture_enabled": False, "expected_revision": policy["revision"]},
    )
    assert stale.status_code == 409
    assert (
        _json(await client.get(f"{_base(primary)}/memory/policy", headers=auth))["capture_enabled"]
        is False
    )
    memory = _json(
        await client.post(
            url,
            headers=auth,
            json={
                "assertion": "I prefer concise text.",
                "kind": "Preference",
                "source_turn_id": source["turn_id"],
            },
        ),
        201,
    )
    evidence = _json(await client.get(f"{url}/{memory['atom_id']}/sources", headers=auth))
    assert evidence["items"][0]["source_turn_id"] == source["turn_id"]
    assert evidence["items"][0]["history_item_id"] == f"turn:{source['turn_id']}"
    invalid = await client.post(
        f"{_base(primary)}/memory",
        headers=auth,
        json={"assertion": "Copy from other profile", "source_turn_id": source["turn_id"]},
    )
    assert invalid.status_code == 404
    assert (
        await client.get(f"{_base(primary)}/memory/{memory['atom_id']}/sources", headers=auth)
    ).status_code == 404
    candidates = _json(await client.get(f"{url}/candidates", headers=auth))
    assert candidates["items"][0]["status"] == "promoted"
    assert (await client.get(f"{_base(primary)}/memory/candidates", headers=auth)).json()[
        "items"
    ] == []


def _note_body(text="Send the authorized reminder."):
    return {
        "request_id": _key(),
        "purpose": "Follow the user's reminder",
        "kind": "note",
        "schedule": "date:" + (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        "payload": {"text": text},
    }


async def test_followup_requires_explicit_authorization_and_cancelled_versions_stay_inactive(
    full_service,
):
    from octop.infra.agents.resident_followups import poll_followups

    client, server, auth, primary, _requests, _template, _catalog = full_service
    url = f"{_base(primary)}/conversation/followups"
    assert _json(await client.get(url, headers=auth))["items"] == []
    body = _note_body("Original text must not be sent after an edit.")
    first = _json(await client.post(url, headers=auth, json=body), 201)
    replay = _json(await client.post(url, headers=auth, json=body), 201)
    assert replay["followup_id"] == first["followup_id"]
    assert first["status"] == "enabled" and first["capabilities"] == ["conversation_notify"]
    assert first["timezone"] == server.config.default_timezone
    changed = {key: value for key, value in body.items() if key != "request_id"}
    changed.update(
        payload={"text": "New authorized text."},
        status="enabled",
        expected_revision=first["revision"],
    )
    updated = _json(await client.put(f"{url}/{first['followup_id']}", headers=auth, json=changed))
    assert updated["authorization_version"] > first["authorization_version"]
    cancelled = _json(
        await client.post(
            f"{url}/{first['followup_id']}/cancel",
            headers=auth,
            json={"expected_revision": updated["revision"]},
        )
    )
    assert cancelled["status"] == "cancelled" and cancelled["next_fire_at"] is None
    repeated = _json(
        await client.post(
            f"{url}/{first['followup_id']}/cancel",
            headers=auth,
            json={"expected_revision": updated["revision"]},
        )
    )
    assert repeated["revision"] == cancelled["revision"]
    due = int((datetime.now(UTC) + timedelta(hours=1)).timestamp() * 1000)
    assert await asyncio.to_thread(poll_followups, server.services, due) == 0
    resurrection = await client.put(
        f"{url}/{first['followup_id']}",
        headers=auth,
        json={**changed, "expected_revision": cancelled["revision"]},
    )
    assert resurrection.status_code == 409
    stranger = await create_user(client, auth, username="followup-stranger")
    assert (await client.get(url, headers=stranger)).status_code == 404
    assert (
        await client.post(
            f"{url}/{first['followup_id']}/cancel",
            headers=stranger,
            json={"expected_revision": cancelled["revision"]},
        )
    ).status_code == 404


async def test_read_watermark_is_monotonic_and_keeps_messages_arriving_concurrently(full_service):
    client, _server, auth, primary, _requests, _template, _catalog = full_service
    base = _base(primary)
    first = await _send(client, auth, primary, "First unread answer")
    inbox = _json(await client.get(f"{base}/conversation/inbox", headers=auth))
    assert inbox["unread_count"] == 1
    assert inbox["last_message_id"] == first["assistant_message_id"]
    second, read = await asyncio.gather(
        _send(client, auth, primary, "New answer arriving as another device reads"),
        client.post(
            f"{base}/conversation/read",
            headers=auth,
            json={"message_id": first["assistant_message_id"]},
        ),
    )
    _json(read)
    after = _json(await client.get(f"{base}/conversation/inbox", headers=auth))
    assert after["last_read_message_id"] == first["assistant_message_id"]
    assert after["last_message_id"] == second["assistant_message_id"]
    assert after["unread_count"] == 1
    _json(
        await client.post(
            f"{base}/conversation/read",
            headers=auth,
            json={"message_id": second["assistant_message_id"]},
        )
    )
    old_device = _json(
        await client.post(
            f"{base}/conversation/read",
            headers=auth,
            json={"message_id": first["assistant_message_id"]},
        )
    )
    assert old_device["last_read_message_id"] == second["assistant_message_id"]
    assert old_device["unread_count"] == 0
    writer, _ = await _profile(client, auth, "Other inbox")
    assert (
        await client.post(
            f"{_base(writer)}/conversation/read",
            headers=auth,
            json={"message_id": second["assistant_message_id"]},
        )
    ).status_code == 409
    assert (
        await client.post(
            f"{base}/conversation/read",
            headers=auth,
            json={"message_id": "invented-future-message"},
        )
    ).status_code == 409


async def test_task_watch_is_quiet_until_change_and_stops_after_one_terminal_notice(
    full_service, monkeypatch
):
    from octop.infra.agents.resident_followups import poll_followups
    from octop.infra.projects import runtime as task_runtime

    client, server, auth, primary, _requests, _template, _catalog = full_service
    runtime = server.app_runtime.resident_conversations
    await runtime.close()
    entered, release = asyncio.Event(), asyncio.Event()

    async def work(services, owner, turn, history):
        entered.set()
        await release.wait()
        yield {"type": "token", "content": "The watched task has finished."}

    monkeypatch.setattr(task_runtime, "run_turn", work)
    created = _json(
        await client.post(
            "/api/project-tasks",
            headers=auth,
            json={"request_id": _key(), "prompt": "Watch my work."},
        ),
        202,
    )
    try:
        await asyncio.wait_for(entered.wait(), 10)
        base = _base(primary)
        url = f"{base}/conversation/followups"
        watch = _json(
            await client.post(
                url,
                headers=auth,
                json={
                    "request_id": _key(),
                    "purpose": "Follow this exact task",
                    "kind": "task_watch",
                    "subject_task_id": created["task_id"],
                },
            ),
            201,
        )
        now = int(datetime.now(UTC).timestamp() * 1000)
        assert await asyncio.to_thread(poll_followups, server.services, now + 1000) == 0
        assert await asyncio.to_thread(poll_followups, server.services, now + 3000) == 0
        with server.services.db.connect() as conn:
            assert (
                conn.execute("SELECT COUNT(*) AS n FROM resident_notifications").fetchone()["n"]
                == 0
            )
        release.set()
        async with asyncio.timeout(10):
            while True:
                task = _json(
                    await client.get(f"/api/project-tasks/{created['task_id']}", headers=auth)
                )
                if task["status"] == "completed":
                    break
                await asyncio.sleep(0.01)
        assert await asyncio.to_thread(poll_followups, server.services, now + 5000) == 1
        assert await asyncio.to_thread(poll_followups, server.services, now + 7000) == 0
        stopped = _json(await client.get(f"{url}/{watch['followup_id']}", headers=auth))
        assert stopped["status"] == "completed" and stopped["next_fire_at"] is None
        # Startup after capture must pick up the durable outbox, without a second observation.
        await runtime.start()
        async with asyncio.timeout(10):
            while True:
                notices = _json(
                    await client.get(f"{base}/conversation/notifications", headers=auth)
                )["items"]
                if notices and notices[0]["status"] == "delivered":
                    break
                await asyncio.sleep(0.01)
        assert len(notices) == 1
        message_id = notices[0]["message_id"]
        await runtime.close()
        await runtime.start()
        assert await asyncio.to_thread(poll_followups, server.services, now + 10000) == 0
        current = _json(await client.get(f"{base}/conversation/notifications", headers=auth))[
            "items"
        ]
        assert [item["message_id"] for item in current] == [message_id]
        history = _json(await client.get(f"{base}/conversation/history", headers=auth))
        assert sum(item["kind"] == "notification" for item in history["items"]) == 1
        await _send(client, auth, primary, "What happened to the watched task?")
        assert "The watched task has finished." in str(_requests[-1][1])
    finally:
        release.set()
        await runtime.start()


@pytest.mark.parametrize("action", ["update", "cancel"])
async def test_followup_edits_and_cancellation_discard_old_pending_delivery(full_service, action):
    from octop.infra.agents.resident_followups import poll_followups

    client, server, auth, primary, _requests, _template, _catalog = full_service
    runtime = server.app_runtime.resident_conversations
    await runtime.close()
    base = _base(primary)
    url = f"{base}/conversation/followups"
    body = {**_note_body("This old version must not be delivered."), "schedule": "interval:60"}
    note = _json(await client.post(url, headers=auth, json=body), 201)
    due = int((datetime.now(UTC) + timedelta(seconds=61)).timestamp() * 1000)
    assert await asyncio.to_thread(poll_followups, server.services, due) == 1
    assert await asyncio.to_thread(poll_followups, server.services, due) == 0
    if action == "cancel":
        _json(
            await client.post(
                f"{url}/{note['followup_id']}/cancel",
                headers=auth,
                json={"expected_revision": note["revision"]},
            )
        )
    else:
        replacement = {key: value for key, value in body.items() if key != "request_id"}
        replacement.update(
            payload={"text": "A new authorized version for the future."},
            status="enabled",
            expected_revision=note["revision"],
        )
        _json(await client.put(f"{url}/{note['followup_id']}", headers=auth, json=replacement))
    await runtime.start()
    async with asyncio.timeout(10):
        while True:
            with server.services.db.connect() as conn:
                pending = conn.execute(
                    "SELECT status,archive_written_at FROM resident_notifications WHERE followup_id=?",
                    (note["followup_id"],),
                ).fetchone()
            if pending["status"] == "discarded":
                break
            await asyncio.sleep(0.01)
    assert pending["archive_written_at"] is None
    assert _json(await client.get(f"{base}/conversation/inbox", headers=auth))["unread_count"] == 0
    history = _json(await client.get(f"{base}/conversation/history", headers=auth))
    assert all(item["kind"] != "notification" for item in history["items"])


async def test_archiving_a_professional_preserves_reads_and_stops_new_private_work(full_service):
    client, _server, auth, _primary, _requests, _template, _catalog = full_service
    profile, _ = await _profile(client, auth, "Archiveable writer")
    base = _base(profile)
    source = await _send(client, auth, profile, "Keep my private history")
    memory = _json(
        await client.post(
            f"{base}/memory", headers=auth, json={"assertion": "Keep my private memory."}
        ),
        201,
    )
    note = _json(
        await client.post(f"{base}/conversation/followups", headers=auth, json=_note_body()), 201
    )
    url = f"/api/agents/resident-profiles/{profile['profile_id']}/archive"
    archived = _json(
        await client.patch(
            url, headers=auth, json={"archived": True, "expected_revision": profile["revision"]}
        )
    )
    assert archived["archived"] is True
    history = _json(await client.get(f"{base}/conversation", headers=auth))
    assert history["turns"][0]["turn_id"] == source["turn_id"]
    assert (
        _json(await client.get(f"{base}/memory", headers=auth))["items"][0]["atom_id"]
        == memory["atom_id"]
    )
    assert (
        await client.post(
            f"{base}/messages",
            headers=auth,
            json={"request_id": _key(), "prompt": "Should stay archived"},
        )
    ).status_code == 409
    assert (
        await client.post(
            f"{base}/memory", headers=auth, json={"assertion": "Cannot write while archived"}
        )
    ).status_code == 409
    stopped = _json(
        await client.get(f"{base}/conversation/followups/{note['followup_id']}", headers=auth)
    )
    assert stopped["status"] == "cancelled"
    restored = _json(
        await client.patch(
            url, headers=auth, json={"archived": False, "expected_revision": archived["revision"]}
        )
    )
    assert restored["archived"] is False
    assert (
        _json(
            await client.get(f"{base}/conversation/followups/{note['followup_id']}", headers=auth)
        )["status"]
        == "cancelled"
    )
    await _send(client, auth, profile, "Continue after restoring")


async def test_explicit_candidate_extraction_is_incremental_and_requires_user_approval(
    full_service, monkeypatch
):
    client, server, auth, primary, _requests, _template, _catalog = full_service
    base = _base(primary)
    owner = server.services.agent_repo.get(primary["agent_id"]).user_id
    args = (server.services, owner, primary["agent_id"], primary["thread_id"])
    with _memory(*args) as memory:
        raw = memory.add_raw(
            "I prefer short, precise examples.",
            host="octop-harness",
            event_type="user_message",
            thread_id=primary["thread_id"],
            session_id=primary["thread_id"],
        )
        memory.add_raw(
            "Assistant speculation should not become a fact.",
            host="octop-harness",
            event_type="assistant_message",
            thread_id=primary["thread_id"],
            session_id=primary["thread_id"],
        )
    calls = []

    def extract(self, events, *, session_id):
        calls.append(events)
        assert session_id == primary["thread_id"]
        assert [event.id for event in events] == [raw.id]
        return ExtractionResult(
            candidates=[
                Candidate(
                    id=_key(),
                    raw_event_ids=[raw.id],
                    candidate_type="Preference",
                    status="pending",
                    title="Concise examples",
                    assertion=raw.content,
                    verbatim_quote=raw.content,
                    quote_event_id=raw.id,
                    subject_name="admin",
                    subject_entity_type="User",
                    target_entity_id=None,
                    confidence="high",
                    importance="medium",
                    recommended_action="promote",
                    promotion_reason="",
                    extractor_version="test/v1",
                    created_at=datetime.now(UTC),
                    session_id=session_id,
                )
            ]
        )

    monkeypatch.setattr(FakeHarnessAgent, "model_factory", SimpleNamespace(), raising=False)
    monkeypatch.setattr(CandidateExtractor, "extract", extract)
    outcome = _json(await client.post(f"{base}/memory/candidates:extract", headers=auth))
    assert outcome["status"] == "completed" and outcome["candidates"] == 1
    assert outcome["events_extracted"] == 1 and len(calls) == 1
    assert _json(await client.get(f"{base}/memory", headers=auth))["items"] == []
    candidate = _json(await client.get(f"{base}/memory/candidates?status=pending", headers=auth))[
        "items"
    ][0]
    evidence = _json(
        await client.get(
            f"{base}/memory/candidates/{candidate['candidate_id']}/sources", headers=auth
        )
    )
    assert evidence["items"][0]["event_id"] == raw.id
    runtime = server.app_runtime.resident_conversations
    await runtime.close()
    await runtime.start()
    repeated = _json(await client.post(f"{base}/memory/candidates:extract", headers=auth))
    assert repeated["status"] == "empty" and len(calls) == 1
    approved = _json(
        await client.post(
            f"{base}/memory/candidates/{candidate['candidate_id']}:approve", headers=auth
        )
    )
    assert approved["status"] == "promoted" and approved["decided_by"] == "user"
    assert (
        _json(await client.get(f"{base}/memory", headers=auth))["items"][0]["assertion"]
        == raw.content
    )
    with _memory(*args) as memory:
        memory.add_raw(
            "Another explicit user observation.",
            host="octop-harness",
            event_type="user_message",
            thread_id=primary["thread_id"],
            session_id=primary["thread_id"],
        )
    monkeypatch.setattr(
        CandidateExtractor,
        "extract",
        lambda *_args, **_kwargs: ExtractionResult(
            candidates=[], failure_reason="Private provider detail must stay hidden."
        ),
    )
    failed = _json(await client.post(f"{base}/memory/candidates:extract", headers=auth))
    assert (
        failed["status"] == "failed" and failed["error_code"] == "RESIDENT_MEMORY_EXTRACTION_FAILED"
    )
    assert failed["pending_events"] == 1
    assert "Private provider detail" not in str(failed)


async def test_full_service_routes_are_documented_typed_and_owner_scoped(full_service):
    client, _server, auth, primary, _requests, _template, _catalog = full_service
    docs = await client.get("/api/docs", headers=auth)
    assert docs.status_code == 200 and "scalar" in docs.text.lower()
    schema = _json(await client.get("/api/openapi.json", headers=auth))
    template_base = "/api/agents/{agent_id}/threads/{thread_id}"
    operations = [
        ("/api/agents/resident-profiles", "post", "201"),
        ("/api/agents/resident-profiles/{profile_id}/template-diff", "get", "200"),
        (f"{template_base}/memory/policy", "put", "200"),
        (f"{template_base}/memory/candidates:extract", "post", "200"),
        (f"{template_base}/conversation/followups", "post", "201"),
        (f"{template_base}/conversation/notifications", "get", "200"),
        (f"{template_base}/conversation/inbox", "get", "200"),
        (f"{template_base}/conversation/read", "post", "200"),
        (f"{template_base}/conversation/context", "get", "200"),
        (f"{template_base}/conversation/context:compact", "post", "200"),
    ]
    for path, method, status in operations:
        operation = schema["paths"][path][method]
        assert operation["summary"]
        assert "$ref" in operation["responses"][status]["content"]["application/json"]["schema"]
    base = _base(primary)
    stranger = await create_user(client, auth, username="docs-stranger")
    for suffix in (
        "memory/policy",
        "memory/candidates",
        "conversation/context",
        "conversation/inbox",
        "conversation/notifications",
        "conversation/followups",
    ):
        assert (await client.get(f"{base}/{suffix}")).status_code == 401
        assert (await client.get(f"{base}/{suffix}", headers=stranger)).status_code == 404
    for suffix in ("memory/candidates:extract", "conversation/context:compact"):
        assert (await client.post(f"{base}/{suffix}")).status_code == 401
        assert (await client.post(f"{base}/{suffix}", headers=stranger)).status_code == 404


@pytest.mark.parametrize("stop", ["cancel", "archive"])
async def test_scheduled_task_uses_normal_admission_and_survives_later_followup_stop(
    full_service, monkeypatch, stop
):
    from octop.infra.agents.resident_followups import poll_followups
    from octop.infra.projects import runtime as task_runtime

    client, server, auth, _primary, _requests, _template, _catalog = full_service
    profile, _ = await _profile(client, auth, "Scheduled professional")
    base = _base(profile)
    source = await _send(
        client, auth, profile, "Create a regular task for this explicitly authorized work."
    )
    runtime = server.app_runtime.resident_conversations
    await runtime.close()
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def work(services, owner, turn, history):
        calls.append(turn)
        entered.set()
        await release.wait()
        yield {"type": "token", "content": "Scheduled task result."}

    monkeypatch.setattr(task_runtime, "run_turn", work)
    body = {
        "request_id": _key(),
        "purpose": "Produce my authorized periodic result",
        "kind": "task",
        "schedule": "interval:60",
        "capabilities": ["task_create", "conversation_notify"],
        "payload": {
            "source_turn_id": source["turn_id"],
            "project_id": None,
            "message": {"request_id": _key(), "prompt": "Produce the scheduled result."},
        },
    }
    url = f"{base}/conversation/followups"
    denied = await client.post(
        url, headers=auth, json={**body, "capabilities": ["conversation_notify"]}
    )
    assert denied.status_code == 422
    followup = _json(await client.post(url, headers=auth, json=body), 201)
    due = int((datetime.now(UTC) + timedelta(seconds=61)).timestamp() * 1000)
    try:
        assert await asyncio.to_thread(poll_followups, server.services, due) == 1
        assert await asyncio.to_thread(poll_followups, server.services, due) == 0
        await asyncio.wait_for(entered.wait(), 10)
        observed = _json(await client.get(f"{url}/{followup['followup_id']}", headers=auth))
        assert observed["last_outcome"] == "admitted" and observed["last_task_id"]
        task_id = observed["last_task_id"]
        linked = _json(await client.get(f"{base}/conversation/delegations", headers=auth))["items"]
        assert len(linked) == 1 and linked[0]["task_id"] == task_id
        assert linked[0]["source_turn_id"] == source["turn_id"]
        assert len(calls) == 1
        if stop == "cancel":
            _json(
                await client.post(
                    f"{url}/{followup['followup_id']}/cancel",
                    headers=auth,
                    json={"expected_revision": observed["revision"]},
                )
            )
        else:
            _json(
                await client.patch(
                    f"/api/agents/resident-profiles/{profile['profile_id']}/archive",
                    headers=auth,
                    json={"archived": True, "expected_revision": profile["revision"]},
                )
            )
        assert await asyncio.to_thread(poll_followups, server.services, due + 60000) == 0
        active = _json(await client.get(f"/api/project-tasks/{task_id}", headers=auth))
        assert active["status"] in {"queued", "running"}
        release.set()
        async with asyncio.timeout(10):
            while True:
                done = _json(await client.get(f"/api/project-tasks/{task_id}", headers=auth))
                if done["status"] == "completed":
                    break
                await asyncio.sleep(0.01)
        assert done["project_id"] is None and done["turns"][0]["reply"] == "Scheduled task result."
        await runtime.start()
        async with asyncio.timeout(10):
            while True:
                detail = _json(
                    await client.get(
                        f"{base}/conversation/delegations/{linked[0]['delegation_id']}",
                        headers=auth,
                    )
                )
                if detail["reports"] and detail["reports"][0]["delivery_status"] == "delivered":
                    break
                await asyncio.sleep(0.01)
        assert len(detail["reports"]) == 1
        history = _json(await client.get(f"{base}/conversation/history", headers=auth))
        assert sum(item["kind"] == "task_report" for item in history["items"]) == 1
        assert (
            len(_json(await client.get(f"{base}/conversation/delegations", headers=auth))["items"])
            == 1
        )
    finally:
        release.set()
        await runtime.start()
