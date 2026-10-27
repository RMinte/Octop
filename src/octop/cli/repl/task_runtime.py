"""CLI streaming through durable Task ingress, without an expert gateway graph."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from typing import Any, cast

from octop.cli.repl.embedded_session import TaskCliRuntime, embedded_task_runtime
from octop.cli.repl.turn import ChatTurnResult
from octop.i18n import tr
from octop.infra.agents.providers.store import ProviderStore
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects import cli_sessions, ingress, source_routes, tasks
from octop.infra.projects.conversation_migrations import (
    ConversationMigrationWorker,
    MigrationCreate,
    create,
    detail,
    legacy_link,
)
from octop.infra.projects.interaction_models import ApprovalPayload
from octop.infra.server import OctopServer
from octop.infra.users.acting import resolve_acting_user_id


def resolve_embedded_user_id(
    server: OctopServer | TaskCliRuntime, *, agent_id: str | None, as_user: str | None
) -> int:
    from octop.cli.support.ctx import resolve_user
    from octop.cli.support.state import default_state_path, load

    assert server.services is not None
    return resolve_acting_user_id(
        server.services,
        as_username=resolve_user(as_user),
        pinned_username=load(default_state_path()).default_user,
        agent_id=agent_id,
    )


class CliChatSession:
    """One observer of durable Task turns; disconnecting does not cancel work."""

    def __init__(self, server: OctopServer | TaskCliRuntime, *, user_id: int) -> None:
        if server.services is None:
            raise OctopError.localized(ErrorCode.INTERNAL_ERROR)
        self.server, self.services, self.user_id = server, server.services, user_id
        self.task_id: str | None = None
        self.turn_id: str | None = None
        self._fresh = False

    def close(self) -> None:
        pass

    async def _migrate(self, thread_id: str, project_id: str | None = None) -> None:
        link = await asyncio.to_thread(legacy_link, self.services, self.user_id, thread_id)
        if link.task_id:
            return
        if link.state == "target_deleted":
            raise OctopError.localized(ErrorCode.NOT_FOUND)
        batch = await asyncio.to_thread(
            create,
            self.services,
            self.user_id,
            MigrationCreate(
                request_id="cli_migration_"
                + uuid.uuid5(uuid.NAMESPACE_URL, thread_id + ":" + (project_id or "")).hex,
                thread_ids=[thread_id],
                project_id=project_id,
            ),
        )
        worker = getattr(
            self.server, "conversation_migration_worker", None
        ) or ConversationMigrationWorker(self.services)
        while await worker.run_once(batch.batch_id):
            pass
        result = await asyncio.to_thread(detail, self.services, self.user_id, batch.batch_id)
        if not result.items or result.items[0].status != "completed":
            raise OctopError.localized(ErrorCode.TASK_BUSY)

    async def _command(
        self,
        text: str,
        *,
        session_key: str,
        request_id: str,
        locale: str,
        saved: dict[str, Any],
        on_chunk: Callable[[dict[str, Any]], None],
    ) -> ChatTurnResult:
        command, _, argument = text.partition(" ")
        reply = ""
        actions: list[dict[str, Any]] = []
        if command == "/new":
            await asyncio.to_thread(
                cli_sessions.draft, self.services, self.user_id, session_key, request_id
            )
            self._fresh, self.task_id, self.turn_id = True, None, None
            reply = tr("cli.unified_tasks.new_draft", locale)
            actions.append({"action": "new_chat", "task_id": None})
        elif command in {"/task", "/thread"}:
            reply = self.task_id or tr("cli.unified_tasks.new_draft", locale)
        elif command in {"/model", "/mode", "/agent", "/expert", "/general"}:
            if command == "/model":
                if argument and not await asyncio.to_thread(
                    ProviderStore(self.services.provider_repo).is_model_ref_usable, argument
                ):
                    raise OctopError.localized(ErrorCode.TASK_MODEL_UNAVAILABLE)
                saved["model_ref"] = argument or None
                actions.append(
                    {"action": "set_model" if argument else "clear_model", "model": argument}
                )
            elif command == "/mode":
                if argument not in {"ask", "plan", "craft"}:
                    raise OctopError.localized(ErrorCode.TASK_CONTROL_INVALID)
                saved["action_mode"] = argument
                actions.append({"action": "set_conversation_mode", "mode": argument})
            else:
                saved["selected_expert_id"] = (
                    None if command == "/general" or not argument else argument
                )
                actions.append({"action": "switch_agent", "agent_id": saved["selected_expert_id"]})
            await asyncio.to_thread(
                cli_sessions.save_preferences, self.services, self.user_id, session_key, saved
            )
            reply = argument or tr("cli.unified_tasks.general", locale)
        elif command == "/models":
            resources = await asyncio.to_thread(tasks.resources, self.services, self.user_id)
            reply = "\n".join(option.model_ref for option in resources.models)
        elif command in {"/help", "/exit", "/quit"}:
            reply = tr("cli.unified_tasks.help", locale)
        else:
            reply = tr("cli.unified_tasks.unknown_command", locale, command=command)
        on_chunk({"type": "token", "content": reply})
        for action in actions:
            on_chunk({"type": "slash_action", **action})
        on_chunk({"type": "done"})
        return ChatTurnResult(text=reply, actions=actions, task_id=self.task_id)

    async def run_turn(
        self,
        agent_id: str | None,
        text: str,
        *,
        session_key: str,
        thread_id: str | None,
        model: str | None,
        conversation_mode: str | None = None,
        on_chunk: Callable[[dict[str, Any]], None],
        task_id: str | None = None,
        request_id: str | None = None,
        project_id: str | None = None,
        no_expert: bool = False,
        engine: tasks.EngineSelection | None = None,
    ) -> ChatTurnResult:
        started = time.monotonic()
        user = await asyncio.to_thread(self.services.user_repo.get, self.user_id)
        locale = user.locale if user and user.locale in {"zh", "en"} else "zh"
        source = await asyncio.to_thread(
            cli_sessions.envelope,
            self.services,
            self.user_id,
            session_key=session_key,
            request_id=request_id or uuid.uuid4().hex,
            text=text,
            task_id=task_id if not self._fresh else None,
            thread_id=thread_id if not self._fresh else None,
            agent_id=agent_id,
            intent={
                "expert": agent_id,
                "no_expert": no_expert,
                "project": project_id,
                "model": model,
                "mode": conversation_mode,
                **({"engine": engine.model_dump()} if engine else {}),
            },
        )
        if source.legacy_thread_id:
            await self._migrate(source.legacy_thread_id, project_id)
        # Lookup precedes interaction resolution: an approval retry must not
        # accidentally approve a newer action with the same human command.
        previous = await asyncio.to_thread(ingress.lookup, self.services, self.user_id, source)
        route = await asyncio.to_thread(
            source_routes.resolve,
            self.services,
            self.user_id,
            source_kind=source.source_kind,
            source_instance=source.source_instance,
            source_key=source.source_key,
            legacy_thread_id=source.legacy_thread_id,
            legacy_agent_id=source.legacy_agent_id,
        )
        self.task_id = route.task_id
        saved = await asyncio.to_thread(
            cli_sessions.preferences, self.services, self.user_id, session_key
        )
        command, _, argument = text.partition(" ")
        controls = {"/approve", "/deny", "/answer", "/cancel"}
        if text.startswith("/") and command not in controls and previous is None:
            return await self._command(
                text,
                session_key=session_key,
                request_id=source.event_id,
                locale=locale,
                saved=saved,
                on_chunk=on_chunk,
            )
        selected_engine = engine
        if selected_engine is None and self.task_id:
            selected_engine = (
                await asyncio.to_thread(tasks.detail, self.services, self.user_id, self.task_id)
            ).engine
        selected_engine = selected_engine or tasks.EngineSelection()
        options = tasks.TaskExecutionOptions(
            model_ref=model
            or (saved.get("model_ref") if selected_engine.engine_id == "octop" else None)
        )
        if previous is None and command in controls:
            if not self.task_id:
                raise OctopError.localized(ErrorCode.NOT_FOUND)
            current = await asyncio.to_thread(
                tasks.detail, self.services, self.user_id, self.task_id
            )
            if command == "/cancel":
                turn_id = argument or current.active_turn_id
                if not turn_id:
                    raise OctopError.localized(ErrorCode.TASK_CONTROL_INVALID)
                source = source.model_copy(update={"operation": "cancel", "turn_id": turn_id})
            else:
                interaction_id, _, answer = argument.partition(" ")
                interaction = next(
                    (
                        interaction
                        for turn in current.turns
                        for interaction in turn.interactions
                        if interaction.interaction_id == interaction_id
                        and interaction.status == "pending"
                    ),
                    None,
                )
                if interaction is None:
                    raise OctopError.localized(ErrorCode.TASK_INTERACTION_CONFLICT)
                if command == "/answer":
                    if interaction.kind != "question" or not answer.strip():
                        raise OctopError.localized(ErrorCode.TASK_CONTROL_INVALID)
                    response = {"kind": "question", "action": "answer", "answer": answer}
                else:
                    if not isinstance(interaction.payload, ApprovalPayload):
                        raise OctopError.localized(ErrorCode.TASK_CONTROL_INVALID)
                    response = {
                        "kind": "approval",
                        "decision": "allow_once" if command == "/approve" else "deny",
                        "action_digest": interaction.payload.action_digest,
                    }
                source = source.model_copy(
                    update={
                        "operation": "respond",
                        "turn_id": interaction.turn_id,
                        "interaction_id": interaction.interaction_id,
                        "interaction_response": {
                            "expected_revision": interaction.revision,
                            "response": response,
                        },
                    }
                )
        elif previous is None and self.task_id:
            await asyncio.to_thread(
                cli_sessions.configure,
                self.services,
                self.user_id,
                self.task_id,
                expert_id=None if no_expert else agent_id or saved.get("selected_expert_id"),
                change_expert=no_expert or agent_id is not None or "selected_expert_id" in saved,
                mode=conversation_mode or saved.get("action_mode"),
                project_id=project_id,
                engine=engine,
            )
        message = tasks.TaskCreate(
            request_id=source.event_id,
            prompt=text,
            project_id=project_id,
            selected_expert_id=None if no_expert else agent_id or saved.get("selected_expert_id"),
            action_mode=cast(
                tasks.ActionMode, conversation_mode or saved.get("action_mode") or "craft"
            ),
            options=options,
            engine=selected_engine,
        )
        receipt = previous or await ingress.submit(self.services, self.user_id, source, message)
        task_worker = self.server.project_task_worker
        if task_worker is not None:
            task_worker.wake()
        old_reply = ""
        tools: dict[str, str] = {}
        blocks: dict[str, str] = {}
        pending: set[str] = set()
        final = None
        async for turn in ingress.observe(self.services, self.user_id, receipt, worker=task_worker):
            receipt = await asyncio.to_thread(
                ingress.get_receipt, self.services, self.user_id, receipt.inbox_id
            )
            self.task_id, self.turn_id, final = receipt.task_id, turn.turn_id, turn
            text_blocks = [block for block in turn.content_blocks if block.type == "text"]
            if not text_blocks and turn.reply.startswith(old_reply):
                delta = turn.reply[len(old_reply) :]
                if delta:
                    on_chunk({"type": "token", "content": delta})
            old_reply = turn.reply
            for block in turn.content_blocks:
                if block.type == "text":
                    before = blocks.get(block.block_id, "")
                    if block.text.startswith(before) and block.text != before:
                        label = (
                            f"\n[{block.speaker_name or block.speaker_agent_id}]\n"
                            if not before and block.speaker_agent_id
                            else ""
                        )
                        on_chunk(
                            {
                                "type": "token",
                                "content": label + block.text[len(before) :],
                                "speaker_agent_id": block.speaker_agent_id,
                                "speaker_name": block.speaker_name,
                            }
                        )
                    blocks[block.block_id] = block.text
                    continue
                if block.type != "tool":
                    continue
                call_id = block.tool_call_id or block.block_id
                if call_id not in tools:
                    on_chunk(
                        {
                            "type": "tool_start",
                            "id": call_id,
                            "name": block.tool_name,
                            "args": block.arguments,
                            "speaker_agent_id": block.speaker_agent_id,
                            "speaker_name": block.speaker_name,
                        }
                    )
                if (
                    block.status in {"completed", "failed", "interrupted"}
                    and tools.get(call_id) != block.status
                ):
                    on_chunk(
                        {
                            "type": "tool_end",
                            "id": call_id,
                            "name": block.tool_name,
                            "content": block.output,
                            "failed": block.status != "completed",
                        }
                    )
                tools[call_id] = block.status
            for interaction in turn.interactions:
                if interaction.status == "pending" and interaction.interaction_id not in pending:
                    pending.add(interaction.interaction_id)
                    payload = interaction.payload.model_dump(mode="json")
                    on_chunk(
                        {
                            "type": "hitl_required",
                            "pending_id": interaction.interaction_id,
                            "request": {
                                "action_requests": [
                                    {
                                        "name": payload.get("tool_name", "ask_user_question"),
                                        "args": payload.get("arguments")
                                        or {"questions": [payload]},
                                    }
                                ]
                            },
                            "interaction": interaction.model_dump(mode="json"),
                        }
                    )
        if final and final.status == "failed":
            try:
                code = ErrorCode(final.error_code)
            except ValueError:
                code = ErrorCode.INTERNAL_ERROR
            on_chunk({"type": "error", "message": tr("errors." + code.value, locale)})
        on_chunk(
            {
                "type": "done",
                "task_id": self.task_id,
                "turn_id": self.turn_id,
                "status": final.status if final else receipt.status,
            }
        )
        return ChatTurnResult(
            text=old_reply,
            elapsed=time.monotonic() - started,
            task_id=self.task_id,
            turn_id=self.turn_id,
            status=final.status if final else receipt.status,
        )


async def run_chat_turn_async(
    agent_id: str | None,
    text: str,
    *,
    as_user: str | None,
    session_key: str,
    thread_id: str | None,
    model: str | None,
    conversation_mode: str | None = None,
    on_chunk: Callable[[dict[str, Any]], None],
    task_id: str | None = None,
    request_id: str | None = None,
    project_id: str | None = None,
    no_expert: bool = False,
    engine: tasks.EngineSelection | None = None,
) -> ChatTurnResult:
    async with embedded_task_runtime() as server:
        user_id = resolve_embedded_user_id(server, agent_id=agent_id, as_user=as_user)
        session = CliChatSession(server, user_id=user_id)
        try:
            return await session.run_turn(
                agent_id,
                text,
                session_key=session_key,
                thread_id=thread_id,
                model=model,
                conversation_mode=conversation_mode,
                on_chunk=on_chunk,
                task_id=task_id,
                request_id=request_id,
                project_id=project_id,
                no_expert=no_expert,
                engine=engine,
            )
        finally:
            session.close()
