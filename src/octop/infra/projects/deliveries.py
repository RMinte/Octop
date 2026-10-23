"""Outbound receipts never own model execution or repeat an unknown send."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from octop.infra.db.repos.unified_sources import (
    UnifiedDelivery,
    UnifiedDeliveryRepo,
    UnifiedSourceRouteRepo,
)
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects import source_routes
from octop.infra.projects.deletion_state import assert_task_writable
from octop.infra.utils.ulid import new_ulid


def enqueue(
    services: SharedServices,
    owner_id: int,
    route_id: str,
    task_id: str,
    turn_id: str,
    key: str,
    payload: dict[str, Any],
) -> UnifiedDelivery:
    identity = hashlib.sha256((route_id + ":" + turn_id + ":" + key).encode()).hexdigest()
    with services.db.transaction() as conn:
        if not services.project_task_repo.lock_owner(conn, owner_id):
            raise OctopError.localized(ErrorCode.FORBIDDEN)
        route = UnifiedSourceRouteRepo.get(conn, owner_id, route_id)
        if route is None or route.task_id != task_id:
            raise OctopError.localized(ErrorCode.NOT_FOUND)
        source_routes.require_writable(route)
        assert_task_writable(services, conn, owner_id, task_id)
        return UnifiedDeliveryRepo.enqueue(
            conn,
            delivery_id="delivery_" + identity,
            route_id=route_id,
            owner_id=owner_id,
            task_id=task_id,
            turn_id=turn_id,
            delivery_key=key,
            payload_json=json.dumps(payload, ensure_ascii=False),
            now=source_routes.now_ms(),
        )


def claim(services: SharedServices, row: UnifiedDelivery) -> UnifiedDelivery | None:
    with services.db.transaction() as conn:
        if not services.project_task_repo.lock_owner(conn, row.owner_id):
            return None
        route = UnifiedSourceRouteRepo.get(conn, row.owner_id, row.route_id)
        user = services.user_repo.get(row.owner_id)
        if (
            route is None
            or route.task_id != row.task_id
            or route.state != "task"
            or user is None
            or user.disabled
        ):
            return None
        try:
            assert_task_writable(services, conn, row.owner_id, row.task_id)
        except OctopError:
            return None
        return UnifiedDeliveryRepo.claim(conn, row.delivery_id, new_ulid(), source_routes.now_ms())


def finish(
    services: SharedServices,
    row: UnifiedDelivery,
    *,
    sent: bool,
    receipt: dict[str, Any] | None = None,
) -> None:
    with services.db.transaction() as conn:
        UnifiedDeliveryRepo.finish(
            conn,
            row,
            status="sent" if sent else "uncertain",
            receipt_json=json.dumps(receipt or {}),
            error_code="" if sent else "unknown_delivery_outcome",
            now=source_routes.now_ms(),
        )


async def recover_im(services: SharedServices, gateway: Any) -> None:
    """Deliver only the saved unsent suffix after an observer disconnects.

    A sending/uncertain receipt is a reconciliation boundary. It can never be
    converted into a second push just because the process restarted.
    """
    import asyncio

    from octop_gateway.models import ChannelSubject

    def completed() -> list[Any]:
        with services.db.transaction() as conn:
            conn.execute(
                "UPDATE unified_source_deliveries SET status='uncertain',error_code='unknown_delivery_outcome',updated_at=? WHERE status='sending' AND lease_until<?",
                (source_routes.now_ms(), source_routes.now_ms()),
            )
            return list(
                conn.execute(
                    "SELECT i.*,t.reply,t.finished_at FROM unified_source_inbox i JOIN project_task_turns t ON t.turn_id=i.turn_id WHERE i.status='admitted' AND i.payload_json LIKE '%\"source_kind\": \"im\"%' AND t.status='completed' AND t.finished_at<? AND NOT EXISTS (SELECT 1 FROM unified_source_deliveries d WHERE d.route_id=i.route_id AND d.turn_id=t.turn_id AND d.delivery_key LIKE 'text:%' AND (d.status IN ('sending','uncertain') OR (d.status='sent' AND d.delivery_key='text:' || CAST(LENGTH(t.reply) AS TEXT)))) ORDER BY i.id LIMIT 100",
                    (source_routes.now_ms() - 1500,),
                ).fetchall()
            )

    for inbox in await asyncio.to_thread(completed):
        source = json.loads(inbox["payload_json"])["source"]
        if source.get("source_kind") != "im" or not inbox["reply"]:
            continue
        target = source.get("reply_target") or {}
        channel = await asyncio.to_thread(services.channel_repo.get, target.get("channel_id", ""))
        if channel is None or not channel.enabled or channel.user_id != inbox["owner_id"]:
            continue

        def previous(inbox: Any = inbox) -> list[UnifiedDelivery]:
            with services.db.connect() as conn:
                return [
                    UnifiedDelivery.from_row(row)
                    for row in conn.execute(
                        "SELECT * FROM unified_source_deliveries WHERE route_id=? AND turn_id=? AND delivery_key LIKE 'text:%' ORDER BY id",
                        (inbox["route_id"], inbox["turn_id"]),
                    ).fetchall()
                ]

        rows = await asyncio.to_thread(previous)
        if any(row.status in {"sending", "uncertain"} for row in rows):
            for row in rows:
                if row.status == "sending":
                    await asyncio.to_thread(
                        claim, services, row
                    )  # expires to uncertain, never resends
            continue
        sent_offset = max(
            (int(row.delivery_key.split(":", 1)[1]) for row in rows if row.status == "sent"),
            default=0,
        )
        full = str(inbox["reply"])
        if sent_offset >= len(full):
            continue
        pending: UnifiedDelivery | None = next(
            (row for row in rows if row.status == "pending"), None
        )
        try:
            candidate = pending or await asyncio.to_thread(
                enqueue,
                services,
                int(inbox["owner_id"]),
                str(inbox["route_id"]),
                str(inbox["task_id"]),
                str(inbox["turn_id"]),
                "text:" + str(len(full)),
                {"text": full[sent_offset:]},
            )
            assert candidate is not None
            claimed = await asyncio.to_thread(claim, services, candidate)
        except OctopError:
            continue
        if claimed is None:
            continue
        sent = False
        try:
            await gateway.push_text(
                str(target["channel_type"]),
                str(target["channel_id"]),
                ChannelSubject(
                    subject_id=str(target["subject_id"]), metadata=target.get("metadata") or {}
                ),
                str(json.loads(claimed.payload_json)["text"]),
            )
            sent = True
        finally:
            await asyncio.to_thread(finish, services, claimed, sent=sent)


async def recover_im_artifacts(services: SharedServices, gateway: Any) -> None:
    """Publish immutable artifact bytes with the same no-replay delivery receipt."""
    import asyncio
    import base64

    from octop_gateway.models import ChannelSubject, FileContent

    from octop.infra.projects.artifacts import download

    def pending() -> list[Any]:
        with services.db.connect() as conn:
            return list(
                conn.execute(
                    "SELECT i.*,a.artifact_id FROM unified_source_inbox i JOIN project_task_turns t ON t.turn_id=i.turn_id JOIN project_task_artifacts a ON a.turn_id=t.turn_id WHERE i.status='admitted' AND i.payload_json LIKE '%\"source_kind\": \"im\"%' AND t.status='completed' AND t.finished_at<? AND NOT EXISTS (SELECT 1 FROM unified_source_deliveries d WHERE d.route_id=i.route_id AND d.turn_id=t.turn_id AND d.delivery_key='artifact:' || a.artifact_id AND d.status IN ('sent','sending','uncertain')) ORDER BY i.id LIMIT 100",
                    (source_routes.now_ms() - 1500,),
                ).fetchall()
            )

    for inbox in await asyncio.to_thread(pending):
        source = json.loads(inbox["payload_json"])["source"]
        if source.get("source_kind") != "im":
            continue
        target = source.get("reply_target") or {}
        channel = await asyncio.to_thread(services.channel_repo.get, target.get("channel_id", ""))
        if channel is None or not channel.enabled or channel.user_id != inbox["owner_id"]:
            continue
        try:
            row = await asyncio.to_thread(
                enqueue,
                services,
                int(inbox["owner_id"]),
                str(inbox["route_id"]),
                str(inbox["task_id"]),
                str(inbox["turn_id"]),
                "artifact:" + str(inbox["artifact_id"]),
                {"artifact_id": inbox["artifact_id"]},
            )
            claimed = await asyncio.to_thread(claim, services, row)
        except OctopError:
            continue
        if claimed is None:
            continue
        sent = False
        try:
            artifact, data = await asyncio.to_thread(
                download,
                services,
                int(inbox["owner_id"]),
                str(inbox["task_id"]),
                str(inbox["artifact_id"]),
            )
            await gateway.push_content(
                str(target["channel_id"]),
                ChannelSubject(
                    subject_id=str(target["subject_id"]), metadata=target.get("metadata") or {}
                ),
                [
                    FileContent(
                        filename=artifact.name,
                        mime_type=artifact.media_type,
                        size=len(data),
                        data=base64.b64encode(data).decode(),
                    )
                ],
            )
            sent = True
        finally:
            await asyncio.to_thread(finish, services, claimed, sent=sent)
