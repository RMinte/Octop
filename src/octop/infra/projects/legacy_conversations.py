"""Explicitly continue an idle, owned local expert thread in a new task.

Reading the legacy source never repairs projections, moves files, changes a
checkpoint or starts an expert. Only the newly created task can execute work.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

from octop_harness.backends.workspace import BackendWorkspace

from octop.infra.backend.resolver import resolve_agent_backend_spec
from octop.infra.db.repos.project_task_legacy_sources import LegacyConversationSourceRow
from octop.infra.db.repos.project_task_turns import ProjectTaskTurnRow
from octop.infra.db.repos.project_tasks import ProjectTaskRow
from octop.infra.db.repos.threads import ThreadRow
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.gateway.hitl.store import HitlPendingStore
from octop.infra.gateway.media.backend_files import (
    file_url_to_abs_path,
    is_allowed_host_download_abs_path,
    is_host_absolute_path,
)
from octop.infra.gateway.process.message_keys import (
    CHECKPOINT_TS_KEY,
    INBOUND_ATTACHMENTS_KEY,
    STREAM_ERROR_FLAG,
    parse_checkpoint_ts_ms,
)
from octop.infra.history.projection import _role, message_inputs
from octop.infra.history.reader import export_messages
from octop.infra.history.service import HistoryArchive
from octop.infra.projects import approval_policy
from octop.infra.projects.content import TaskContentBlock
from octop.infra.projects.deletion_state import assert_project_writable
from octop.infra.projects.expert_profiles import expert_configuration, resolve_expert_profile
from octop.infra.projects.legacy_conversation_models import (
    LegacyConversationCreate,
    LegacySourceAttachment,
)
from octop.infra.projects.tasks import now_ms
from octop.infra.projects.title_policy import provisional_title
from octop.infra.utils.llm_text import llm_text_content, strip_thinking
from octop.infra.utils.ulid import new_ulid

__all__ = ["LegacyConversationCreate", "LegacySourceAttachment", "create"]


def _local_backend(spec: Any) -> bool:
    if spec is None:
        return True
    if isinstance(spec, str):
        return spec in {"filesystem", "local_shell"}
    if not isinstance(spec, dict):
        return False
    kind = spec.get("type")
    if kind in {"filesystem", "local_shell"}:
        return True
    return (
        kind == "composite"
        and _local_backend(spec.get("default"))
        and all(_local_backend(route) for route in (spec.get("routes") or {}).values())
    )


def _identity(
    services: SharedServices, owner_id: int, body: LegacyConversationCreate
) -> tuple[Any, ThreadRow]:
    agent, config = expert_configuration(services, owner_id, body.agent_id)
    try:
        spec = resolve_agent_backend_spec(config.get("backend"), repo=services.storage_backend_repo)
    except (ValueError, KeyError) as exc:
        raise OctopError.localized(ErrorCode.TASK_RESOURCE_UNAVAILABLE) from exc
    thread = services.thread_repo.get(body.thread_id)
    if thread is None or thread.agent_id != body.agent_id or thread.user_id != owner_id:
        raise OctopError.localized(ErrorCode.NOT_FOUND)
    if thread.channel_type != "dashboard" or not _local_backend(spec):
        raise OctopError.localized(ErrorCode.TASK_RESOURCE_UNAVAILABLE)
    return agent, thread


def _replay(
    services: SharedServices, owner_id: int, request_id: str, fingerprint: str
) -> str | None:
    with services.db.transaction() as conn:
        if not services.project_task_repo.lock_owner(conn, owner_id):
            raise OctopError.localized(ErrorCode.FORBIDDEN)
        receipt = services.project_task_legacy_source_repo.by_request(conn, owner_id, request_id)
        if receipt is None:
            return None
        if receipt.fingerprint != fingerprint:
            raise OctopError.localized(ErrorCode.TASK_REQUEST_CONFLICT)
        return receipt.task_id


def _data(wire: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    value = wire.get("data")
    data = value if isinstance(value, dict) else wire
    role = str(wire.get("type") or wire.get("role") or "")
    role = {"human": "user", "ai": "assistant"}.get(role, role)
    return role, data


def _text(content: Any) -> str:
    # This shared helper skips private thinking blocks and malformed think tags.
    return llm_text_content(content)


def _file_references(data: dict[str, Any]) -> list[dict[str, str]]:
    extra = data.get("additional_kwargs") or {}
    raw = extra.get(INBOUND_ATTACHMENTS_KEY) or []
    result = [dict(item) for item in raw if isinstance(item, dict)]
    content = data.get("content")
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict) or block.get("type") not in {"image_url", "image", "file"}:
            continue
        path = str(block.get("workspace_path") or "")
        url = block.get("image_url") or block.get("url")
        url = url.get("url") if isinstance(url, dict) else url
        if not path and isinstance(url, str) and url.startswith("workspace://"):
            path = url.removeprefix("workspace://")
        result.append(
            {
                "workspace_path": path,
                "filename": str(block.get("filename") or block.get("name") or "image"),
                "media_type": str(block.get("media_type") or block.get("mime_type") or "image/png"),
                **(
                    {"inline_data_url": url}
                    if isinstance(url, str) and url.startswith("data:")
                    else {}
                ),
            }
        )
    return result


@dataclass
class _VisibleTurn:
    prompt: str = ""
    replies: list[str] = field(default_factory=list)
    blocks: list[TaskContentBlock] = field(default_factory=list)
    files: list[dict[str, str]] = field(default_factory=list)
    message_ids: list[str] = field(default_factory=list)
    created_at: int = 0
    status: str = "completed"
    has_assistant: bool = False


def _visible_turns(
    wires: list[dict[str, Any]], fallback_timestamp: int, *, allow_incomplete: bool = False
) -> list[_VisibleTurn]:
    turns: list[_VisibleTurn] = []
    current: _VisibleTurn | None = None
    pending: dict[str, TaskContentBlock] = {}
    for wire in wires:
        role, data = _data(wire)
        if role == "system":
            continue
        if role not in {"user", "assistant", "tool"}:
            raise OctopError.localized(ErrorCode.TASK_RESOURCE_UNAVAILABLE)
        extra = data.get("additional_kwargs") or {}
        if role == "user":
            if current is not None:
                if not current.has_assistant or pending:
                    if not allow_incomplete:
                        raise OctopError.localized(ErrorCode.TASK_BUSY)
                    current.status = "cancelled"
                    for block in pending.values():
                        block.status = "interrupted"
                    pending.clear()
                turns.append(current)
            current = _VisibleTurn(
                prompt=_text(data.get("content")),
                created_at=parse_checkpoint_ts_ms(extra.get(CHECKPOINT_TS_KEY))
                or fallback_timestamp,
            )
        if current is None:
            current = _VisibleTurn(created_at=fallback_timestamp)
        message_id = str(data.get("id") or "")
        if message_id:
            current.message_ids.append(message_id)
        current.files.extend(_file_references(data))
        if role == "assistant":
            speaker_id = str(extra["speaker_agent_id"]) if extra.get("speaker_agent_id") else None
            speaker_name = str(extra["speaker_name"]) if extra.get("speaker_name") else None
            current.has_assistant = True
            if extra.get(STREAM_ERROR_FLAG):
                current.status = "failed"
            text = strip_thinking(_text(data.get("content")))
            if text:
                current.replies.append(text)
                current.blocks.append(
                    TaskContentBlock(
                        block_id="source",
                        type="text",
                        status="completed",
                        text=text,
                        speaker_agent_id=speaker_id,
                        speaker_name=speaker_name,
                    )
                )
            calls = list(data.get("tool_calls") or [])
            known_ids = {str(call.get("id") or "") for call in calls}
            content = data.get("content")
            for item in content if isinstance(content, list) else []:
                if (
                    isinstance(item, dict)
                    and item.get("type") == "tool_use"
                    and str(item.get("id") or "") not in known_ids
                ):
                    calls.append(
                        {
                            "id": item.get("id"),
                            "name": item.get("name"),
                            "args": item.get("input") or {},
                        }
                    )
            for call in calls:
                call_id = str(call.get("id") or "")
                if not call_id:
                    if not allow_incomplete:
                        raise OctopError.localized(ErrorCode.TASK_BUSY)
                    current.status = "cancelled"
                    current.blocks.append(
                        TaskContentBlock(
                            block_id="source",
                            type="tool",
                            status="interrupted",
                            tool_name=str(call.get("name") or ""),
                            arguments=call.get("args") or {},
                        )
                    )
                    continue
                block = TaskContentBlock(
                    block_id="source",
                    type="tool",
                    status="completed",
                    tool_call_id=call_id,
                    tool_name=str(call.get("name") or ""),
                    arguments=call.get("args") or {},
                    speaker_agent_id=speaker_id,
                    speaker_name=speaker_name,
                )
                current.blocks.append(block)
                pending[call_id] = block
        elif role == "tool":
            call_id = str(data.get("tool_call_id") or "")
            result_block = pending.pop(call_id, None)
            if result_block is None:
                result_block = TaskContentBlock(
                    block_id="source",
                    type="tool",
                    status="completed",
                    tool_call_id=call_id or None,
                    tool_name=str(data.get("name") or ""),
                    speaker_agent_id=str(extra["speaker_agent_id"])
                    if extra.get("speaker_agent_id")
                    else None,
                    speaker_name=str(extra["speaker_name"]) if extra.get("speaker_name") else None,
                )
                current.blocks.append(result_block)
            result_block.output = _text(data.get("content"))
            result_block.status = "failed" if data.get("status") == "error" else "completed"
    if current is None:
        raise OctopError.localized(ErrorCode.TASK_BUSY)
    if not current.has_assistant or pending:
        if not allow_incomplete:
            raise OctopError.localized(ErrorCode.TASK_BUSY)
        current.status = "cancelled"
        for block in pending.values():
            block.status = "interrupted"
    turns.append(current)
    return turns


async def _files(
    workspace: BackendWorkspace,
    agent_id: str,
    references: list[dict[str, str]],
    *,
    shared_viewer: bool = False,
) -> list[LegacySourceAttachment]:
    result: list[LegacySourceAttachment] = []
    seen: set[str] = set()
    for item in references:
        path = str(item.get("workspace_path") or item.get("path") or "").strip()
        if path.startswith("workspace://"):
            path = path.removeprefix("workspace://")
        if path.startswith("file://"):
            path = file_url_to_abs_path(path)
        key = path or str(item.get("filename") or item.get("name") or "")
        if key in seen:
            continue
        seen.add(key)
        source_agent = str(item.get("agent_id") or agent_id)
        allowed = (
            bool(path)
            and source_agent == agent_id
            and not any(part == ".." for part in path.replace("\\", "/").split("/"))
        )
        if allowed and is_host_absolute_path(path):
            allowed = await asyncio.to_thread(
                is_allowed_host_download_abs_path, path, workspace=Path(workspace.workspace_dir)
            )
        if allowed and shared_viewer:
            # A shared persona does not grant access to its owner's project,
            # memory or auth files. Only existing thread media remains linked.
            relative = path.replace("\\", "/")
            if is_host_absolute_path(path):
                try:
                    relative = (
                        Path(path)
                        .resolve()
                        .relative_to(Path(workspace.workspace_dir).resolve())
                        .as_posix()
                    )
                except ValueError:
                    relative = ""
            allowed = relative.startswith(("inbound/", "outbound/"))
        try:
            available = bool(
                allowed
                and await workspace.aexists(path)
                and await workspace.adownload_bytes(path) is not None
            )
        except (OSError, ValueError, PermissionError):
            available = False
        result.append(
            LegacySourceAttachment(
                name=str(
                    item.get("filename")
                    or item.get("name")
                    or path.replace("\\", "/").rsplit("/", 1)[-1]
                    or "attachment"
                ),
                media_type=str(item.get("media_type") or "application/octet-stream"),
                source_agent_id=source_agent,
                workspace_path=path or None,
                available=available,
                download_url=f"/api/agents/{quote(agent_id, safe='')}/workspace/download?path={quote(path, safe='')}"
                if available
                else None,
            )
        )
    return result


def _save(
    services: SharedServices,
    owner_id: int,
    body: LegacyConversationCreate,
    fingerprint: str,
    source_digest: str,
    visible: list[_VisibleTurn],
    attachments: list[list[LegacySourceAttachment]],
) -> str:
    with services.db.transaction() as conn:
        if not services.project_task_repo.lock_owner(conn, owner_id):
            raise OctopError.localized(ErrorCode.FORBIDDEN)
        receipt = services.project_task_legacy_source_repo.by_request(
            conn, owner_id, body.request_id
        )
        if receipt is not None:
            if receipt.fingerprint != fingerprint:
                raise OctopError.localized(ErrorCode.TASK_REQUEST_CONFLICT)
            return receipt.task_id
        agent, thread = _identity(services, owner_id, body)
        selected = (
            body.selected_expert_id
            if "selected_expert_id" in body.model_fields_set
            else body.agent_id
        )
        resolve_expert_profile(services, owner_id, selected)
        if body.project_id is not None:
            project = services.project_repo.get_in_transaction(conn, body.project_id, owner_id)
            if project is None:
                raise OctopError.localized(ErrorCode.NOT_FOUND)
            assert_project_writable(services, conn, owner_id, body.project_id)
            if project.archived:
                raise OctopError.localized(ErrorCode.PROJECT_ARCHIVED)
        policy = approval_policy.in_transaction(conn, services, owner_id)
        task_id, timestamp = "task_" + new_ulid(), now_ms()
        mode = (
            thread.conversation_mode
            if thread.conversation_mode in {"ask", "plan", "craft"}
            else "craft"
        )
        services.project_task_repo.insert(
            conn,
            ProjectTaskRow(
                task_id,
                owner_id,
                body.project_id,
                (thread.title or provisional_title(visible[0].prompt))[:128],
                "legacy_" + hashlib.sha256(f"{owner_id}:{body.request_id}".encode()).hexdigest(),
                None,
                timestamp,
                timestamp,
                status="completed",
                selected_expert_id=selected,
                action_mode=mode,
                approval_mode=policy.mode,
                approval_revision=policy.revision,
                approval_updated_at=int(policy.updated_at.timestamp() * 1000),
            ),
        )
        for source, files in zip(visible, attachments, strict=True):
            turn_id = "turn_" + new_ulid()
            for index, block in enumerate(source.blocks):
                block.block_id = f"{turn_id}:{index + 1}"
                if block.speaker_agent_id and block.speaker_name is None:
                    speaker = services.agent_repo.get(block.speaker_agent_id)
                    if speaker and (speaker.user_id == owner_id or speaker.is_shared):
                        block.speaker_name = speaker.name
            configuration = {
                "branch_origin": {
                    "source_thread_id": body.thread_id,
                    "source_agent_id": body.agent_id,
                    "source_message_ids": source.message_ids,
                    "operation": "legacy",
                    "history_only": True,
                    "source_status": source.status,
                },
                "expert_profile": {"expert_id": body.agent_id, "name": agent.name},
                "action_mode": mode,
                "legacy_attachments": [item.model_dump(mode="json") for item in files],
            }
            services.project_task_branch_repo.insert_history(
                conn,
                ProjectTaskTurnRow(
                    turn_id=turn_id,
                    task_id=task_id,
                    request_id="legacy_history_" + new_ulid(),
                    prompt=source.prompt,
                    reply="\n\n".join(source.replies),
                    status=source.status,
                    error_code="",
                    activity="",
                    configuration_json=json.dumps(configuration),
                    model_ref="",
                    worker_id="",
                    cancel_requested=False,
                    created_at=source.created_at,
                    updated_at=source.created_at,
                    content_blocks_json=json.dumps(
                        [item.model_dump(mode="json") for item in source.blocks]
                    ),
                    content_revision=1,
                ),
            )
        services.project_task_legacy_source_repo.insert(
            conn,
            LegacyConversationSourceRow(
                task_id,
                owner_id,
                body.request_id,
                fingerprint,
                body.agent_id,
                body.thread_id,
                source_digest,
                timestamp,
            ),
        )
        return task_id


async def create(
    services: SharedServices,
    owner_id: int,
    body: LegacyConversationCreate,
    *,
    registry: Any,
    history_archive: HistoryArchive | None,
    hitl_store: HitlPendingStore,
) -> str:
    fingerprint = hashlib.sha256(
        json.dumps(body.model_dump(exclude_unset=True), sort_keys=True).encode()
    ).hexdigest()
    replay = await asyncio.to_thread(_replay, services, owner_id, body.request_id, fingerprint)
    if replay is not None:
        return replay
    source_agent, thread = await asyncio.to_thread(_identity, services, owner_id, body)
    if not registry.try_begin_history_backfill(body.agent_id):
        raise OctopError.localized(ErrorCode.TASK_BUSY)
    try:
        if hitl_store.resolve_pending_for_thread(
            body.thread_id, agent_id=body.agent_id, user_id=owner_id
        ):
            raise OctopError.localized(ErrorCode.TASK_BUSY)
        harness = registry.get_agent(body.agent_id)
        state = await harness.graph.aget_state({"configurable": {"thread_id": body.thread_id}})
        if getattr(state, "next", None) or any(
            getattr(task, "interrupts", None) for task in getattr(state, "tasks", ())
        ):
            raise OctopError.localized(ErrorCode.TASK_BUSY)
        if history_archive is not None:
            if await asyncio.to_thread(history_archive.store.turn, body.thread_id, unfinished=True):
                raise OctopError.localized(ErrorCode.TASK_BUSY)
            segments = await asyncio.to_thread(history_archive.store.segments, body.thread_id)
            if any(item["agent_id"] != body.agent_id for item in segments):
                raise OctopError.localized(ErrorCode.NOT_FOUND)
            wires = await export_messages(history_archive, registry, body.agent_id, body.thread_id)
        elif (
            await asyncio.to_thread(services.thread_message_repo.projection_status, body.thread_id)
            == "ready"
        ):
            head = await asyncio.to_thread(services.thread_message_repo.head, body.thread_id)
            rows = await asyncio.to_thread(
                services.thread_message_repo.range_rows, body.thread_id, 0, head
            )
            wires = [json.loads(row.message_json) for row in rows]
        else:
            raw = list((getattr(state, "values", None) or {}).get("messages") or [])
            normalized = message_inputs(raw)
            if len(normalized) != len([message for message in raw if _role(message) != "system"]):
                raise OctopError.localized(ErrorCode.TASK_RESOURCE_UNAVAILABLE)
            wires = [json.loads(item.message_json) for item in normalized]
        visible = _visible_turns(wires, thread.created_at * 1000)
        visible[-1].files.extend(item.as_ref() for item in thread.artifacts)
        workspace = harness.workspace
        files = [
            await _files(
                workspace, body.agent_id, item.files, shared_viewer=source_agent.user_id != owner_id
            )
            for item in visible
        ]
        source_digest = hashlib.sha256(json.dumps(wires, sort_keys=True).encode()).hexdigest()
        return await asyncio.to_thread(
            _save, services, owner_id, body, fingerprint, source_digest, visible, files
        )
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise OctopError.localized(ErrorCode.TASK_RESOURCE_UNAVAILABLE) from exc
    finally:
        registry.end_history_backfill(body.agent_id)
