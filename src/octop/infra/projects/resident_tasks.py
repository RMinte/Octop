"""Private resident conversations executed by the canonical Task worker.

Resident request/delegation rows remain authorization and delivery receipts. They
never choose an ordinary expert, and their internal Task is hidden by TaskRepo.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import sqlite3
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any, cast

import aiosqlite
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    message_to_dict,
    messages_from_dict,
)
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from octop_harness.request import ChatRequest

from octop.i18n import tr
from octop.infra.agents.memory.backend import open_memory_kwargs
from octop.infra.agents.providers.box import OctopHarnessAgent as HarnessAgent
from octop.infra.agents.providers.store import ProviderStore
from octop.infra.agents.resident_identity import (
    resident_execution_scope,
    resident_harness_config,
    resident_memory_scope,
    resident_prompt_scope,
)
from octop.infra.agents.resident_tools import resident_tools_scope
from octop.infra.db.repos.project_task_turns import ProjectTaskTurnRow
from octop.infra.db.repos.project_tasks import ProjectTaskRow
from octop.infra.db.repos.resident_conversations import ResidentConversationRow
from octop.infra.db.repos.resident_requests import ResidentRequestRow
from octop.infra.db.repos.unified_sources import UnifiedSourceRouteRepo
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.gateway.media.attachment_hints import content_blocks_need_vision
from octop.infra.gateway.process.usage_record import UsageTracker
from octop.infra.projects import input_attachments, source_routes
from octop.infra.projects.checkpoint_saver import TaskCheckpointSaver
from octop.infra.projects.context import capture_memory, close_pending_tools, model_context
from octop.infra.projects.image_middleware import TaskImageMaterializeMiddleware
from octop.infra.projects.input_attachment_models import TaskInputAttachment
from octop.infra.projects.settings import snapshot_presentation
from octop.infra.projects.stream import TASK_PROTOCOL
from octop.infra.users.resource_policy import assert_token_quota_available
from octop.infra.utils.locale import normalize_locale


def _require(services: SharedServices, owner: int, thread_id: str) -> ResidentConversationRow:
    binding = services.resident_conversation_repo.get_by_thread(thread_id)
    user = services.user_repo.get(owner)
    agent = services.agent_repo.get(binding.agent_id) if binding else None
    if (
        binding is None
        or binding.owner_id != owner
        or binding.status != "ready"
        or user is None
        or user.disabled
        or agent is None
        or agent.user_id != owner
        or agent.is_shared
        or not agent.enabled
    ):
        raise OctopError.localized(ErrorCode.NOT_FOUND)
    return binding


def task_id(owner: int, thread_id: str) -> str:
    return "resident_task_" + hashlib.sha256(f"{owner}:{thread_id}".encode()).hexdigest()


def _turn_messages(services: SharedServices, turn: ProjectTaskTurnRow) -> list[BaseMessage] | None:
    """Read a completed native transcript, including host-tool receipts, without setup writes."""
    if turn.status != "completed":
        return None
    path = (
        services.paths.root / "project-tasks" / turn.task_id / turn.turn_id / "checkpoints.sqlite"
    )
    if not path.is_file():
        return None
    from langgraph.checkpoint.sqlite import SqliteSaver

    from octop.infra.history.migration_source import SourceUnavailable, _checkpoint_values

    with contextlib.closing(
        sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    ) as conn:
        conn.execute("PRAGMA query_only=ON")
        if (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='checkpoints'"
            ).fetchone()
            is None
        ):
            return None
        saver = SqliteSaver(conn)
        saver.is_setup = True
        try:
            wires, _ = _checkpoint_values(saver, turn.turn_id, None)
        except SourceUnavailable:
            return None
    messages = messages_from_dict(wires)
    index = next(
        (
            index
            for index, message in enumerate(messages)
            if message.id == f"resident:{turn.turn_id}:user"
        ),
        None,
    )
    return messages[index:] if index is not None else None


def history_messages(
    services: SharedServices, owner: int, thread_id: str
) -> list[BaseMessage] | None:
    """Read the private timeline without opening an executable legacy graph."""
    _require(services, owner, thread_id)
    user = services.user_repo.get(owner)
    locale = normalize_locale(user.locale if user else "en")
    identifier = task_id(owner, thread_id)
    if services.project_task_repo.get_internal(identifier, owner) is None:
        return None
    messages: list[BaseMessage] = []
    for turn in services.project_task_turn_repo.list(identifier):
        raw = json.loads(turn.configuration_json)
        kind = raw.get("execution_kind")
        if kind == "resident_history":
            messages.extend(messages_from_dict(raw.get("messages", [])))
        elif kind == "resident_notification":
            messages.extend(messages_from_dict([raw["resident_message"]]))
        elif kind == "resident":
            transcript = _turn_messages(services, turn)
            if transcript:
                messages.extend(transcript)
                continue
            messages.append(
                HumanMessage(
                    content=_user_content(turn, locale),
                    id=f"resident:{turn.turn_id}:user",
                    additional_kwargs={
                        "resident_request_id": turn.request_id,
                        "checkpoint_ts": turn.created_at,
                    },
                )
            )
            if turn.reply or turn.status in {"completed", "failed", "cancelled"}:
                messages.append(
                    AIMessage(
                        content=turn.reply,
                        id=f"resident:{turn.turn_id}:assistant",
                        additional_kwargs={
                            "checkpoint_ts": turn.updated_at,
                            **(
                                {"error_code": turn.error_code, "octop_stream_error": True}
                                if turn.error_code
                                else {}
                            ),
                        },
                    )
                )
    return messages


def _user_content(turn: ProjectTaskTurnRow, locale: str = "en") -> str | list[str | dict[Any, Any]]:
    raw = json.loads(turn.configuration_json)
    identifiers = raw.get("resident_input", {}).get("attachment_ids", [])
    if not identifiers:
        return turn.prompt
    metadata = {
        item.attachment_id: item
        for item in (
            TaskInputAttachment.model_validate(item)
            for item in raw.get("resident_configuration", {}).get("attachments", [])
        )
    }
    blocks: list[str | dict[Any, Any]] = []
    if turn.prompt:
        blocks.append({"type": "text", "text": turn.prompt})
    for identifier in identifiers:
        attachment = metadata.get(identifier)
        if attachment is None:
            raise OctopError.localized(ErrorCode.TASK_ATTACHMENT_UNAVAILABLE)
        if input_attachments.is_image(attachment.media_type):
            blocks.extend(
                cast(
                    list[str | dict[Any, Any]],
                    input_attachments.content("", [identifier], [attachment]),
                )
            )
        else:
            blocks.append(
                {
                    "type": "text",
                    "text": tr(
                        "agents.resident.attachment_hint",
                        locale,
                        name=attachment.name,
                        attachment_id=identifier,
                    ),
                }
            )
    return blocks


def history_active(services: SharedServices, owner: int, thread_id: str) -> bool:
    _require(services, owner, thread_id)
    task = services.project_task_repo.get_internal(task_id(owner, thread_id), owner)
    with services.db.connect() as conn:
        return bool(
            (task and task.active_turn_id)
            or services.resident_request_repo.active_in_transaction(conn, owner, thread_id)
        )


async def ensure_task(
    services: SharedServices, owner: int, thread_id: str, first_request: str
) -> str:
    binding = await asyncio.to_thread(_require, services, owner, thread_id)
    identifier = task_id(owner, thread_id)
    if await asyncio.to_thread(services.project_task_repo.get_internal, identifier, owner):
        return identifier
    fence = source_routes.writer_fence(services, thread_id)
    await asyncio.to_thread(fence.__enter__)
    try:
        if await asyncio.to_thread(services.project_task_repo.get_internal, identifier, owner):
            return identifier
        from octop.infra.history.migration_source import (
            SourceSnapshot,
            SourceUnavailable,
            read_source,
        )
        from octop.infra.projects.expert_profiles import _workspace

        row = await asyncio.to_thread(services.agent_repo.get, binding.agent_id)
        thread = await asyncio.to_thread(services.thread_repo.get, thread_id)
        assert row is not None and thread is not None
        cfg = json.loads(row.config_json or "{}")
        workspace = await asyncio.to_thread(_workspace, services, row, cfg)
        try:
            source = await read_source(services, thread, cfg, workspace)
        except SourceUnavailable as exc:
            if str(exc) != "empty_history":
                raise
            source = SourceSnapshot([], hashlib.sha256(b"[]").hexdigest(), {}, [], [])

        instance = await asyncio.to_thread(source_routes.local_instance, services)

        def create() -> None:
            now = source_routes.now_ms()
            with services.db.transaction() as conn:
                if not services.project_task_repo.lock_owner(conn, owner):
                    raise OctopError.localized(ErrorCode.FORBIDDEN)
                if services.project_task_repo.get_internal_in_transaction(conn, identifier, owner):
                    return
                route = UnifiedSourceRouteRepo.ensure(
                    conn,
                    owner_id=owner,
                    source_kind="resident",
                    source_instance=instance,
                    source_key=thread_id,
                    metadata_json=json.dumps({"agent_id": binding.agent_id}),
                    now=now,
                )
                if route.task_id and route.task_id != identifier:
                    raise OctopError.localized(ErrorCode.TASK_REQUEST_CONFLICT)
                services.project_task_repo.insert(
                    conn,
                    ProjectTaskRow(
                        identifier,
                        owner,
                        None,
                        row.name,
                        "resident-first:" + first_request,
                        None,
                        now,
                        now,
                        internal_kind="resident",
                    ),
                )
                if not UnifiedSourceRouteRepo.transition(
                    conn,
                    owner,
                    route.route_id,
                    route.revision,
                    route.state,
                    "task",
                    task_id=identifier,
                    source_digest=source.digest,
                    now=now,
                ):
                    raise OctopError.localized(ErrorCode.TASK_REVISION_CONFLICT)
                if source.wires:
                    seed = identifier + "_history"
                    services.project_task_turn_repo.insert_imported(
                        conn,
                        ProjectTaskTurnRow(
                            seed,
                            identifier,
                            seed,
                            "",
                            "",
                            "completed",
                            "",
                            "",
                            json.dumps(
                                {
                                    "imported_history": True,
                                    "execution_kind": "resident_history",
                                    "source_digest": source.digest,
                                    "messages": source.wires,
                                    "action_timeline_enabled": False,
                                }
                            ),
                            "",
                            "",
                            False,
                            now,
                            now,
                            finished_at=now,
                        ),
                    )
                    if not services.project_task_turn_repo.seed_imported_context_in_transaction(
                        conn, identifier, seed, json.dumps(source.wires, ensure_ascii=False)
                    ):
                        raise OctopError.localized(ErrorCode.TASK_REVISION_CONFLICT)

        await asyncio.to_thread(create)
        return identifier
    finally:
        await asyncio.to_thread(fence.__exit__, None, None, None)


async def admit(services: SharedServices, queued: ResidentRequestRow) -> str:
    identifier = await ensure_task(services, queued.owner_id, queued.thread_id, queued.request_id)

    def write() -> None:
        _require(services, queued.owner_id, queued.thread_id)
        now = source_routes.now_ms()
        with services.db.transaction() as conn:
            if not services.project_task_repo.lock_owner(conn, queued.owner_id):
                raise OctopError.localized(ErrorCode.FORBIDDEN)
            authority = conn.execute(
                "SELECT status,cancel_requested FROM resident_requests WHERE owner_id=? AND turn_id=?",
                (queued.owner_id, queued.turn_id),
            ).fetchone()
            if (
                authority is None
                or authority["status"] != "running"
                or authority["cancel_requested"]
            ):
                raise OctopError.localized(ErrorCode.RESIDENT_INTERRUPTED)
            old = services.project_task_turn_repo.get_in_transaction(conn, queued.turn_id)
            if old is not None:
                if old.task_id != identifier or old.request_id != queued.request_id:
                    raise OctopError.localized(ErrorCode.TASK_REQUEST_CONFLICT)
                return
            task = services.project_task_repo.get_internal_in_transaction(
                conn, identifier, queued.owner_id
            )
            assert task is not None
            if task.active_turn_id:
                raise OctopError.localized(ErrorCode.TASK_BUSY)
            raw = {
                "execution_kind": "resident",
                "resident_thread_id": queued.thread_id,
                "resident_input": json.loads(queued.input_json),
                "resident_configuration": json.loads(queued.configuration_json),
                "action_timeline_enabled": False,
                "presentation": snapshot_presentation(services, False).model_dump(),
                "action_mode": "ask",
            }
            services.project_task_repo.activate(conn, identifier, queued.turn_id, now)
            services.project_task_turn_repo.insert(
                conn,
                ProjectTaskTurnRow(
                    queued.turn_id,
                    identifier,
                    queued.request_id,
                    queued.prompt,
                    "",
                    "queued",
                    "",
                    "",
                    json.dumps(raw, ensure_ascii=False),
                    raw["resident_configuration"]["model_ref"],
                    "",
                    False,
                    queued.created_at,
                    now,
                ),
            )

    await asyncio.to_thread(write)
    return identifier


def cancel(services: SharedServices, owner: int, thread_id: str, turn_id: str) -> None:
    with services.db.transaction() as conn:
        task = services.project_task_repo.get_internal_in_transaction(
            conn, task_id(owner, thread_id), owner
        )
        turn = services.project_task_turn_repo.get_in_transaction(conn, turn_id)
        if (
            task is None
            or turn is None
            or turn.task_id != task.task_id
            or turn.status in {"completed", "cancelled", "failed"}
        ):
            return
        services.project_task_turn_repo.cancel(conn, turn_id, source_routes.now_ms())
        if turn.status == "queued":
            services.project_task_repo.finish(conn, task.task_id, turn_id, source_routes.now_ms())


async def append_message(
    services: SharedServices, owner: int, thread_id: str, message: AIMessage
) -> None:
    identifier = await ensure_task(services, owner, thread_id, str(message.id))

    def write() -> None:
        _require(services, owner, thread_id)
        now = source_routes.now_ms()
        turn_id = "resident_notice_" + hashlib.sha256(str(message.id).encode()).hexdigest()
        with services.db.transaction() as conn:
            task = services.project_task_repo.get_internal_in_transaction(conn, identifier, owner)
            assert task is not None
            if services.project_task_turn_repo.get_in_transaction(conn, turn_id):
                return
            if task.active_turn_id:
                raise OctopError.localized(ErrorCode.TASK_BUSY)
            services.project_task_turn_repo.insert_imported(
                conn,
                ProjectTaskTurnRow(
                    turn_id,
                    identifier,
                    str(message.id),
                    "",
                    str(message.content),
                    "completed",
                    "",
                    "",
                    json.dumps(
                        {
                            "execution_kind": "resident_notification",
                            "resident_message": message_to_dict(message),
                            "action_timeline_enabled": False,
                            "presentation": snapshot_presentation(services, False).model_dump(),
                        },
                        ensure_ascii=False,
                    ),
                    "",
                    "",
                    False,
                    now,
                    now,
                    finished_at=now,
                ),
            )

    await asyncio.to_thread(write)


async def run(
    services: SharedServices, owner: int, turn: ProjectTaskTurnRow, history: list[Any]
) -> AsyncGenerator[dict[str, Any], None]:
    raw = json.loads(turn.configuration_json)
    thread_id = str(raw["resident_thread_id"])
    binding = await asyncio.to_thread(_require, services, owner, thread_id)
    row = await asyncio.to_thread(services.agent_repo.get, binding.agent_id)
    user = await asyncio.to_thread(services.user_repo.get, owner)
    assert row is not None
    providers = ProviderStore(services.provider_repo)
    if not await asyncio.to_thread(providers.is_model_ref_usable, turn.model_ref):
        raise OctopError.localized(ErrorCode.RESIDENT_MODEL_UNAVAILABLE)
    await asyncio.to_thread(
        assert_token_quota_available, services.user_policy_repo, services.usage_repo, owner
    )
    locale = normalize_locale(user.locale if user else "en")
    memory_policy = await asyncio.to_thread(
        services.resident_memory_policy_repo.get, owner, thread_id
    )
    namespace, backend, options = open_memory_kwargs(
        agent_id=binding.agent_id,
        cfg=json.loads(row.config_json or "{}"),
        octop_config=services.config,
        workspace_dir=Path(json.loads(row.config_json or "{}")["workspace_dir"]),
    )
    cfg = resident_harness_config(
        row,
        thread_id=thread_id,
        timezone=services.config.default_timezone,
        locale=locale,
        default_model=turn.model_ref,
        memory_capture_enabled=bool(memory_policy and memory_policy.capture_enabled),
        memory_backend=backend,
        memory_backend_config=options,
    )
    cfg.providers = await asyncio.to_thread(providers.build_harness_configs)
    from octop.infra.projects.model_failover import automatic_route, task_failover_middleware

    user_content = _user_content(turn, locale)
    needs_images = content_blocks_need_vision(user_content) or any(
        content_blocks_need_vision(
            message.get("content")
            if isinstance(message, dict)
            else getattr(message, "content", None)
        )
        for message in history
    )
    if needs_images and not await asyncio.to_thread(
        providers.is_model_ref_multimodal, turn.model_ref
    ):
        raise OctopError.localized(ErrorCode.TASK_IMAGE_MODEL_REQUIRED)
    failover = await task_failover_middleware(services, owner, turn, raw, cfg.providers)
    cfg.middleware = [
        *(cfg.middleware or []),
        failover,
        TaskImageMaterializeMiddleware(services, owner, turn.task_id),
    ]
    cfg.protocol = TASK_PROTOCOL
    cfg.model_retry_enabled = False
    cfg.session_log_enabled = False
    cfg.memory_namespace = namespace
    cfg.system_prompt = raw["resident_configuration"].get("system_prompt") or row.system_prompt
    directory = services.paths.root / "project-tasks" / turn.task_id / turn.turn_id
    await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True)
    from octop.infra.agents.resident_conversation import (
        ResidentInput,
        _known_error_code,
        _task_context,
    )
    from octop.infra.agents.resident_memory import resident_memory_context

    request = await asyncio.to_thread(services.resident_request_repo.get, owner, turn.request_id)
    if request is None or request.turn_id != turn.turn_id or request.cancel_requested:
        raise OctopError.localized(ErrorCode.RESIDENT_INTERRUPTED)
    authorization = ResidentInput.model_validate(raw["resident_input"])
    memory = await asyncio.to_thread(
        resident_memory_context, services, owner, binding.agent_id, thread_id
    )
    usage = UsageTracker()
    reply = ""
    completed = False
    async with aiosqlite.connect(str(directory / "checkpoints.sqlite")) as connection:
        cfg.checkpointer = AsyncSqliteSaver(connection)
        agent = await asyncio.to_thread(HarnessAgent, cfg, agent_id=binding.agent_id)
        try:
            saver = TaskCheckpointSaver(services, owner, turn, agent.checkpointer)
            agent._checkpointer_instance = saver
            agent._base_graph.checkpointer = saver
            agent.graph.checkpointer = saver
            cfg.checkpointer = saver
            with (
                resident_execution_scope(owner, thread_id),
                resident_memory_scope(memory),
                resident_prompt_scope(cfg.system_prompt),
                resident_tools_scope(
                    services=services,
                    owner_id=owner,
                    agent_id=binding.agent_id,
                    thread_id=thread_id,
                    turn_id=turn.turn_id,
                    read_tools=authorization.read_tools,
                    task_context=_task_context(request),
                    knowledge_base_ids=authorization.knowledge_base_ids,
                    web_search_enabled=bool(
                        raw["resident_configuration"].get("web_search_enabled", False)
                    ),
                    attachment_read_enabled=bool(
                        raw["resident_configuration"].get("attachment_read_enabled", False)
                    ),
                ),
            ):
                events = cast(
                    AsyncGenerator[dict[str, Any], None],
                    agent.stream(
                        ChatRequest(
                            messages=[
                                *history,
                                HumanMessage(
                                    content=user_content,
                                    id=f"resident:{turn.turn_id}:user",
                                    additional_kwargs={
                                        "resident_request_id": turn.request_id,
                                        "checkpoint_ts": turn.created_at,
                                    },
                                ),
                            ],
                            thread_id=turn.turn_id,
                            agent_id=binding.agent_id,
                            user=str(owner),
                            source="resident-task",
                            callbacks=failover.callbacks,
                            configurable={
                                "model": turn.model_ref,
                                "octop_auto_route": automatic_route(raw),
                            },
                        ),
                        protocol=TASK_PROTOCOL,
                    ),
                )
                async with contextlib.aclosing(events):
                    async for event in events:
                        await asyncio.to_thread(_require, services, owner, thread_id)
                        usage.observe(event)
                        if event.get("type") == "token":
                            reply += str(event.get("content") or "")
                            yield event
                        elif event.get("type") == "error":
                            raise OctopError.localized(
                                ErrorCode(
                                    _known_error_code(
                                        str(
                                            event.get("error_code")
                                            or ErrorCode.INTERNAL_ERROR.value
                                        )
                                    )
                                )
                            )
                        elif event.get("type") == "hitl_required":
                            raise OctopError.localized(ErrorCode.RESIDENT_TEXT_ONLY)
                        elif event.get("type") in {"reasoning", "reasoning_summary", "message_end"}:
                            yield event
                completed = True
                messages = await model_context(agent, turn.turn_id)
                saved = await asyncio.to_thread(
                    services.project_task_turn_repo.save_context,
                    turn.turn_id,
                    turn.worker_id,
                    json.dumps(
                        [message_to_dict(m) for m in close_pending_tools(messages, locale)],
                        ensure_ascii=False,
                    ),
                )
                if saved and completed:
                    await capture_memory(agent, owner, thread_id, turn.prompt, reply)
        finally:
            if usage.usage:
                await asyncio.to_thread(
                    services.project_task_turn_repo.record_usage,
                    turn.turn_id,
                    int(usage.usage.get("input_tokens") or 0),
                    int(usage.usage.get("output_tokens") or 0),
                )
            try:
                await agent.aclose()
            finally:
                await failover.aclose()
