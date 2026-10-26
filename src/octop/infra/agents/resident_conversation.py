"""Durable text turns for a private resident Agent, independent of HTTP clients."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import aclosing, asynccontextmanager, suppress
from datetime import UTC, datetime
from functools import partial
from typing import Annotated, Any, Literal, cast
from zoneinfo import ZoneInfo

from octop_gateway.models import ChannelSubject, InboundMessage, TextContent
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from octop.i18n import error_message, tr
from octop.infra.agents.manager import AgentManager
from octop.infra.agents.resident_identity import (
    get_resident_identity,
    resident_execution_scope,
    resident_memory_scope,
    resident_prompt_scope,
)
from octop.infra.agents.resident_reporting import ResidentReportDelivery
from octop.infra.agents.resident_tools import (
    ResidentTaskContext,
    ResidentToolInput,
    default_task_context,
    resident_tools_scope,
)
from octop.infra.agents.resident_worker_lock import ResidentWorkerLock
from octop.infra.conversations.updates import CommittedUpdates
from octop.infra.db.repos.resident_conversations import ResidentConversationRow
from octop.infra.db.repos.resident_delegations import ResidentTaskReportRow
from octop.infra.db.repos.resident_notifications import ResidentNotificationRow
from octop.infra.db.repos.resident_requests import (
    ResidentRequestArchived,
    ResidentRequestBusy,
    ResidentRequestConflict,
    ResidentRequestRow,
)
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.gateway.gateway import Gateway
from octop.infra.gateway.threads import ThreadRegistry
from octop.infra.gateway.ws import WS_CHANNEL_ID
from octop.infra.history.service import HistoryArchive
from octop.infra.users.preferences import get_preferred_model_from_json
from octop.infra.utils.ulid import new_ulid

logger = logging.getLogger(__name__)
ACTIVE = {"queued", "running"}
LEASE_MS = 30_000
TurnStatus = Literal["queued", "running", "completed", "failed", "cancelled", "interrupted"]
BindingStatus = Literal["not_created", "initializing", "ready", "failed"]


def now_ms() -> int:
    return time.time_ns() // 1_000_000


async def _settled_io[IO](function: Callable[..., IO], *args: Any, **kwargs: Any) -> IO:
    """Keep process/thread ownership until an admitted persistence mutation settles."""
    operation = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    cancelled = False
    while not operation.done():
        try:
            await asyncio.shield(operation)
        except asyncio.CancelledError:
            if operation.cancelled():
                raise
            cancelled = True
        except Exception:
            break
    if cancelled:
        with suppress(Exception):
            operation.result()
        raise asyncio.CancelledError
    return operation.result()


class ResidentModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ResidentBinding(ResidentModel):
    status: BindingStatus
    agent_id: str | None = None
    thread_id: str | None = None
    name: str | None = None
    timezone: str
    error_code: str | None = None


class ResidentConversationStream(ResidentModel):
    transport: Literal["websocket"] = "websocket"
    path: Literal["/api/project-tasks/ws"] = "/api/project-tasks/ws"
    protocol_version: Literal[3] = 3


class ResidentCapabilities(ResidentModel):
    api_version: Literal[1] = 1
    identity: Literal["private_agent_and_thread"] = "private_agent_and_thread"
    identity_initialization: Literal["server_managed"] = "server_managed"
    assistant_role: Literal["life_work_steward"] = "life_work_steward"
    professional_work: Literal["delegated_tasks"] = "delegated_tasks"
    conversation_transport: Literal["rest_polling"] = "rest_polling"
    conversation_stream: ResidentConversationStream = Field(
        default_factory=ResidentConversationStream,
        description="Preferred live observation. REST snapshots and command receipts remain available; this declaration does not mean a client has subscribed.",
    )
    message_types: list[Literal["text"]] = ["text"]
    history_days_search_location: bool = True
    memory_management: bool = True
    memory_capture: Literal["explicit_opt_in_raw_events"] = "explicit_opt_in_raw_events"
    memory_candidates: Literal["explicit_extract_and_user_approval"] = (
        "explicit_extract_and_user_approval"
    )
    task_delegation: Literal["natural_language"] = "natural_language"
    turn_read_tools: bool = True
    selected_knowledge_search: bool = True
    model_task_delegation: Literal["one_per_turn"] = "one_per_turn"
    followup_kinds: list[Literal["note", "task_watch", "task"]] = ["note", "task_watch", "task"]
    notification_target: Literal["conversation"] = "conversation"
    shared_read_watermark: bool = True
    context_compaction: bool = True


RequestId = Annotated[
    str,
    StringConstraints(
        min_length=1, max_length=200, strip_whitespace=True, pattern=r"^[A-Za-z0-9_-]+$"
    ),
]


class ResidentMessage(ResidentToolInput):
    request_id: RequestId = Field(description="Stable send identity; reuse after a lost response.")
    prompt: Annotated[str, StringConstraints(min_length=1, max_length=32768)] = Field(
        description="Plain user text; the steward may delegate requested work to a task. Optional task_context selects additional execution scope. Direct chat attachments and slash commands are unavailable."
    )


class ResidentCancel(ResidentModel):
    request_id: RequestId = Field(description="Stable identity of this cancellation intent.")
    turn_id: str = Field(min_length=1, max_length=200, description="Exact turn to cancel.")


class ResidentTurn(ResidentModel):
    turn_id: str
    revision: int = Field(
        ge=1,
        description="Monotonic version of this durable turn, shared by REST and live snapshots.",
    )
    request_id: str
    user_message_id: str
    assistant_message_id: str = Field(
        description="Stable receipt display ID; distinct from the provider's canonical history ID."
    )
    prompt: str
    reply: str
    status: TurnStatus
    created_at: datetime
    updated_at: datetime
    error_code: str | None
    error_message: str | None
    authorization: ResidentToolInput = Field(
        default_factory=ResidentToolInput,
        description="Additional host capabilities explicitly selected for this original send. Default task delegation is server-managed; retries cannot change the frozen scope.",
    )


class ResidentSnapshot(ResidentModel):
    agent_id: str
    thread_id: str
    revision: int
    active_turn_id: str | None
    has_earlier: bool
    turns: list[ResidentTurn]


def _task_context(row: ResidentRequestRow) -> ResidentTaskContext | None:
    configuration = json.loads(row.configuration_json)
    if "resident_task_context" in configuration:
        return ResidentTaskContext.model_validate(configuration["resident_task_context"])
    # Admitted turns keep their original scope, including turns queued before
    # natural-language delegation was introduced.
    return ResidentToolInput.model_validate_json(row.input_json).task_context


def turn_view(row: ResidentRequestRow, locale: str) -> ResidentTurn:
    code = _known_error_code(row.error_code) if row.error_code else None
    return ResidentTurn(
        turn_id=row.turn_id,
        revision=row.revision,
        request_id=row.request_id,
        user_message_id=f"resident:{row.turn_id}:user",
        assistant_message_id=f"resident:{row.turn_id}:assistant",
        prompt=row.prompt,
        reply=row.reply,
        status=cast(TurnStatus, row.status),
        created_at=datetime.fromtimestamp(row.created_at / 1000, UTC),
        updated_at=datetime.fromtimestamp(row.updated_at / 1000, UTC),
        error_code=code,
        error_message=error_message(code, locale) if code else None,
        authorization=ResidentToolInput.model_validate_json(row.input_json),
    )


def _known_error_code(value: str) -> str:
    try:
        return ErrorCode(value).value
    except ValueError:
        return ErrorCode.INTERNAL_ERROR.value


def binding_view(services: SharedServices, owner_id: int) -> ResidentBinding:
    row = get_resident_identity(services, owner_id)
    agent = services.agent_repo.get(row.agent_id) if row else None
    return ResidentBinding(
        status=cast(BindingStatus, row.status) if row else "not_created",
        agent_id=row.agent_id if row else None,
        thread_id=row.thread_id if row else None,
        name=agent.name if agent else None,
        timezone=services.config.default_timezone,
        error_code=(row.error_code or None) if row else None,
    )


class ResidentConversationRuntime:
    def __init__(
        self,
        services: SharedServices,
        registry: AgentManager,
        gateway: Gateway,
        *,
        history_archive: HistoryArchive | None = None,
    ) -> None:
        from octop.infra.agents.resident_notifications import (
            ResidentNotificationDelivery,  # noqa: PLC0415
        )

        self.services = services
        self.registry = registry
        self.gateway = gateway
        self.worker_id = new_ulid()
        self._wake = asyncio.Event()
        self._updates = CommittedUpdates[str]()
        self._loop_task: asyncio.Task[None] | None = None
        self._active: dict[str, asyncio.Task[None]] = {}
        self._active_threads: set[str] = set()
        self._thread_locks: dict[str, asyncio.Lock] = {}
        self._followup_locks: dict[str, asyncio.Lock] = {}
        self._notification_retry: dict[str, float] = {}
        self._last_followup_poll = 0.0
        self._report_retry: dict[str, float] = {}
        self.report_delivery = ResidentReportDelivery(services, registry, history_archive)
        self.notification_delivery = ResidentNotificationDelivery(
            services, registry, history_archive
        )
        self.report_delivery.task_backed = getattr(gateway, "_task_worker", None) is not None
        self.notification_delivery.task_backed = self.report_delivery.task_backed
        self._closing = False
        self._process_lock = ResidentWorkerLock(services.paths.root / "resident-worker.lock")

    async def start(self) -> None:
        if self._loop_task is not None:
            return
        acquire = asyncio.create_task(asyncio.to_thread(self._process_lock.acquire))
        try:
            await asyncio.shield(acquire)
        except asyncio.CancelledError:
            await acquire
            await asyncio.to_thread(self._process_lock.release)
            raise
        except RuntimeError as exc:
            raise OctopError.localized(ErrorCode.RESIDENT_WORKER_CONFLICT) from exc
        try:
            await asyncio.to_thread(
                self.services.resident_history_repo.backfill_days,
                self.services.config.default_timezone,
            )
            await _settled_io(self.services.resident_inbox_repo.initialize_existing)
        except BaseException:
            await asyncio.to_thread(self._process_lock.release)
            raise
        self._closing = False
        self._loop_task = asyncio.create_task(self._loop(), name="resident-conversation-worker")

    def wake(self) -> None:
        self._wake.set()

    def watch_updates(self, thread_id: str, wake: Callable[[], None]) -> Callable[[], None]:
        return self._updates.watch(thread_id, wake)

    def replace_services(self, services: SharedServices) -> None:
        if self._active:
            raise ValueError("Cannot replace persistence during a resident conversation")
        self.services = services
        self.report_delivery.services = services
        self.notification_delivery.services = services

    async def close(self) -> None:
        self._closing = True
        if self._loop_task:
            self._loop_task.cancel()
            await asyncio.gather(self._loop_task, return_exceptions=True)
            self._loop_task = None
        for task in list(self._active.values()):
            if task.cancelling() == 0:
                task.cancel()
        await asyncio.gather(*self._active.values(), return_exceptions=True)
        self._active.clear()
        self._active_threads.clear()
        await asyncio.to_thread(self._process_lock.release)

    def _require(self, owner_id: int, agent_id: str, thread_id: str) -> ResidentConversationRow:
        row = self.services.resident_conversation_repo.get_by_agent(agent_id)
        if row is None or row.owner_id != owner_id or row.thread_id != thread_id:
            raise OctopError.localized(ErrorCode.NOT_FOUND)
        if row.status != "ready":
            raise OctopError.localized(ErrorCode.AGENT_NOT_RUNNING)
        agent = self.services.agent_repo.get(agent_id)
        thread = self.services.thread_repo.get(thread_id)
        if (
            agent is None
            or agent.user_id != owner_id
            or agent.is_shared
            or agent.kind != "expert"
            or thread is None
            or thread.agent_id != agent_id
            or thread.user_id != owner_id
        ):
            raise OctopError.localized(ErrorCode.NOT_FOUND)
        if not agent.enabled:
            raise OctopError.localized(ErrorCode.AGENT_NOT_RUNNING)
        return row

    async def snapshot(
        self, owner_id: int, agent_id: str, thread_id: str, limit: int, locale: str
    ) -> ResidentSnapshot:
        await asyncio.to_thread(self._require, owner_id, agent_id, thread_id)
        revision, rows = await asyncio.to_thread(
            self.services.resident_request_repo.snapshot, owner_id, thread_id, limit + 1
        )
        return ResidentSnapshot(
            agent_id=agent_id,
            thread_id=thread_id,
            revision=revision,
            active_turn_id=next((row.turn_id for row in rows if row.status in ACTIVE), None),
            has_earlier=len(rows) > limit,
            turns=[turn_view(row, locale) for row in rows[-limit:]],
        )

    async def request(
        self, owner_id: int, agent_id: str, thread_id: str, request_id: str
    ) -> ResidentRequestRow:
        await asyncio.to_thread(self._require, owner_id, agent_id, thread_id)
        row = await asyncio.to_thread(self.services.resident_request_repo.get, owner_id, request_id)
        if row is None or row.thread_id != thread_id:
            raise OctopError.localized(ErrorCode.NOT_FOUND)
        return row

    async def send(
        self, owner_id: int, agent_id: str, thread_id: str, body: ResidentMessage
    ) -> ResidentRequestRow:
        binding = await asyncio.to_thread(self._require, owner_id, agent_id, thread_id)
        if not body.prompt.strip() or body.prompt.lstrip().startswith("/"):
            raise OctopError.localized(ErrorCode.RESIDENT_TEXT_ONLY)
        # Freeze only explicit turn selections. Replays return the original receipt
        # even when a formerly selected knowledge base has since been revoked.
        authorization = ResidentToolInput(
            read_tools=body.read_tools,
            task_context=body.task_context,
            knowledge_base_ids=body.knowledge_base_ids,
        )
        input_json = json.dumps(
            authorization.model_dump(mode="json", exclude_defaults=True),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        previous = await asyncio.to_thread(
            self.services.resident_request_repo.get, owner_id, body.request_id
        )
        if previous is not None:
            if (
                previous.thread_id != thread_id
                or previous.prompt != body.prompt
                or previous.input_json != input_json
            ):
                raise OctopError.localized(ErrorCode.RESIDENT_REQUEST_CONFLICT)
            return previous
        if body.knowledge_base_ids:
            await asyncio.to_thread(
                self.registry.validate_knowledge_base_ids, owner_id, body.knowledge_base_ids
            )
        configuration = await asyncio.to_thread(self._configuration, owner_id, agent_id)
        configuration["resident_input"] = json.loads(input_json)
        if binding.role == "primary":
            task_context = body.task_context or default_task_context(
                body.request_id, body.prompt, configuration["model_ref"]
            )
            configuration["resident_task_context"] = task_context.model_dump(mode="json")
        accepted_at = now_ms()
        timezone = self.services.config.default_timezone
        try:
            row, _ = await asyncio.to_thread(
                self.services.resident_request_repo.create,
                owner_id,
                thread_id,
                body.request_id,
                new_ulid(),
                body.prompt,
                accepted_at,
                configuration_json=json.dumps(configuration),
                day_key=datetime.fromtimestamp(accepted_at / 1000, ZoneInfo(timezone))
                .date()
                .isoformat(),
                timezone=timezone,
                input_json=input_json,
            )
        except ResidentRequestConflict as exc:
            raise OctopError.localized(ErrorCode.RESIDENT_REQUEST_CONFLICT) from exc
        except ResidentRequestArchived as exc:
            raise OctopError.localized(ErrorCode.RESIDENT_PROFILE_ARCHIVED) from exc
        except ResidentRequestBusy as exc:
            raise OctopError.localized(ErrorCode.AGENT_BUSY) from exc
        self._updates.notify(thread_id)
        self.wake()
        return row

    def _configuration(self, owner_id: int, agent_id: str) -> dict[str, Any]:
        row = self.services.agent_repo.get(agent_id)
        assert row is not None
        config = self.registry.get_config(agent_id)
        model = self.registry.providers.resolve_explicit_default_model(row, config)
        user = self.services.user_repo.get(owner_id)
        preferred = get_preferred_model_from_json(user.preferences_json if user else None)
        if not model and preferred and self.registry.providers.is_model_ref_usable(preferred):
            model = preferred
        model = model or self.registry.resolve_fallback_model_ref()
        snapshot = self.services.resident_profile_repo.configuration_snapshot(owner_id, agent_id)
        binding = self.services.resident_conversation_repo.get_by_agent(agent_id)
        if binding is not None and binding.role == "primary":
            # Freeze the current steward policy for new turns without rewriting the
            # private identity, historical snapshots or already admitted recovery work.
            snapshot["system_prompt"] = tr(
                "agents.resident.system_prompt", user.locale if user else "en"
            )
        return {"model_ref": model, **snapshot}

    async def cancel(
        self, owner_id: int, agent_id: str, thread_id: str, body: ResidentCancel
    ) -> ResidentRequestRow:
        await asyncio.to_thread(self._require, owner_id, agent_id, thread_id)
        try:
            row = await asyncio.to_thread(
                self.services.resident_request_repo.cancel,
                owner_id,
                thread_id,
                body.request_id,
                body.turn_id,
                now_ms(),
            )
        except ResidentRequestConflict as exc:
            raise OctopError.localized(ErrorCode.RESIDENT_REQUEST_CONFLICT) from exc
        if row is None:
            raise OctopError.localized(ErrorCode.NOT_FOUND)
        if getattr(self.gateway, "_task_worker", None) is not None:
            from octop.infra.projects.resident_tasks import cancel

            await asyncio.to_thread(cancel, self.services, owner_id, thread_id, row.turn_id)
        # A repeated cancellation for an old turn can never signal the next turn.
        task = self._active.get(row.turn_id)
        if (
            row.status == "running"
            and row.cancel_requested
            and task is not None
            and task.cancelling() == 0
        ):
            if getattr(self.gateway, "_task_worker", None) is None:
                with resident_execution_scope(owner_id, thread_id):
                    self.registry.cancel_stream(agent_id, thread_id)
            task.cancel()
        self._updates.notify(thread_id)
        self.wake()
        return row

    async def _loop(self) -> None:
        while not self._closing:
            self._wake.clear()
            try:
                if time.monotonic() - self._last_followup_poll >= 1:
                    from octop.infra.agents.resident_followups import (
                        poll_followups,  # noqa: PLC0415
                    )

                    await _settled_io(poll_followups, self.services)
                    self._last_followup_poll = time.monotonic()
                await _settled_io(self.services.resident_request_repo.interrupt_active, now_ms())
                reports = await asyncio.to_thread(
                    self.services.resident_task_report_repo.pending, 100
                )
                notices = await asyncio.to_thread(
                    self.services.resident_notification_repo.pending, 100
                )
                blocked_threads = {report.thread_id for report in reports} | {
                    n.thread_id for n in notices
                }
                for notice in notices:
                    if (
                        notice.thread_id in self._active_threads
                        or time.monotonic()
                        < self._notification_retry.get(notice.notification_id, 0)
                    ):
                        continue
                    key = f"notice:{notice.notification_id}"
                    task = asyncio.create_task(self.deliver_notification(notice), name=key)
                    self._active[key] = task
                    self._active_threads.add(notice.thread_id)
                    task.add_done_callback(partial(self._finished, key, notice.thread_id))
                for report in reports:
                    if (
                        report.thread_id in self._active_threads
                        or time.monotonic() < self._report_retry.get(report.report_id, 0)
                    ):
                        continue
                    key = f"report:{report.report_id}"
                    task = asyncio.create_task(self.deliver_report(report), name=key)
                    self._active[key] = task
                    self._active_threads.add(report.thread_id)
                    task.add_done_callback(partial(self._finished, key, report.thread_id))
                pending = await asyncio.to_thread(
                    self.services.resident_request_repo.pending, now_ms(), 8
                )
                for row in pending:
                    if (
                        row.turn_id in self._active
                        or row.thread_id in self._active_threads
                        or row.thread_id in blocked_threads
                    ):
                        continue
                    task = asyncio.create_task(self._execute(row), name=f"resident:{row.turn_id}")
                    self._active[row.turn_id] = task
                    self._active_threads.add(row.thread_id)
                    task.add_done_callback(partial(self._finished, row.turn_id, row.thread_id))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Resident conversation polling failed")
            with suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=0.25)

    async def _execute(self, queued: ResidentRequestRow) -> None:
        async with self._thread_locks.setdefault(queued.thread_id, asyncio.Lock()):
            # A task may finish between the poll and this user turn. Consume its
            # result before admitting any new model execution on the same graph.
            while reports := await asyncio.to_thread(
                self.services.resident_task_report_repo.pending_for_thread,
                queued.owner_id,
                queued.thread_id,
                100,
            ):
                for report in reports:
                    if not await self._deliver_report_locked(report):
                        return
            while notices := await asyncio.to_thread(
                self.services.resident_notification_repo.pending,
                100,
                owner=queued.owner_id,
                thread=queued.thread_id,
            ):
                for notice in notices:
                    if not await self._deliver_notification_locked(notice):
                        return
            await self._execute_locked(queued)

    @asynccontextmanager
    async def thread_scope(
        self, thread_id: str, *, require_idle: bool = False
    ) -> AsyncIterator[None]:
        lock = self._thread_locks.setdefault(thread_id, asyncio.Lock())
        if require_idle and lock.locked():
            raise OctopError.localized(ErrorCode.AGENT_BUSY)
        async with lock:
            yield

    @asynccontextmanager
    async def followup_scope(self, followup_id: str) -> AsyncIterator[None]:
        async with self._followup_locks.setdefault(followup_id, asyncio.Lock()):
            yield

    async def deliver_notification(self, row: ResidentNotificationRow) -> None:
        async with self.thread_scope(row.thread_id):
            await self._deliver_notification_locked(row)

    async def _deliver_notification_locked(self, row: ResidentNotificationRow) -> bool:
        async with self.followup_scope(row.followup_id):
            try:
                await self.notification_delivery.deliver(row)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                code = (
                    exc.code.value
                    if isinstance(exc, OctopError)
                    else ErrorCode.INTERNAL_ERROR.value
                )
                logger.warning(
                    "Resident notification deferred id=%s code=%s", row.notification_id, code
                )
                await asyncio.to_thread(
                    self.services.resident_notification_repo.record_error,
                    row.notification_id,
                    code,
                    now_ms(),
                )
                self._notification_retry[row.notification_id] = time.monotonic() + 2
                return False
            self._notification_retry.pop(row.notification_id, None)
            return True

    async def deliver_report(self, report: ResidentTaskReportRow) -> None:
        async with self._thread_locks.setdefault(report.thread_id, asyncio.Lock()):
            await self._deliver_report_locked(report)

    async def _deliver_report_locked(self, report: ResidentTaskReportRow) -> bool:
        try:
            await self.report_delivery.deliver(report)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            code = exc.code.value if isinstance(exc, OctopError) else ErrorCode.INTERNAL_ERROR.value
            logger.warning(
                "Resident result delivery deferred report=%s code=%s", report.report_id, code
            )
            await asyncio.to_thread(
                self.services.resident_task_report_repo.record_delivery_error,
                report.report_id,
                code,
                now_ms(),
            )
            self._report_retry[report.report_id] = time.monotonic() + 2
            return False
        self._report_retry.pop(report.report_id, None)
        return True

    async def _execute_locked(self, queued: ResidentRequestRow) -> None:
        repo = self.services.resident_request_repo
        # A cancelled coroutine cannot stop a DB operation already running in a
        # thread. Settle the claim before deciding whether a terminal write is due.
        claim = asyncio.create_task(
            asyncio.to_thread(
                repo.mark_running, queued.turn_id, self.worker_id, now_ms(), now_ms() + LEASE_MS
            )
        )
        claimed = False
        reply = queued.reply
        status = "completed"
        code: str | None = None
        heartbeat: asyncio.Task[None] | None = None
        try:
            claimed = await asyncio.shield(claim)
            if not claimed:
                return
            self._updates.notify(queued.thread_id)
            parent = asyncio.current_task()
            assert parent is not None
            heartbeat = asyncio.create_task(
                self._heartbeat(queued, parent), name=f"resident-lease:{queued.turn_id}"
            )
            binding = await asyncio.to_thread(
                self.services.resident_conversation_repo.get_by_thread, queued.thread_id
            )
            if binding is None or binding.owner_id != queued.owner_id:
                raise OctopError.localized(ErrorCode.NOT_FOUND)
            config = json.loads(queued.configuration_json)
            authorization = ResidentToolInput.model_validate_json(queued.input_json)
            model = config.get("model_ref")
            if not model or not await asyncio.to_thread(
                self.registry.providers.is_model_ref_usable, model
            ):
                raise OctopError.localized(ErrorCode.RESIDENT_MODEL_UNAVAILABLE)
            if getattr(self.gateway, "_task_worker", None) is not None:
                reply, status, code = await self._execute_task(queued)
                return
            try:
                harness = self.registry.get_agent(binding.agent_id)
            except OctopError:
                await self.registry.start(binding.agent_id)
                harness = self.registry.get_agent(binding.agent_id)
            policy = await asyncio.to_thread(
                self.services.resident_memory_policy_repo.get, queued.owner_id, queued.thread_id
            )
            desired_capture = bool(policy and policy.capture_enabled)
            if bool(getattr(harness.config, "memory_capture_enabled", False)) != desired_capture:
                await self.registry.reload(binding.agent_id)
            message = self._message(binding, queued, model)
            from octop.infra.agents.resident_memory import resident_memory_context  # noqa: PLC0415

            memory = await asyncio.to_thread(
                resident_memory_context,
                self.services,
                queued.owner_id,
                binding.agent_id,
                queued.thread_id,
            )
            with (
                resident_execution_scope(queued.owner_id, queued.thread_id),
                resident_memory_scope(memory),
                resident_prompt_scope(config.get("system_prompt")),
                resident_tools_scope(
                    services=self.services,
                    owner_id=queued.owner_id,
                    agent_id=binding.agent_id,
                    thread_id=queued.thread_id,
                    turn_id=queued.turn_id,
                    read_tools=authorization.read_tools,
                    task_context=_task_context(queued),
                    knowledge_base_ids=authorization.knowledge_base_ids,
                ),
            ):
                chunks = cast(
                    AsyncGenerator[dict[str, Any], None],
                    self.gateway.processor.iter_turn_chunks(message),
                )
                async with aclosing(chunks):
                    async for chunk in chunks:
                        kind = chunk.get("type")
                        if kind == "token":
                            content = chunk.get("content")
                            if isinstance(content, str):
                                reply += content
                                if not await asyncio.to_thread(
                                    repo.update_reply,
                                    queued.turn_id,
                                    self.worker_id,
                                    reply,
                                    now_ms(),
                                ):
                                    raise OctopError.localized(ErrorCode.RESIDENT_INTERRUPTED)
                                self._updates.notify(queued.thread_id)
                        elif kind == "error":
                            status, code = (
                                "failed",
                                _known_error_code(str(chunk.get("error_code") or "INTERNAL_ERROR")),
                            )
                        elif kind == "hitl_required":
                            status, code = "interrupted", ErrorCode.RESIDENT_TEXT_ONLY.value
                        elif kind == "cancelled":
                            status = "cancelled"
        except asyncio.CancelledError:
            claimed = await claim
            if not claimed:
                return
            if getattr(self.gateway, "_task_worker", None) is not None:
                from octop.infra.projects.resident_tasks import cancel

                await asyncio.to_thread(
                    cancel, self.services, queued.owner_id, queued.thread_id, queued.turn_id
                )
            current = await asyncio.to_thread(repo.get, queued.owner_id, queued.request_id)
            status = "cancelled" if current and current.cancel_requested else "interrupted"
            code = ErrorCode.RESIDENT_INTERRUPTED.value if status == "interrupted" else None
        except OctopError as exc:
            status, code = "failed", exc.code.value
        except Exception:
            logger.exception("Resident turn failed turn=%s", queued.turn_id)
            status, code = "failed", ErrorCode.INTERNAL_ERROR.value
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            if claimed:
                if getattr(self.gateway, "_task_worker", None) is not None:
                    from octop.infra.projects.resident_tasks import cancel

                    if status != "completed":
                        await _settled_io(
                            cancel, self.services, queued.owner_id, queued.thread_id, queued.turn_id
                        )
                    native = await _settled_io(
                        self.services.project_task_turn_repo.get, queued.turn_id
                    )
                    receipt = await _settled_io(repo.get, queued.owner_id, queued.request_id)
                    # A cancellation may interrupt the observing coroutine before
                    # its tuple assignment. Canonical persisted output wins.
                    reply = (
                        native.reply
                        if native and native.reply
                        else receipt.reply
                        if receipt
                        else reply
                    )
                try:
                    await _settled_io(
                        repo.finish,
                        queued.turn_id,
                        self.worker_id,
                        status,
                        reply,
                        code or "",
                        now_ms(),
                    )
                finally:
                    self._updates.notify(queued.thread_id)
            self.wake()

    async def _execute_task(self, queued: ResidentRequestRow) -> tuple[str, str, str | None]:
        from octop.infra.projects import resident_tasks

        while True:
            try:
                identifier = await resident_tasks.admit(self.services, queued)
                break
            except OctopError as exc:
                if exc.code != ErrorCode.TASK_BUSY:
                    raise
                # The previous exact turn may still be settling its Task
                # cancellation after the resident receipt became terminal.
                await asyncio.sleep(0.05)
        worker = self.gateway._task_worker
        assert worker is not None
        stop = worker.watch_updates(identifier, lambda: self._updates.notify(queued.thread_id))
        worker.wake()
        try:
            while True:
                current = await asyncio.to_thread(
                    self.services.project_task_turn_repo.get, queued.turn_id
                )
                if current is None:
                    raise OctopError.localized(ErrorCode.RESIDENT_INTERRUPTED)
                if not await asyncio.to_thread(
                    self.services.resident_request_repo.update_reply,
                    queued.turn_id,
                    self.worker_id,
                    current.reply,
                    now_ms(),
                ):
                    raise OctopError.localized(ErrorCode.RESIDENT_INTERRUPTED)
                self._updates.notify(queued.thread_id)
                if current.status in {"completed", "failed", "cancelled"}:
                    return current.reply, current.status, current.error_code or None
                if current.wait_kind or current.pause_json:
                    return current.reply, "interrupted", ErrorCode.RESIDENT_INTERRUPTED.value
                await asyncio.sleep(0.1)
        finally:
            stop()

    def _finished(self, turn_id: str, thread_id: str, task: asyncio.Task[None]) -> None:
        self._active.pop(turn_id, None)
        self._active_threads.discard(thread_id)
        self.wake()
        if not task.cancelled() and (exc := task.exception()) is not None:
            logger.error("Resident worker failed turn=%s", turn_id, exc_info=exc)

    async def _heartbeat(self, row: ResidentRequestRow, parent: asyncio.Task[None]) -> None:
        try:
            while True:
                await asyncio.sleep(5)
                current = await asyncio.to_thread(
                    self.services.resident_request_repo.get, row.owner_id, row.request_id
                )
                if current is None or current.cancel_requested:
                    if parent.cancelling() == 0:
                        parent.cancel()
                    return
                renewed = await asyncio.to_thread(
                    self.services.resident_request_repo.renew,
                    row.turn_id,
                    self.worker_id,
                    now_ms(),
                    now_ms() + LEASE_MS,
                )
                if not renewed:
                    if parent.cancelling() == 0:
                        parent.cancel()
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Resident lease maintenance failed turn=%s", row.turn_id)
            if parent.cancelling() == 0:
                parent.cancel()

    @staticmethod
    def _message(
        binding: ResidentConversationRow, row: ResidentRequestRow, model: str
    ) -> InboundMessage:
        session_key = f"{binding.agent_id}:dashboard:{row.owner_id}:resident"
        authorization = ResidentToolInput.model_validate_json(row.input_json)
        has_host_tools = bool(
            authorization.read_tools or _task_context(row) or authorization.knowledge_base_ids
        )
        return InboundMessage(
            channel_id=WS_CHANNEL_ID,
            channel_type=ThreadRegistry.CHANNEL_DASHBOARD,
            tenant_id=binding.agent_id,
            channel_subject=ChannelSubject(subject_id=str(row.owner_id)),
            channel_session_id=session_key,
            content=[TextContent(text=row.prompt)],
            metadata={
                "session_key": session_key,
                "thread_id": row.thread_id,
                "mcp_servers": [],
                "skills": [],
                "knowledge_base_ids": [],
                "conversation_mode": "craft" if has_host_tools else "ask",
                "model": model,
                "_resident_request_id": row.request_id,
                "_resident_user_message_id": f"resident:{row.turn_id}:user",
            },
        )
