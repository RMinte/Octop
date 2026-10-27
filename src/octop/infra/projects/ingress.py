"""One durable admission path for transport inputs; execution belongs to Tasks."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal

from octop.infra.db.repos.unified_sources import (
    UnifiedInbox,
    UnifiedSourceInboxRepo,
    UnifiedSourceRouteRepo,
)
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects import source_routes, task_submission, tasks
from octop.infra.projects.deletion_state import assert_project_writable, assert_task_writable
from octop.infra.projects.models import ProjectModel
from octop.infra.projects.settings import snapshot_presentation

logger = logging.getLogger(__name__)


class SourceEnvelope(ProjectModel):
    source_kind: str
    source_instance: str
    source_key: str
    event_id: str
    content_digest: str = ""
    legacy_thread_id: str | None = None
    legacy_agent_id: str | None = None
    reply_target: dict[str, Any] = {}
    operation: Literal["message", "respond", "cancel", "reset", "notification"] = "message"
    notification_text: str | None = None
    authority: dict[str, Any] = {}
    source_settings: dict[str, Any] = {}
    display_only: bool = False
    turn_id: str | None = None
    interaction_id: str | None = None
    interaction_response: dict[str, Any] | None = None


@dataclass(frozen=True)
class SubmissionReceipt:
    inbox_id: str
    route_id: str
    event_id: str
    status: str
    task_id: str | None
    turn_id: str | None
    replayed: bool = False
    error_code: str = ""


def _receipt(row: UnifiedInbox, replayed: bool = False) -> SubmissionReceipt:
    return SubmissionReceipt(
        row.inbox_id,
        row.route_id,
        row.event_id,
        row.status,
        row.task_id,
        row.turn_id,
        replayed,
        row.error_code,
    )


def receive(
    services: SharedServices, owner_id: int, source: SourceEnvelope, message: tasks.TaskCreate
) -> SubmissionReceipt:
    if not source.event_id or not source.source_key or not source.source_instance:
        raise OctopError.localized(ErrorCode.TASK_REQUEST_CONFLICT)
    route = source_routes.resolve(
        services,
        owner_id,
        source_kind=source.source_kind,
        source_instance=source.source_instance,
        source_key=source.source_key,
        legacy_thread_id=source.legacy_thread_id,
        legacy_agent_id=source.legacy_agent_id,
    )
    identity = hashlib.sha256((route.route_id + ":" + source.event_id).encode()).hexdigest()
    # The transport identity owns the request id. An untrusted sender cannot
    # replay another source's accepted Task by choosing a user-visible string.
    message = message.model_copy(update={"request_id": "source_" + identity})
    fingerprint = hashlib.sha256(
        json.dumps(
            {"source": source.model_dump(mode="json"), "message": message.model_dump(mode="json")},
            sort_keys=True,
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    with services.db.transaction() as conn:
        if not services.project_task_repo.lock_owner(conn, owner_id):
            raise OctopError.localized(ErrorCode.FORBIDDEN)
        user = services.user_repo.get(owner_id)
        if user is None or user.disabled:
            raise OctopError.localized(ErrorCode.FORBIDDEN)
        previous = UnifiedSourceInboxRepo.get(conn, owner_id, "input_" + identity)
        if previous is not None:
            if previous.status == "target_deleted":
                raise OctopError.localized(ErrorCode.NOT_FOUND)
            if previous.fingerprint != fingerprint:
                raise OctopError.localized(ErrorCode.TASK_REQUEST_CONFLICT)
            return _receipt(previous, True)
        current = UnifiedSourceRouteRepo.get(conn, owner_id, route.route_id)
        assert current is not None
        if current.state == "blocked":
            raise OctopError.localized(ErrorCode.FORBIDDEN)
        if current.task_id is None and current.state == "task" and current.revision > 1:
            raise OctopError.localized(ErrorCode.NOT_FOUND)
        if current.task_id:
            task = services.project_task_repo.get_in_transaction(conn, current.task_id, owner_id)
            if task is None:
                raise OctopError.localized(ErrorCode.NOT_FOUND)
            if source.operation != "cancel":
                assert_project_writable(services, conn, owner_id, task.project_id)
            if message.expected_configuration_revision is None:
                message = message.model_copy(
                    update={"expected_configuration_revision": task.configuration_revision}
                )
        elif source.operation != "cancel":
            assert_project_writable(services, conn, owner_id, message.project_id)
        payload = json.dumps(
            {"source": source.model_dump(mode="json"), "message": message.model_dump(mode="json")},
            ensure_ascii=False,
        )
        row = UnifiedSourceInboxRepo.receive(
            conn,
            inbox_id="input_" + identity,
            route_id=route.route_id,
            owner_id=owner_id,
            event_id=source.event_id,
            fingerprint=fingerprint,
            payload_json=payload,
            now=source_routes.now_ms(),
        )
    return _receipt(row)


def get_receipt(services: SharedServices, owner_id: int, inbox_id: str) -> SubmissionReceipt:
    with services.db.connect() as conn:
        row = UnifiedSourceInboxRepo.get(conn, owner_id, inbox_id)
    if row is None or row.status == "target_deleted":
        raise OctopError.localized(ErrorCode.NOT_FOUND)
    return _receipt(row)


def lookup(
    services: SharedServices, owner_id: int, source: SourceEnvelope
) -> SubmissionReceipt | None:
    route = source_routes.resolve(
        services,
        owner_id,
        source_kind=source.source_kind,
        source_instance=source.source_instance,
        source_key=source.source_key,
        legacy_thread_id=source.legacy_thread_id,
        legacy_agent_id=source.legacy_agent_id,
    )
    if route.state in {"detached", "deleted", "target_deleted"}:
        raise OctopError.localized(ErrorCode.NOT_FOUND)
    identity = hashlib.sha256((route.route_id + ":" + source.event_id).encode()).hexdigest()
    with services.db.connect() as conn:
        row = UnifiedSourceInboxRepo.get(conn, owner_id, "input_" + identity)
    if row is None:
        return None
    if row.status == "target_deleted":
        raise OctopError.localized(ErrorCode.NOT_FOUND)
    payload = json.loads(row.payload_json)
    if not isinstance(payload, dict) or not isinstance(payload.get("source"), dict):
        raise OctopError.localized(ErrorCode.NOT_FOUND)
    accepted = payload["source"]
    if source.content_digest and accepted.get("content_digest") != source.content_digest:
        raise OctopError.localized(ErrorCode.TASK_REQUEST_CONFLICT)
    return _receipt(row, True)


async def admit(services: SharedServices, owner_id: int, inbox_id: str) -> SubmissionReceipt:
    def prepare() -> tuple[UnifiedInbox, Any]:
        with services.db.connect() as conn:
            row = UnifiedSourceInboxRepo.get(conn, owner_id, inbox_id)
            route = UnifiedSourceRouteRepo.get(conn, owner_id, row.route_id) if row else None
        if row is None or route is None:
            raise OctopError.localized(ErrorCode.NOT_FOUND)
        return row, route

    row, route = await asyncio.to_thread(prepare)
    if row.status == "target_deleted" or route.state in {"detached", "deleted", "target_deleted"}:
        raise OctopError.localized(ErrorCode.NOT_FOUND)
    if row.status != "received":
        return _receipt(row, True)
    source_routes.require_writable(route)
    payload = json.loads(row.payload_json)
    message = tasks.TaskCreate.model_validate(payload["message"])
    source = SourceEnvelope.model_validate(payload["source"])
    if source.operation == "notification":
        return await asyncio.to_thread(
            record_notification,
            services,
            owner_id,
            source,
            source.notification_text or "",
            title=message.prompt,
            expert_id=message.selected_expert_id,
            project_id=message.project_id,
        )
    if source.operation != "message":

        def control() -> None:
            from octop.infra.projects import interactions
            from octop.infra.projects.interaction_models import InteractionResponse

            if source.operation == "reset":
                with services.db.transaction() as conn:
                    latest = UnifiedSourceInboxRepo.get(conn, owner_id, inbox_id)
                    if latest is None or latest.status != "received":
                        return
                    if route.task_id is not None:
                        assert_task_writable(services, conn, owner_id, route.task_id)
                    source_routes.reset_in_transaction(
                        services, conn, owner_id, route.route_id, message.request_id
                    )
                    UnifiedSourceInboxRepo.admit(conn, row, None, None, source_routes.now_ms())
                return

            if route.task_id is None:
                raise OctopError.localized(ErrorCode.NOT_FOUND)
            task_id = route.task_id
            if source.operation == "respond":
                assert task_id is not None
                if (
                    not source.turn_id
                    or not source.interaction_id
                    or source.interaction_response is None
                ):
                    raise OctopError.localized(ErrorCode.TASK_INTERACTION_CONFLICT)
                response = InteractionResponse.model_validate(
                    {**source.interaction_response, "request_id": message.request_id}
                )
                if (
                    source.source_kind == "im"
                    and response.response.kind == "approval"
                    and response.response.decision == "allow_once"
                ):
                    raise OctopError.localized(ErrorCode.FORBIDDEN)
                interactions.respond(
                    services, owner_id, task_id, source.turn_id, source.interaction_id, response
                )
            elif source.operation == "cancel":
                assert task_id is not None
                if not source.turn_id:
                    raise OctopError.localized(ErrorCode.TASK_CONTROL_INVALID)
                tasks.cancel(services, owner_id, task_id, source.turn_id)
            with services.db.transaction() as conn:
                UnifiedSourceInboxRepo.admit(
                    conn,
                    row,
                    task_id,
                    source.turn_id,
                    source_routes.now_ms(),
                )

        await asyncio.to_thread(control)
        return await asyncio.to_thread(get_receipt, services, owner_id, inbox_id)

    def admission_hook(conn: Any, task_id: str | None, turn_id: str | None) -> None:
        current = UnifiedSourceRouteRepo.get(conn, owner_id, route.route_id)
        if current is None:
            raise OctopError.localized(ErrorCode.NOT_FOUND)
        source_routes.require_writable(current)
        if current.revision != route.revision or current.task_id != route.task_id:
            raise OctopError.localized(ErrorCode.TASK_BUSY, status=409)
        latest = UnifiedSourceInboxRepo.get(conn, owner_id, inbox_id)
        if latest is None:
            raise OctopError.localized(ErrorCode.NOT_FOUND)
        if latest.status == "received" and not UnifiedSourceInboxRepo.is_head(conn, latest):
            raise OctopError.localized(ErrorCode.TASK_BUSY, status=409)
        if current.task_id is None and current.revision > 1 and current.state != "draft":
            raise OctopError.localized(ErrorCode.NOT_FOUND)
        if task_id is not None and turn_id is not None:
            if current.task_id is None and not UnifiedSourceRouteRepo.transition(
                conn,
                owner_id,
                current.route_id,
                current.revision,
                current.state,
                "task",
                task_id=task_id,
                now=source_routes.now_ms(),
            ):
                raise OctopError.localized(ErrorCode.TASK_REVISION_CONFLICT)
            UnifiedSourceInboxRepo.admit(conn, row, task_id, turn_id, source_routes.now_ms())
            accepted = services.project_task_turn_repo.get_in_transaction(conn, turn_id)
            assert accepted is not None
            config = json.loads(accepted.configuration_json)
            config["source_origin"] = {
                "route_id": row.route_id,
                "inbox_id": row.inbox_id,
                "source": source.model_dump(mode="json"),
            }
            from octop.infra.projects.source_access import restrict_external_configuration

            config = restrict_external_configuration(config)
            conn.execute(
                "UPDATE project_task_turns SET configuration_json=? WHERE turn_id=?",
                (json.dumps(config, ensure_ascii=False), turn_id),
            )

    admitted_message = (
        tasks.TaskMessage.model_validate(
            message.model_dump(include=set(tasks.TaskMessage.model_fields))
        )
        if route.task_id is not None
        else message
    )
    await task_submission.submit(
        services,
        owner_id,
        admitted_message,
        task_id=route.task_id,
        project_id=message.project_id if route.task_id is None else None,
        admission_hook=admission_hook,
    )
    return await asyncio.to_thread(get_receipt, services, owner_id, inbox_id)


async def submit(
    services: SharedServices, owner_id: int, source: SourceEnvelope, message: tasks.TaskCreate
) -> SubmissionReceipt:
    receipt = await asyncio.to_thread(receive, services, owner_id, source, message)
    if receipt.status != "received":
        return receipt
    try:
        return await admit(services, owner_id, receipt.inbox_id)
    except OctopError as exc:
        if exc.code == ErrorCode.TASK_BUSY:
            return receipt
        raise


def record_notification(
    services: SharedServices,
    owner_id: int,
    source: SourceEnvelope,
    text: str,
    *,
    title: str = "",
    expert_id: str | None = None,
    project_id: str | None = None,
) -> SubmissionReceipt:
    """A text delivery is a canonical non-model turn, never an invented model call."""
    from octop.infra.db.repos.project_task_turns import ProjectTaskTurnRow
    from octop.infra.db.repos.project_tasks import ProjectTaskRow
    from octop.infra.projects.content import TaskContent
    from octop.infra.utils.ulid import new_ulid

    source = source.model_copy(update={"operation": "notification", "notification_text": text})
    title = title or text
    received = receive(
        services,
        owner_id,
        source,
        tasks.TaskCreate(
            request_id="notification-placeholder",
            prompt=title,
            selected_expert_id=expert_id,
            project_id=project_id,
        ),
    )
    with services.db.transaction() as conn:
        row = UnifiedSourceInboxRepo.get(conn, owner_id, received.inbox_id)
        assert row is not None
        if row.status != "received":
            return _receipt(row, True)
        if not services.project_task_repo.lock_owner(conn, owner_id):
            raise OctopError.localized(ErrorCode.FORBIDDEN)
        route = UnifiedSourceRouteRepo.get(conn, owner_id, row.route_id)
        assert route is not None
        source_routes.require_writable(route)
        if not source.display_only and not UnifiedSourceInboxRepo.is_head(conn, row):
            raise OctopError.localized(ErrorCode.TASK_BUSY, status=409)
        now = source_routes.now_ms()
        task = (
            services.project_task_repo.get_in_transaction(conn, route.task_id, owner_id)
            if route.task_id
            else None
        )
        assert_project_writable(
            services, conn, owner_id, task.project_id if task is not None else project_id
        )
        if task is None:
            if route.task_id or (route.state == "task" and route.revision > 1):
                raise OctopError.localized(ErrorCode.NOT_FOUND)
            if (
                project_id
                and services.project_repo.get_in_transaction(conn, project_id, owner_id) is None
            ):
                raise OctopError.localized(ErrorCode.NOT_FOUND)
            task = ProjectTaskRow(
                "task_" + new_ulid(),
                owner_id,
                project_id,
                title or text,
                json.loads(row.payload_json)["message"]["request_id"],
                None,
                now,
                now,
                selected_expert_id=expert_id,
            )
            services.project_task_repo.insert(conn, task)
            if not UnifiedSourceRouteRepo.transition(
                conn,
                owner_id,
                route.route_id,
                route.revision,
                route.state,
                "task",
                task_id=task.task_id,
                now=now,
            ):
                raise OctopError.localized(ErrorCode.TASK_REVISION_CONFLICT)
        if task.active_turn_id is not None and (not source.display_only or source.source_settings):
            raise OctopError.localized(ErrorCode.TASK_BUSY, status=409)
        if source.source_settings:
            from dataclasses import replace

            settings = source.source_settings
            if set(settings) - {"action_mode", "model_ref"} or (
                "action_mode" in settings
                and settings["action_mode"] not in {"ask", "plan", "craft"}
            ):
                raise OctopError.localized(ErrorCode.PROJECT_RESOURCE_INVALID)
            if "action_mode" in settings:
                updated = services.project_task_repo.update_configuration(
                    conn,
                    replace(task, action_mode=settings["action_mode"], updated_at=now),
                    task.configuration_revision,
                )
                if updated is None:
                    raise OctopError.localized(ErrorCode.TASK_REVISION_CONFLICT)
                task = updated
            metadata = json.loads(route.metadata_json)
            metadata["settings"] = {**metadata.get("settings", {}), **settings}
            conn.execute(
                "UPDATE unified_source_routes SET metadata_json=?,revision=revision+1,updated_at=? WHERE route_id=? AND owner_id=?",
                (json.dumps(metadata, ensure_ascii=False), now, route.route_id, owner_id),
            )
        turn_id = "turn_" + new_ulid()
        content = TaskContent(turn_id)
        content.append("text", text)
        content.end_text()
        record_active = task.active_turn_id is None
        if record_active:
            services.project_task_repo.activate(conn, task.task_id, turn_id, now)
        services.project_task_turn_repo.insert_imported(
            conn,
            ProjectTaskTurnRow(
                turn_id,
                task.task_id,
                json.loads(row.payload_json)["message"]["request_id"],
                title,
                "" + text,
                "completed",
                "",
                "",
                json.dumps(
                    {
                        "execution_kind": "notification",
                        "source": source.model_dump(mode="json"),
                        "action_timeline_enabled": False,
                        "presentation": snapshot_presentation(services, False).model_dump(),
                    }
                ),
                "",
                "",
                False,
                now,
                now,
                content_blocks_json=json.dumps(content.dump(), ensure_ascii=False),
                content_revision=content.revision,
                started_at=now,
                finished_at=now,
            ),
        )
        if record_active:
            services.project_task_repo.finish(conn, task.task_id, turn_id, now)
        UnifiedSourceInboxRepo.admit(conn, row, task.task_id, turn_id, now)
    return get_receipt(services, owner_id, received.inbox_id)


async def observe(
    services: SharedServices, owner_id: int, receipt: SubmissionReceipt, *, worker: Any = None
) -> AsyncIterator[tasks.TaskTurn]:
    """A disconnected observer does not cancel or re-execute an admitted turn."""
    wake = asyncio.Event()
    stop = None
    previous = ""
    try:
        while True:
            wake.clear()
            receipt = await asyncio.to_thread(get_receipt, services, owner_id, receipt.inbox_id)
            if receipt.status == "failed":
                raise OctopError.localized(ErrorCode.TASK_RESOURCE_UNAVAILABLE)
            if receipt.task_id and receipt.turn_id:
                if worker is not None and stop is None:
                    stop = worker.watch_updates(receipt.task_id, wake.set)
                    worker.wake()
                detail = await asyncio.to_thread(tasks.detail, services, owner_id, receipt.task_id)
                turn = next(
                    (turn for turn in detail.turns if turn.turn_id == receipt.turn_id), None
                )
                if turn is None:
                    raise OctopError.localized(ErrorCode.NOT_FOUND)
                encoded = turn.model_dump_json()
                if encoded != previous:
                    previous = encoded
                    yield turn
                if turn.status in {
                    "completed",
                    "failed",
                    "cancelled",
                    "waiting_approval",
                    "waiting_answer",
                    "waiting_client_tool",
                    "paused",
                    "interrupted",
                }:
                    return
            else:
                with contextlib.suppress(OctopError):
                    receipt = await admit(services, owner_id, receipt.inbox_id)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(wake.wait(), timeout=0.2)
    finally:
        if stop is not None:
            stop()


class TaskIngressWorker:
    def __init__(
        self, services: SharedServices, task_worker: Any = None, gateway: Any = None
    ) -> None:
        self.services, self.task_worker, self.gateway = services, task_worker, gateway
        self._task: asyncio.Task[None] | None = None
        self._recovery: asyncio.Task[None] | None = None
        self._cron_recovery: asyncio.Task[None] | None = None
        self._event = asyncio.Event()

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="task-source-inbox")
        self._recovery = asyncio.create_task(self._recover(), name="task-source-recovery")
        self._cron_recovery = asyncio.create_task(
            self._recover_cron(), name="cron-occurrence-recovery"
        )

    def wake(self) -> None:
        self._event.set()

    async def close(self) -> None:
        if self._cron_recovery is not None:
            self._cron_recovery.cancel()
            await asyncio.gather(self._cron_recovery, return_exceptions=True)
            self._cron_recovery = None
        if self._recovery is not None:
            self._recovery.cancel()
            await asyncio.gather(self._recovery, return_exceptions=True)
            self._recovery = None
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _recover(self) -> None:
        while True:
            if self.gateway is not None:
                from octop.infra.projects.deliveries import recover_im, recover_im_artifacts

                try:
                    await recover_im(self.services, self.gateway)
                    await recover_im_artifacts(self.services, self.gateway)
                except Exception:
                    logger.exception("Source outbound recovery failed")
            await asyncio.sleep(1)

    async def _recover_cron(self) -> None:
        while True:
            service = getattr(self.gateway, "_task_cron_delivery", None)
            if service is not None:
                from octop.infra.cron.task_delivery import recover

                try:
                    await recover(service)
                except Exception:
                    logger.exception("Cron occurrence recovery failed")
            await asyncio.sleep(1)

    async def _loop(self) -> None:
        while True:
            self._event.clear()

            def pending() -> list[UnifiedInbox]:
                with self.services.db.connect() as conn:
                    return UnifiedSourceInboxRepo.pending(conn)

            for row in await asyncio.to_thread(pending):
                try:
                    await admit(self.services, row.owner_id, row.inbox_id)
                    if self.task_worker is not None:
                        self.task_worker.wake()
                except OctopError as exc:
                    if exc.code != ErrorCode.TASK_BUSY:

                        def failed(
                            code: str = exc.code.value, identifier: str = row.inbox_id
                        ) -> None:
                            with self.services.db.transaction() as conn:
                                conn.execute(
                                    "UPDATE unified_source_inbox SET status='failed',error_code=?,updated_at=? WHERE inbox_id=? AND status='received'",
                                    (code, source_routes.now_ms(), identifier),
                                )

                        await asyncio.to_thread(failed)
                except Exception:
                    logger.exception("Source admission failed: inbox=%s", row.inbox_id)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._event.wait(), timeout=0.5)
