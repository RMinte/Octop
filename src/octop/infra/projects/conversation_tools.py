"""Public Task exports, independent memory maintenance and draft-only editing."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Annotated, Any, Literal

from langchain_core.messages import HumanMessage, SystemMessage
from octop_harness.context_usage import extract_usage_from_message
from octop_memory.core import Memory
from pydantic import Field, StringConstraints

from octop.infra.agents.providers.box import OctopChatModelFactory
from octop.infra.agents.providers.store import ProviderStore
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects import approval_policy, artifacts, tasks
from octop.infra.projects.expert_memory_extraction import _single_attempt_model
from octop.infra.projects.expert_profiles import resolve_expert_profile
from octop.infra.projects.models import ProjectModel, Reference
from octop.infra.usage.model_calls import model_call_scope
from octop.infra.users.resource_policy import assert_token_quota_available
from octop.infra.utils.llm_text import llm_text_content


class TaskMemoryEntry(ProjectModel):
    id: str
    content: str
    layer: Literal["atom", "raw"]
    editable: bool
    created_at: datetime | None = None
    updated_at: datetime | None = None


class TaskMemoryEntries(ProjectModel):
    task_id: str
    enabled: bool
    entries: list[TaskMemoryEntry]


class TaskMemorySave(ProjectModel):
    request_id: tasks.RequestID
    content: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32768)
    ]
    id: str | None = Field(default=None, min_length=1, max_length=256)


class PromptPolish(ProjectModel):
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32768)]
    task_id: Reference | None = None
    selected_expert_id: Reference | None = None
    model_ref: str | None = Field(default=None, max_length=256)


class PolishedPrompt(ProjectModel):
    text: str


def export_task(services: SharedServices, owner_id: int, task_id: str) -> dict[str, Any]:
    """Read every public turn under one snapshot, excluding runtime credentials."""
    with services.db.read_snapshot() as conn:
        task = services.project_task_repo.get_in_transaction(conn, task_id, owner_id)
        if task is None:
            raise OctopError.localized(ErrorCode.NOT_FOUND)
        policy = approval_policy.in_transaction(conn, services, owner_id)
        turns = services.project_task_turn_repo.list_in_transaction(conn, task_id)
        files = artifacts.for_turns(services, conn, task_id, [turn.turn_id for turn in turns])
        events = services.project_task_repo.configuration_events(conn, task_id)
        public_turns = []
        for turn in turns:
            view = tasks.turn_view(turn, files.get(turn.turn_id)).model_dump(mode="json")
            view.pop("reasoning", None)
            public_turns.append(view)
        return {
            "schema_version": 1,
            "task": tasks.summary(task, policy).model_dump(mode="json"),
            "snapshot_revision": task.list_revision,
            "configuration_events": [
                {
                    "configuration_revision": event.configuration_revision,
                    "previous_expert_id": event.previous_expert_id,
                    "selected_expert_id": event.selected_expert_id,
                    "selected_expert_name": event.selected_expert_name,
                    "previous_action_mode": event.previous_action_mode,
                    "action_mode": event.action_mode,
                    "created_at": tasks._datetime(event.created_at).isoformat(),
                }
                for event in events
            ],
            "turns": public_turns,
        }


def export_markdown(payload: dict[str, Any]) -> str:
    lines = [f"# {payload['task']['title']}", "", f"Task: {payload['task']['task_id']}", ""]
    for turn in payload["turns"]:
        lines.extend([f"## {turn['turn_id']}", "", f"{turn['status']} · {turn['created_at']}", ""])
        lines.extend(["### User", "", turn["prompt"], "", "### Assistant", "", turn["reply"], ""])
        for block in turn.get("content_blocks", []):
            if block["type"] == "tool":
                rendered = json.dumps(block, ensure_ascii=False, indent=2)
                fence = "`" * (
                    max((len(run) for run in re.findall(r"`+", rendered)), default=2) + 1
                )
                lines.extend(
                    [
                        f"### Tool: {block.get('tool_name') or ''}",
                        "",
                        fence + "json",
                        rendered,
                        fence,
                        "",
                    ]
                )
        for key in ("attachments", "legacy_attachments"):
            if turn.get(key):
                lines.extend(
                    [f"### {key}", "", json.dumps(turn[key], ensure_ascii=False, indent=2), ""]
                )
    return "\n".join(lines)


def _memory_enabled(
    services: SharedServices, owner_id: int, task_id: str, *, mutable: bool
) -> bool:
    task = services.project_task_repo.get(task_id, owner_id)
    user = services.user_repo.get(owner_id)
    if task is None or user is None or user.disabled:
        raise OctopError.localized(ErrorCode.NOT_FOUND)
    project = services.project_repo.get(task.project_id, owner_id) if task.project_id else None
    if mutable and (task.archived or (project and project.archived)):
        raise OctopError.localized(ErrorCode.TASK_ARCHIVED)
    if mutable and task.active_turn_id:
        raise OctopError.localized(ErrorCode.TASK_BUSY)
    return (
        json.loads(project.capabilities_json).get("memory_enabled", True) is not False
        if project
        else True
    )


@contextmanager
def _memory(services: SharedServices, task_id: str) -> Iterator[Memory]:
    directory = services.paths.root / "project-tasks" / task_id
    directory.mkdir(parents=True, exist_ok=True)
    memory = Memory(namespace=task_id, backend_config={"db_path": str(directory / "memory.sqlite")})
    try:
        yield memory
    finally:
        memory.backend.close()


def _atom_entry(atom: Any) -> TaskMemoryEntry:
    return TaskMemoryEntry(
        id="atom/" + atom.id,
        content=atom.assertion,
        layer="atom",
        editable=True,
        created_at=atom.created_at,
        updated_at=atom.created_at,
    )


def query_task_memory(
    services: SharedServices, owner_id: int, task_id: str, query: str = "", limit: int = 50
) -> TaskMemoryEntries:
    enabled = _memory_enabled(services, owner_id, task_id, mutable=False)
    with _memory(services, task_id) as memory:
        atoms = (
            memory.search_atoms(query, limit=limit)
            if query.strip()
            else memory.list_atoms(limit=limit)
        )
        entries = [_atom_entry(atom) for atom in atoms]
        if len(entries) < limit:
            raw = (
                memory.search_raw(query, limit=limit - len(entries))
                if query.strip()
                else memory.list_raw(limit=limit - len(entries))
            )
            entries.extend(
                TaskMemoryEntry(
                    id="raw/" + event.id,
                    content=event.content,
                    layer="raw",
                    editable=False,
                    created_at=event.timestamp,
                )
                for event in raw
            )
    return TaskMemoryEntries(task_id=task_id, enabled=enabled, entries=entries)


def _request(memory: Memory, request_id: str, fingerprint: str) -> dict[str, Any] | None:
    saved = memory.get_meta("task_management_" + hashlib.sha256(request_id.encode()).hexdigest())
    if saved is None:
        return None
    result: dict[str, Any] = json.loads(saved)
    if result["fingerprint"] != fingerprint:
        raise OctopError.localized(ErrorCode.TASK_REQUEST_CONFLICT)
    return result


def mutate_task_memory(
    services: SharedServices,
    owner_id: int,
    task_id: str,
    request_id: str,
    *,
    content: str | None = None,
    identifier: str | None = None,
) -> TaskMemoryEntry | None:
    fingerprint = hashlib.sha256(
        json.dumps([identifier, content], ensure_ascii=False).encode()
    ).hexdigest()
    # The owner lock excludes a new Task admission while this short local write
    # validates idle state. Only memory facts are changed, never checkpoints.
    with services.db.transaction() as conn:
        if not services.project_task_repo.lock_owner(conn, owner_id):
            raise OctopError.localized(ErrorCode.FORBIDDEN)
        _memory_enabled(services, owner_id, task_id, mutable=True)
        with _memory(services, task_id) as memory, memory.backend.transaction():
            saved = _request(memory, request_id, fingerprint)
            if saved is not None:
                return TaskMemoryEntry.model_validate(saved["entry"]) if saved["entry"] else None
            entry = None
            if identifier:
                if not identifier.startswith("atom/"):
                    raise OctopError.localized(ErrorCode.FORBIDDEN)
                atom = memory.get_atom(identifier.removeprefix("atom/"))
                if atom is None or atom.deprecated_at is not None:
                    raise OctopError.localized(ErrorCode.NOT_FOUND)
                if content is None:
                    memory.deprecate_atom(atom.id, actor="user")
                else:
                    try:
                        atom = memory.replace_atom(atom.id, assertion=content.strip(), actor="user")
                    except ValueError:
                        raise OctopError.localized(ErrorCode.TASK_REQUEST_CONFLICT) from None
                    assert atom is not None
                    entry = _atom_entry(atom)
            elif content is not None and content.strip():
                node = memory.store(content.strip(), conversation_id=task_id)
                atom = memory.get_atom(node.atom_id)
                assert atom is not None
                entry = _atom_entry(atom)
            else:
                raise OctopError.localized(ErrorCode.TASK_SETTINGS_INVALID, setting="content")
            memory.set_meta(
                "task_management_" + hashlib.sha256(request_id.encode()).hexdigest(),
                json.dumps(
                    {
                        "fingerprint": fingerprint,
                        "entry": entry.model_dump(mode="json") if entry else None,
                    },
                    ensure_ascii=False,
                ),
            )
            return entry


async def polish(services: SharedServices, owner_id: int, body: PromptPolish) -> PolishedPrompt:
    task = (
        await asyncio.to_thread(services.project_task_repo.get, body.task_id, owner_id)
        if body.task_id
        else None
    )
    if body.task_id and task is None:
        raise OctopError.localized(ErrorCode.NOT_FOUND)
    expert = await asyncio.to_thread(
        resolve_expert_profile,
        services,
        owner_id,
        body.selected_expert_id
        if "selected_expert_id" in body.model_fields_set
        else (task.selected_expert_id if task else None),
    )
    reference = (
        body.model_ref
        or (expert or {}).get("default_model")
        or await asyncio.to_thread(tasks.resolve_model_ref, services)
    )
    store = ProviderStore(services.provider_repo)
    if not reference or not await asyncio.to_thread(store.is_model_ref_usable, reference):
        raise OctopError.localized(ErrorCode.TASK_RESOURCE_UNAVAILABLE)
    await asyncio.to_thread(
        assert_token_quota_available, services.user_policy_repo, services.usage_repo, owner_id
    )
    factory = OctopChatModelFactory(await asyncio.to_thread(store.build_harness_configs))
    try:
        model = _single_attempt_model(
            await asyncio.to_thread(factory.get_chat_model, reference),
            store.get_model_reasoning_capability(reference),
        )
        with model_call_scope(
            services,
            user_id=owner_id,
            purpose="prompt_polish",
            task_id=body.task_id,
            project_id=task.project_id if task else None,
        ):
            result = await asyncio.wait_for(
                model.ainvoke(
                    [
                        SystemMessage(
                            "Rewrite the user's draft prompt for clarity and actionable detail. Preserve intent and language. Do not answer or execute the draft. Return only the rewritten prompt, without commentary or thinking."
                        ),
                        HumanMessage(body.text),
                    ],
                    max_tokens=2048,
                ),
                timeout=30,
            )
        if usage := extract_usage_from_message(result):
            await asyncio.to_thread(
                services.usage_repo.record,
                agent_id="",
                user_id=owner_id,
                thread_id=body.task_id or "",
                source="prompt_polish",
                model=reference,
                input_tokens=usage["input_tokens"],
                output_tokens=usage["output_tokens"],
            )
        text = llm_text_content(result).strip()
        if not text or result.tool_calls:
            raise OctopError.localized(ErrorCode.TASK_RESOURCE_UNAVAILABLE)
        return PolishedPrompt(text=text)
    except OctopError:
        raise
    except Exception:
        raise OctopError.localized(ErrorCode.TASK_RESOURCE_UNAVAILABLE) from None
    finally:
        await factory.aclose()
