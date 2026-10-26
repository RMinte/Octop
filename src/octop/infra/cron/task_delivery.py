"""Scheduled occurrences submit canonical Tasks and deliver their saved output."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import asdict
from typing import Any

from octop_gateway.models import ChannelSubject

from octop.i18n import tr
from octop.infra.cron.delivery import CronDeliveryCommand
from octop.infra.db.repos.unified_sources import CronOccurrenceRepo
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.gateway.threads import ThreadRegistry
from octop.infra.projects import deliveries, ingress, source_routes, tasks
from octop.infra.utils.locale import resolve_user_locale
from octop.infra.utils.ulid import new_ulid


def _authorize(service: Any, command: CronDeliveryCommand) -> None:
    services = service._task_services
    row = services.cron_repo.get(command.cron_id)
    user = services.user_repo.get(command.user_id)
    automatic = command.event_key.startswith(("schedule:", "mail:"))
    if (
        row is None
        or row.user_id != command.user_id
        or user is None
        or user.disabled
        or (automatic and not row.enabled)
        or services.settings_repo.get(f"deleted_cron_binding:{command.cron_id}") is not None
    ):
        raise OctopError.localized(ErrorCode.FORBIDDEN)
    from octop.infra.cron.configuration import validate_task_selection

    validate_task_selection(
        services,
        command.user_id,
        conversation_policy=command.conversation_policy,
        target_task_id=command.target_task_id,
        project_id=command.project_id,
        selected_expert_id=command.selected_expert_id,
    )


def _update(
    service: Any, occurrence_id: str, status: str, receipt: ingress.SubmissionReceipt | None = None
) -> None:
    with service._task_services.db.transaction() as conn:
        CronOccurrenceRepo.update(
            conn,
            occurrence_id,
            status=status,
            task_id=receipt.task_id if receipt else None,
            turn_id=receipt.turn_id if receipt else None,
            now=source_routes.now_ms(),
        )


def reserve(service: Any, command: CronDeliveryCommand) -> Any:
    """Persist an accepted manual/scheduled event before returning to its caller."""
    _authorize(service, command)
    identifier = (
        "occurrence_"
        + hashlib.sha256((command.cron_id + ":" + command.event_key).encode()).hexdigest()
    )
    with service._task_services.db.transaction() as conn:
        return CronOccurrenceRepo.receive(
            conn,
            occurrence_id=identifier,
            cron_id=command.cron_id,
            owner_id=command.user_id,
            event_key=command.event_key,
            payload_json=json.dumps(asdict(command), ensure_ascii=False),
            now=source_routes.now_ms(),
        )


async def deliver(service: Any, command: CronDeliveryCommand) -> None:
    """An active run and recovery must observe one complete delivery outcome."""
    if not command.event_key:
        command = CronDeliveryCommand(**{**asdict(command), "event_key": "manual:" + new_ulid()})
    key = command.cron_id + ":" + command.event_key
    lock = service._task_occurrence_locks.setdefault(key, asyncio.Lock())
    async with lock:
        await _deliver(service, command)


async def _deliver(service: Any, command: CronDeliveryCommand) -> None:
    services, owner = service._task_services, command.user_id
    key = command.event_key or "manual:" + new_ulid()
    command = CronDeliveryCommand(**{**asdict(command), "event_key": key})
    occurrence_id = (
        "occurrence_" + hashlib.sha256((command.cron_id + ":" + key).encode()).hexdigest()
    )

    def receive() -> Any:
        _authorize(service, command)
        with services.db.transaction() as conn:
            return CronOccurrenceRepo.receive(
                conn,
                occurrence_id=occurrence_id,
                cron_id=command.cron_id,
                owner_id=owner,
                event_key=key,
                payload_json=json.dumps(asdict(command), ensure_ascii=False),
                now=source_routes.now_ms(),
            )

    saved = await asyncio.to_thread(receive)
    if saved["status"] in {"completed", "failed", "uncertain"}:
        return
    # A restart uses the accepted occurrence inputs, never today's changed job prompt.
    command = CronDeliveryCommand(**json.loads(saved["payload_json"]))
    await asyncio.to_thread(_authorize, service, command)
    session = await asyncio.to_thread(
        service._gateway.thread_registry.get_session, command.session_key
    )
    if session is not None and (session.user_id != owner or session.agent_id != command.agent_id):
        raise OctopError.localized(ErrorCode.FORBIDDEN)
    target: dict[str, Any] = {
        "channel_type": session.channel_type if session else "dashboard",
        "channel_id": session.channel_id if session else "octop-dashboard",
        "subject_id": session.channel_subject_id if session else str(owner),
        "metadata": dict(session.channel_metadata or {}) if session else {},
    }
    policy = command.conversation_policy
    source_key = command.session_key if policy == "continue_target" else "cron:" + command.cron_id
    if policy == "per_run":
        source_key += ":" + occurrence_id
    if policy == "task":
        source_key += ":task:" + str(command.target_task_id)
    source = ingress.SourceEnvelope(
        source_kind="cron",
        source_instance=source_routes.local_instance(services),
        source_key=source_key,
        event_id=occurrence_id,
        legacy_thread_id=session.thread_id if session and policy == "continue_target" else None,
        legacy_agent_id=command.agent_id,
        reply_target=target,
        authority={"cron_id": command.cron_id, "automatic": key.startswith(("schedule:", "mail:"))},
    )
    route = await asyncio.to_thread(
        source_routes.resolve,
        services,
        owner,
        source_kind=source.source_kind,
        source_instance=source.source_instance,
        source_key=source.source_key,
        legacy_thread_id=source.legacy_thread_id,
        legacy_agent_id=source.legacy_agent_id,
    )
    if policy == "task":
        assert command.target_task_id is not None
        await asyncio.to_thread(
            source_routes.bind_task, services, owner, route.route_id, command.target_task_id
        )
    replay = await asyncio.to_thread(ingress.lookup, services, owner, source)
    if replay is None and command.fresh_thread and policy == "continue_target":
        await asyncio.to_thread(
            source_routes.reset, services, owner, route.route_id, "cron-new-" + occurrence_id
        )
    locale = resolve_user_locale(user_repo=services.user_repo, user_id=owner)
    if command.task_type == "text":
        title = tr("cron.history.executed", locale, cron_id=command.cron_id, name=command.cron_name)
        while True:
            await asyncio.to_thread(_authorize, service, command)
            try:
                receipt = await asyncio.to_thread(
                    ingress.record_notification,
                    services,
                    owner,
                    source,
                    command.prompt,
                    title=title,
                    expert_id=command.selected_expert_id,
                    project_id=command.project_id,
                )
                break
            except OctopError as exc:
                if exc.code != ErrorCode.TASK_BUSY:
                    raise
                await asyncio.sleep(0.2)
        text = command.prompt
    else:
        receipt = replay or await ingress.submit(
            services,
            owner,
            source,
            tasks.TaskCreate(
                request_id="cron-placeholder",
                prompt=command.prompt,
                project_id=command.project_id,
                selected_expert_id=command.selected_expert_id,
                options=tasks.TaskExecutionOptions(
                    model_ref=command.model,
                    mcp_servers=list(command.mcp_servers) if command.mcp_servers else None,
                ),
            ),
        )
        await asyncio.to_thread(_update, service, occurrence_id, "submitted", receipt)
        if service._task_worker:
            service._task_worker.wake()
        last = None
        async for turn in ingress.observe(services, owner, receipt, worker=service._task_worker):
            last = turn
        if last is None or last.status != "completed" or not last.reply.strip():
            status = "waiting" if last and last.status.startswith("waiting_") else "failed"
            await asyncio.to_thread(_update, service, occurrence_id, status)
            raise OctopError.localized(ErrorCode.TASK_INTERRUPTED)
        receipt = await asyncio.to_thread(ingress.get_receipt, services, owner, receipt.inbox_id)
        text = last.reply
    if receipt.task_id is None or receipt.turn_id is None:
        raise OctopError.localized(ErrorCode.TASK_INTERRUPTED)
    await asyncio.to_thread(_update, service, occurrence_id, "submitted", receipt)
    row = await asyncio.to_thread(
        deliveries.enqueue,
        services,
        owner,
        receipt.route_id,
        receipt.task_id,
        receipt.turn_id,
        "cron-final",
        {"text": text, "target": target, "occurrence_id": occurrence_id},
    )
    claimed = await asyncio.to_thread(deliveries.claim, services, row)
    if claimed is None:
        if row.status in {"sent", "uncertain"}:
            await asyncio.to_thread(
                _update,
                service,
                occurrence_id,
                "completed" if row.status == "sent" else "uncertain",
            )
        return
    sent = False
    try:
        await asyncio.to_thread(_authorize, service, command)
        if ThreadRegistry.is_virtual_channel(target["channel_type"]):
            await service._gateway.ws_hub.push_to_user(
                owner,
                {
                    "type": "dashboard_push",
                    "task_id": receipt.task_id,
                    "thread_id": receipt.task_id,
                    "agent_id": command.selected_expert_id,
                    "text": text,
                    "agent_name": command.cron_name,
                },
            )
        else:
            channel = await asyncio.to_thread(services.channel_repo.get, target["channel_id"])
            if channel is None or not channel.enabled or channel.user_id != owner:
                raise OctopError.localized(ErrorCode.FORBIDDEN)
            await service._gateway.push_text(
                target["channel_type"],
                target["channel_id"],
                ChannelSubject(subject_id=target["subject_id"], metadata=target["metadata"]),
                text,
            )
        sent = True
    finally:
        await asyncio.to_thread(deliveries.finish, services, claimed, sent=sent)
        await asyncio.to_thread(
            _update, service, occurrence_id, "completed" if sent else "uncertain"
        )


async def recover(service: Any) -> None:
    """Resume persisted occurrences; unknown sends and terminal executions never repeat."""

    def pending() -> list[Any]:
        with service._task_services.db.connect() as conn:
            return list(
                conn.execute(
                    "SELECT * FROM unified_cron_occurrences WHERE status IN ('received','submitted','waiting') ORDER BY updated_at,id LIMIT 16"
                ).fetchall()
            )

    semaphore = asyncio.Semaphore(4)

    async def one(row: Any) -> None:
        async with semaphore:

            def touch() -> None:
                with service._task_services.db.transaction() as conn:
                    conn.execute(
                        "UPDATE unified_cron_occurrences SET updated_at=? WHERE occurrence_id=?",
                        (source_routes.now_ms(), row["occurrence_id"]),
                    )

            await asyncio.to_thread(touch)
            try:
                await deliver(service, CronDeliveryCommand(**json.loads(row["payload_json"])))

                def status() -> str:
                    with service._task_services.db.connect() as conn:
                        saved = conn.execute(
                            "SELECT status FROM unified_cron_occurrences WHERE occurrence_id=?",
                            (row["occurrence_id"],),
                        ).fetchone()
                    return str(saved["status"]) if saved else "failed"

                result = await asyncio.to_thread(status)
                if result in {"completed", "failed", "uncertain"}:
                    await asyncio.to_thread(
                        service._task_services.cron_repo.set_run_status,
                        str(row["cron_id"]),
                        ts=int(time.time()),
                        status="ok" if result == "completed" else "error",
                        error=None if result == "completed" else result,
                    )
            except OctopError as exc:
                if exc.code not in {ErrorCode.TASK_BUSY, ErrorCode.TASK_INTERRUPTED}:
                    await asyncio.to_thread(_update, service, str(row["occurrence_id"]), "failed")
                    await asyncio.to_thread(
                        service._task_services.cron_repo.set_run_status,
                        str(row["cron_id"]),
                        ts=int(time.time()),
                        status="error",
                        error=exc.code.value,
                    )

    await asyncio.gather(*(one(row) for row in await asyncio.to_thread(pending)))
