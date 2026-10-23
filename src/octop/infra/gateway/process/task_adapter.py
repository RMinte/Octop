"""Channel projection of canonical Task turns; no direct Harness execution."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from octop_gateway.models import FileContent, InboundMessage, MessageEvent

from octop.i18n import (
    channel_tool_hint_end,
    channel_tool_hint_start,
    error_message,
    tool_display_name,
    tr,
)
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.gateway.hitl.format import format_hitl_card
from octop.infra.gateway.media.source_store import SourceMediaBackend
from octop.infra.gateway.process.harness_request import _group_turn_text
from octop.infra.gateway.process.message_keys import (
    resolve_user_id_for_message,
    session_key_from_message,
)
from octop.infra.gateway.slash.parser import parse_slash
from octop.infra.gateway.threads import ThreadRegistry
from octop.infra.projects import (
    artifacts,
    deliveries,
    ingress,
    input_attachments,
    source_routes,
    tasks,
)
from octop.infra.projects.interaction_models import ApprovalPayload, QuestionPayload
from octop.infra.utils.locale import resolve_user_locale


def event_id(message: InboundMessage) -> str:
    for key in ("message_id", "msg_id", "event_id", "messageId", "MsgId", "id"):
        value = (message.metadata or {}).get(key)
        if isinstance(value, (str, int)) and str(value):
            return str(value)
    # Some gateway parsers expose only their original timestamp. Preserve that
    # identity across retries; never deduplicate two equal texts alone.
    return hashlib.sha256(
        json.dumps(
            {
                "timestamp": str(message.timestamp),
                "content": [part.model_dump(mode="json") for part in message.content],
            },
            sort_keys=True,
            ensure_ascii=False,
        ).encode()
    ).hexdigest()


class TaskChannelAdapter:
    def __init__(self, services: SharedServices, worker: Any = None, gateway: Any = None) -> None:
        self.services, self.worker, self.gateway = services, worker, gateway

    def context(
        self, message: InboundMessage
    ) -> tuple[int, ingress.SourceEnvelope, str | None, str]:
        services = self.services
        agent_id = message.tenant_id or None
        channel = services.channel_repo.get(message.channel_id)
        if ThreadRegistry.is_virtual_channel(message.channel_type):
            owner = resolve_user_id_for_message(message, agent_owner_id=None)
        else:
            if channel is None or not channel.enabled:
                raise OctopError.localized(ErrorCode.FORBIDDEN)
            owner = channel.user_id
            agent_id = channel.agent_id or None
        user = services.user_repo.get(owner)
        if user is None or user.disabled:
            raise OctopError.localized(ErrorCode.FORBIDDEN)
        key = session_key_from_message(message, agent_id=agent_id or "general")
        session = services.session_repo.get(key)
        explicit = (
            (message.metadata or {}).get("thread_id")
            if ThreadRegistry.is_virtual_channel(message.channel_type)
            else None
        )
        legacy = (
            explicit
            if isinstance(explicit, str) and explicit
            else session.thread_id
            if session
            else None
        )
        if legacy:
            thread = services.thread_repo.get(legacy)
            if thread is None or thread.user_id != owner:
                raise OctopError.localized(ErrorCode.NOT_FOUND)
            if services.thread_repo.is_resident(legacy):
                raise OctopError.localized(ErrorCode.FORBIDDEN)
        locale = resolve_user_locale(
            user_repo=services.user_repo,
            user_id=owner,
            channel_type=message.channel_type,
            metadata=message.metadata,
        )
        source = ingress.SourceEnvelope(
            source_kind=message.channel_type
            if ThreadRegistry.is_virtual_channel(message.channel_type)
            else "im",
            source_instance=message.channel_id or source_routes.local_instance(services),
            source_key=key,
            event_id=event_id(message),
            content_digest=hashlib.sha256(
                json.dumps(
                    [part.model_dump(mode="json") for part in message.content],
                    sort_keys=True,
                    ensure_ascii=False,
                ).encode()
            ).hexdigest(),
            legacy_thread_id=legacy,
            legacy_agent_id=agent_id,
            authority={"principal": "external_unverified", "owner_approval_required": True}
            if not ThreadRegistry.is_virtual_channel(message.channel_type)
            else {},
            reply_target={
                "channel_id": message.channel_id,
                "channel_type": message.channel_type,
                "subject_id": message.channel_subject.subject_id if message.channel_subject else "",
                "metadata": {
                    key: value
                    for key, value in (message.metadata or {}).items()
                    if key
                    in {
                        "chat_id",
                        "chatid",
                        "chat_type",
                        "openid",
                        "open_kfid",
                        "external_userid",
                        "sender_id",
                    }
                },
            },
        )
        return owner, source, agent_id, locale

    async def _attachments(self, message: InboundMessage, owner: int) -> list[str]:
        backend = SourceMediaBackend(
            self.services.paths.root / "source-media" / str(owner) / message.channel_id,
            self.services.config.max_upload_bytes,
        )
        result = []
        for part in message.content:
            if getattr(part, "type", "") == "text":
                continue
            data = getattr(part, "data", None)
            if isinstance(data, str):
                data = base64.b64decode(data, validate=True)
            path = getattr(part, "local_path", None)
            if not data and path:
                data = await backend.read(path)
            if not isinstance(data, bytes):
                raise OctopError.localized(ErrorCode.TASK_ATTACHMENT_UNAVAILABLE)
            name = getattr(part, "filename", None) or Path(path or "attachment").name
            attachment = await asyncio.to_thread(
                input_attachments.save,
                self.services,
                owner,
                name,
                data,
                getattr(part, "mime_type", None),
            )
            result.append(attachment.attachment_id)
        return result

    async def submit(self, message: InboundMessage) -> tuple[ingress.SubmissionReceipt, str]:
        owner, source, expert, locale = await asyncio.to_thread(self.context, message)
        route = await asyncio.to_thread(
            source_routes.resolve,
            self.services,
            owner,
            source_kind=source.source_kind,
            source_instance=source.source_instance,
            source_key=source.source_key,
            legacy_thread_id=source.legacy_thread_id,
            legacy_agent_id=source.legacy_agent_id,
        )
        replay = await asyncio.to_thread(ingress.lookup, self.services, owner, source)
        if replay is not None:
            if replay.status == "received":
                try:
                    replay = await ingress.admit(self.services, owner, replay.inbox_id)
                except OctopError as exc:
                    if exc.code != ErrorCode.TASK_BUSY:
                        raise
            return replay, locale
        defaults = json.loads(route.metadata_json).get("settings", {})
        task = (
            await asyncio.to_thread(tasks.detail, self.services, owner, route.task_id)
            if route.task_id
            else None
        )
        command = parse_slash(message.text)
        body = tasks.TaskCreate(
            request_id="source-placeholder",
            prompt=message.text or "attachment",
            selected_expert_id=expert,
        )
        channel = await asyncio.to_thread(self.services.channel_repo.get, message.channel_id)
        binding = json.loads(channel.config_json or "{}").get("_octop_task", {}) if channel else {}
        meta = message.metadata or {}
        options = {
            key: meta[key]
            for key in (
                "model_ref",
                "skills",
                "mcp_servers",
                "knowledge_base_ids",
                "reasoning_mode",
                "reasoning_effort",
            )
            if key in meta
        }
        if meta.get("model"):
            options["model_ref"] = meta["model"]
        if "model_ref" not in options and defaults.get("model_ref"):
            options["model_ref"] = defaults["model_ref"]
        body = body.model_copy(
            update={
                "options": tasks.TaskExecutionOptions.model_validate(options),
                "project_id": binding.get("project_id"),
                "action_mode": meta.get("conversation_mode")
                if meta.get("conversation_mode") in {"ask", "plan", "craft"}
                else defaults.get("action_mode", binding.get("action_mode", "craft")),
            }
        )
        turn = (
            next(
                (
                    turn
                    for turn in (task.turns if task else [])
                    if turn.turn_id == task.active_turn_id
                ),
                None,
            )
            if task
            else None
        )
        pending = [item for item in (turn.interactions if turn else []) if item.status == "pending"]
        if command and command.name in {"stop", "cancel"}:
            if turn is None:
                raise OctopError.localized(ErrorCode.TASK_CONTROL_INVALID)
            source = source.model_copy(update={"operation": "cancel", "turn_id": turn.turn_id})
        elif command and command.name in {"new", "clear"}:
            source = source.model_copy(update={"operation": "reset"})
        elif command and command.name in {"approve", "reject", "deny", "respond"}:
            parts = command.args.split(maxsplit=1)
            selected = next(
                (item for item in pending if parts and item.interaction_id == parts[0]), None
            )
            if selected is None and len(pending) == 1 and not parts:
                selected = pending[0]
            if selected is None or turn is None:
                raise OctopError.localized(ErrorCode.TASK_INTERACTION_CLOSED)
            if isinstance(selected.payload, ApprovalPayload) and command.name in {
                "approve",
                "reject",
                "deny",
            }:
                response = {
                    "kind": "approval",
                    "decision": "allow_once" if command.name == "approve" else "deny",
                    "action_digest": selected.payload.action_digest,
                }
            elif (
                isinstance(selected.payload, QuestionPayload)
                and command.name == "respond"
                and len(parts) > 1
            ):
                response = {"kind": "question", "action": "answer", "answer": parts[1]}
            else:
                raise OctopError.localized(ErrorCode.TASK_INTERACTION_CONFLICT)
            source = source.model_copy(
                update={
                    "operation": "respond",
                    "turn_id": turn.turn_id,
                    "interaction_id": selected.interaction_id,
                    "interaction_response": {
                        "expected_revision": selected.revision,
                        "response": response,
                    },
                }
            )
        elif command and command.name == "compact":
            body = body.model_copy(update={"prompt": "/compact"})
        elif command and command.name in {
            "help",
            "status",
            "model",
            "models",
            "mode",
            "ask",
            "plan",
            "craft",
            "pending",
        }:
            text, settings = await self._command(command, task, pending, locale, defaults)
            source = source.model_copy(
                update={"source_settings": settings, "display_only": not bool(settings)}
            )
            receipt = await asyncio.to_thread(
                ingress.record_notification,
                self.services,
                owner,
                source,
                text,
                title=message.text,
                expert_id=expert,
                project_id=body.project_id,
            )
            return receipt, locale
        elif command:
            # Commands that used to mutate an expert thread must be explicitly
            # adapted, rather than silently executing the old Harness path.
            raise OctopError.localized(
                ErrorCode.TASK_CONTROL_INVALID, details={"unsupported_command": command.name}
            )
        elif (
            pending and all(isinstance(item.payload, QuestionPayload) for item in pending) and turn
        ):
            item = pending[0]
            source = source.model_copy(
                update={
                    "operation": "respond",
                    "turn_id": turn.turn_id,
                    "interaction_id": item.interaction_id,
                    "interaction_response": {
                        "expected_revision": item.revision,
                        "response": {
                            "kind": "question",
                            "action": "answer",
                            "answer": message.text,
                        },
                    },
                }
            )
        else:
            attachments = await self._attachments(message, owner)
            text = _group_turn_text(message, message.text, locale=locale)
            body = body.model_copy(update={"prompt": text, "attachment_ids": attachments})
        receipt = await ingress.submit(self.services, owner, source, body)
        if self.worker:
            self.worker.wake()
        return receipt, locale

    async def _command(
        self, command: Any, task: Any, pending: list[Any], locale: str, defaults: dict[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        if command.name == "help":
            return tr("channel.task_help", locale), {}
        if command.name == "pending":
            if not pending:
                return tr("slash.hitl.pending_empty", locale), {}
            return "\n\n".join(self._interaction_card(item, locale) for item in pending), {}
        if command.name in {"mode", "ask", "plan", "craft"}:
            mode = command.args.strip() if command.name == "mode" else command.name
            if not mode:
                return tr(
                    "slash.mode.current",
                    locale,
                    mode=task.action_mode if task else defaults.get("action_mode", "craft"),
                ), {}
            if mode not in {"ask", "plan", "craft"}:
                return tr("slash.mode.usage", locale), {}
            return tr("slash.mode.set", locale, mode=mode), {"action_mode": mode}
        if command.name in {"model", "models"}:
            model = command.args.strip()
            if not model:
                return tr(
                    "slash.model.current",
                    locale,
                    model=defaults.get("model_ref") or tr("slash.status.model_auto", locale),
                ), {}
            if model in {"reset", "auto", "default"}:
                return tr("slash.model.cleared", locale), {"model_ref": None}
            from octop.infra.agents.providers.store import ProviderStore

            if not await asyncio.to_thread(
                ProviderStore(self.services.provider_repo).is_model_ref_usable, model
            ):
                raise OctopError.localized(ErrorCode.TASK_MODEL_UNAVAILABLE)
            return tr("slash.model.set", locale, model=model), {"model_ref": model}
        return tr(
            "channel.task_status",
            locale,
            task_id=task.task_id if task else "—",
            status=task.status if task else "—",
            mode=task.action_mode if task else defaults.get("action_mode", "craft"),
            pending=len(pending),
        ), {}

    @staticmethod
    def _interaction_card(item: Any, locale: str) -> str:
        action = (
            {
                "name": item.payload.tool_name,
                "args": item.payload.arguments,
                "description": item.payload.reason,
            }
            if isinstance(item.payload, ApprovalPayload)
            else {
                "name": "ask_user_question",
                "args": {"questions": [item.payload.model_dump(mode="json")]},
            }
        )
        card = format_hitl_card([action], pending_id=item.interaction_id, locale=locale)
        if isinstance(item.payload, ApprovalPayload):
            card = card.replace(
                tr("slash.hitl.card_footer", locale), tr("channel.task_owner_approval", locale)
            )
        return card

    async def process(self, message: InboundMessage) -> AsyncIterator[MessageEvent]:
        receipt, locale = await self.submit(message)
        if receipt.turn_id is None and receipt.status == "admitted":
            yield MessageEvent.completed()
            return
        owner, _, _, _ = await asyncio.to_thread(self.context, message)
        yield MessageEvent.typing()
        shown = ""
        tools_started = set()
        tools_finished = set()
        async for turn in ingress.observe(self.services, owner, receipt, worker=self.worker):
            receipt = await asyncio.to_thread(
                ingress.get_receipt, self.services, owner, receipt.inbox_id
            )
            assert receipt.task_id is not None
            for block in turn.content_blocks:
                if block.type != "tool":
                    continue
                key = block.tool_call_id or block.block_id
                name = block.tool_name or "tool"
                label = tool_display_name(name, locale)
                if key not in tools_started:
                    tools_started.add(key)
                    yield MessageEvent.tool_start(
                        label, tool_hint_text=channel_tool_hint_start(label, locale), tool_key=name
                    )
                if block.status != "streaming" and key not in tools_finished:
                    tools_finished.add(key)
                    yield MessageEvent.tool_end(
                        label, tool_hint_text=channel_tool_hint_end(label, locale)
                    )
            if turn.reply.startswith(shown) and turn.reply != shown:
                text = turn.reply[len(shown) :]
                row = await asyncio.to_thread(
                    deliveries.enqueue,
                    self.services,
                    owner,
                    receipt.route_id,
                    receipt.task_id,
                    turn.turn_id,
                    "text:" + str(len(turn.reply)),
                    {"text": text},
                )
                claimed = await asyncio.to_thread(deliveries.claim, self.services, row)
                if claimed:
                    sent = False
                    try:
                        # A MESSAGE is handed to the channel immediately. DELTA
                        # may stay buffered until COMPLETED and cannot be receipted here.
                        yield MessageEvent.text(text)
                        sent = True
                    finally:
                        await asyncio.to_thread(
                            deliveries.finish, self.services, claimed, sent=sent
                        )
                shown = turn.reply
            for item in turn.interactions:
                if item.status != "pending":
                    continue
                text = self._interaction_card(item, locale)
                row = await asyncio.to_thread(
                    deliveries.enqueue,
                    self.services,
                    owner,
                    receipt.route_id,
                    receipt.task_id,
                    turn.turn_id,
                    "interaction:" + item.interaction_id + ":" + str(item.revision),
                    {"text": text},
                )
                claimed = await asyncio.to_thread(deliveries.claim, self.services, row)
                if claimed:
                    sent = False
                    try:
                        yield MessageEvent.text(text)
                        sent = True
                    finally:
                        await asyncio.to_thread(
                            deliveries.finish, self.services, claimed, sent=sent
                        )
            for attachment in turn.artifacts:
                row = await asyncio.to_thread(
                    deliveries.enqueue,
                    self.services,
                    owner,
                    receipt.route_id,
                    receipt.task_id,
                    turn.turn_id,
                    "artifact:" + attachment.artifact_id,
                    {"artifact_id": attachment.artifact_id},
                )
                claimed = await asyncio.to_thread(deliveries.claim, self.services, row)
                if claimed:
                    sent = False
                    try:
                        artifact, data = await asyncio.to_thread(
                            artifacts.download,
                            self.services,
                            owner,
                            receipt.task_id,
                            attachment.artifact_id,
                        )
                        yield MessageEvent(
                            content=[
                                FileContent(
                                    filename=artifact.name,
                                    mime_type=artifact.media_type,
                                    size=len(data),
                                    data=base64.b64encode(data).decode(),
                                )
                            ]
                        )
                        sent = True
                    finally:
                        await asyncio.to_thread(
                            deliveries.finish, self.services, claimed, sent=sent
                        )
            if turn.status in {"failed", "interrupted"} and turn.error_code:
                yield MessageEvent.error_event(error_message(turn.error_code, locale))
        yield MessageEvent.completed()

    async def chunks(self, message: InboundMessage) -> AsyncIterator[dict[str, Any]]:
        receipt, _ = await self.submit(message)
        owner, _, _, _ = await asyncio.to_thread(self.context, message)
        shown = ""
        if receipt.turn_id or receipt.status == "received":
            async for turn in ingress.observe(self.services, owner, receipt, worker=self.worker):
                receipt = await asyncio.to_thread(
                    ingress.get_receipt, self.services, owner, receipt.inbox_id
                )
                if turn.reply.startswith(shown) and turn.reply != shown:
                    yield {"type": "token", "content": turn.reply[len(shown) :]}
                    shown = turn.reply
                if turn.interactions:
                    yield {
                        "type": "hitl_required",
                        "request": {
                            "task_id": receipt.task_id,
                            "turn_id": turn.turn_id,
                            "interactions": [
                                item.model_dump(mode="json") for item in turn.interactions
                            ],
                        },
                    }
        yield {
            "type": "done",
            "task_id": receipt.task_id,
            "thread_id": receipt.task_id,
            "turn_id": receipt.turn_id,
        }
