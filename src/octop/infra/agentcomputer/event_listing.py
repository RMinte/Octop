"""Read an owned Task's durable control events without exposing internal payloads."""

from __future__ import annotations

import base64
import binascii
import json
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal, cast, get_args
from uuid import UUID

from psycopg import Error as PostgresError
from pydantic import Field, StrictBool, StrictInt, field_validator

from octop.infra.agentcomputer.commands import (
    StorageDataError,
    StorageUnavailableError,
    canonical_json,
)
from octop.infra.agentcomputer.identity import IdentityError, get_or_create_identity
from octop.infra.agentcomputer.models import ControlModel, RunView
from octop.infra.agentcomputer.runs import get_run_in_transaction
from octop.infra.agentcomputer.tasks import _get_task
from octop.infra.db.repos.ac_events import AcEventRow
from octop.infra.db.repos.ac_run_dispatches import AcRunDispatchRow
from octop.infra.db.repos.ac_runs import AcRunRow
from octop.infra.db.services import SharedServices

EventType = Literal[
    "run.accepted",
    "run.claimed",
    "run.dispatching",
    "run.engineAccepted",
    "run.cancelRequested",
    "run.interrupted",
    "run.finished",
    "tool.prepared",
    "tool.dispatching",
    "tool.terminal",
    "tool.outcomeUnknown",
    "approval.requested",
    "approval.decided",
    "approval.expired",
    "question.asked",
    "question.answered",
    "question.expired",
]
_EVENT_TYPES = set(get_args(EventType))
_CURSOR_FIELDS = {
    "v",
    "boxID",
    "ownerID",
    "taskID",
    "restoreEpoch",
    "afterSequence",
    "afterEventID",
    "throughSequence",
}
_CURSOR_PATTERN = re.compile(r"[A-Za-z0-9_-]+\Z")


class InvalidTaskEventCursor(ValueError):
    """The caller supplied a malformed or differently scoped event cursor."""


class TaskEventItem(ControlModel):
    eventID: UUID
    taskID: UUID
    turnID: UUID
    runID: UUID
    sequence: Annotated[StrictInt, Field(ge=1)]
    occurredAt: datetime
    type: EventType = Field(description="Persisted control fact, not a model-token stream.")

    @field_validator("occurredAt")
    @classmethod
    def utc_event_time(cls, value: datetime) -> datetime:
        if value.utcoffset() != timedelta(0):
            raise ValueError("event time must be UTC")
        return value


class TaskEventListResponse(ControlModel):
    items: list[TaskEventItem]
    coveredSequence: Annotated[StrictInt, Field(ge=0)] = Field(
        description="Highest committed Task sequence included in this page's fixed read window."
    )
    hasMore: StrictBool = Field(description="More events remain in this read window.")
    nextCursor: str = Field(
        description="Opaque cursor for the next page or, once caught up, the next poll."
    )


def _canonical_uuid(value: object) -> UUID:
    if type(value) is not str:
        raise ValueError("invalid UUID")
    result = UUID(value)
    if str(result) != value:
        raise ValueError("non-canonical UUID")
    return result


def _encode_cursor(
    *,
    box_id: UUID,
    owner_id: int,
    task_id: UUID,
    restore_epoch: int,
    after_sequence: int,
    after_event_id: UUID | None,
    through_sequence: int | None,
) -> str:
    payload = canonical_json(
        {
            "v": 1,
            "boxID": str(box_id),
            "ownerID": owner_id,
            "taskID": str(task_id),
            "restoreEpoch": restore_epoch,
            "afterSequence": after_sequence,
            "afterEventID": str(after_event_id) if after_event_id is not None else None,
            "throughSequence": through_sequence,
        }
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def _decode_cursor(
    cursor: str,
    *,
    box_id: UUID,
    owner_id: int,
    task_id: UUID,
    restore_epoch: int,
) -> tuple[int, UUID | None, int | None]:
    if not 1 <= len(cursor) <= 768 or _CURSOR_PATTERN.fullmatch(cursor) is None:
        raise InvalidTaskEventCursor("invalid cursor encoding")
    try:
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        if base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii") != cursor:
            raise ValueError("non-canonical cursor encoding")
        payload = json.loads(raw.decode("utf-8"))
        if type(payload) is not dict or set(payload) != _CURSOR_FIELDS:
            raise ValueError("invalid cursor fields")
        if canonical_json(payload).encode("utf-8") != raw:
            raise ValueError("non-canonical cursor payload")
        after = payload["afterSequence"]
        through = payload["throughSequence"]
        event_id = payload["afterEventID"]
        if (
            type(payload["v"]) is not int
            or payload["v"] != 1
            or type(payload["ownerID"]) is not int
            or payload["ownerID"] != owner_id
            or type(payload["restoreEpoch"]) is not int
            or payload["restoreEpoch"] != restore_epoch
            or payload["boxID"] != str(box_id)
            or payload["taskID"] != str(task_id)
            or type(after) is not int
            or not 0 <= after <= 2**63 - 1
            or (event_id is None) != (after == 0)
            or (through is not None and (type(through) is not int or through < after))
        ):
            raise ValueError("cursor scope or position mismatch")
        return after, _canonical_uuid(event_id) if event_id is not None else None, through
    except (binascii.Error, UnicodeError, ValueError, TypeError) as exc:
        raise InvalidTaskEventCursor("invalid cursor") from exc


def _validate_payload(
    event: AcEventRow, run: RunView, run_row: AcRunRow, dispatch: AcRunDispatchRow | None
) -> None:
    if (
        event.schema_version != 1
        or event.payload_version != 1
        or event.event_type not in _EVENT_TYPES
    ):
        raise StorageDataError("unsupported Task event")
    try:
        payload = json.loads(event.payload_json)
        if type(payload) is not dict or canonical_json(payload) != event.payload_json:
            raise ValueError("non-canonical event payload")
        kind = event.event_type
        if kind == "run.accepted":
            if payload.get("runID") != event.run_id or payload.get("turnID") != event.turn_id:
                raise ValueError("acceptance identity mismatch")
        elif kind in {"run.claimed", "run.dispatching", "run.engineAccepted", "run.interrupted"}:
            if (
                dispatch is None
                or payload.get("runID") != event.run_id
                or payload.get("turnID") != event.turn_id
                or payload.get("dispatchID") != dispatch.dispatch_id
                or type(payload.get("runRevision")) is not int
                or type(payload.get("executionEpoch")) is not int
                or type(payload.get("restoreEpoch")) is not int
                or payload["runRevision"] < 1
                or payload["executionEpoch"] != dispatch.execution_epoch
                or payload["restoreEpoch"] != dispatch.restore_epoch
            ):
                raise ValueError("dispatch event binding mismatch")
            if kind == "run.claimed" and (
                event.created_at != dispatch.created_at or payload["runRevision"] != 2
            ):
                raise ValueError("claim event differs from dispatch journal")
            if kind == "run.dispatching" and (
                event.created_at != dispatch.dispatch_started_at or payload["runRevision"] != 3
            ):
                raise ValueError("dispatch event differs from dispatch journal")
            if kind == "run.engineAccepted" and (
                event.created_at != dispatch.engine_accepted_at
                or payload["runRevision"] != 4
                or payload.get("receiptID") != dispatch.engine_receipt_id
            ):
                raise ValueError("engine acceptance event differs from dispatch journal")
            if kind == "run.interrupted" and (
                run.state != "interrupted"
                or payload.get("reconciliationRequired") is not True
                or payload["runRevision"] != run_row.revision
                or event.created_at != run_row.updated_at
            ):
                raise ValueError("interruption event mismatch")
            if run.dispatchPhase == "notDispatched":
                raise ValueError("dispatch event without dispatch")
        elif kind == "run.cancelRequested":
            if payload.get("runID") != event.run_id:
                raise ValueError("cancellation event binding mismatch")
        elif kind == "run.finished":
            if (
                payload.get("runID") != event.run_id
                or payload.get("state") not in {"succeeded", "stopped"}
                or payload["state"] != run.state
                or payload.get("runRevision") != run_row.revision
                or event.created_at != run_row.updated_at
            ):
                raise ValueError("terminal event binding mismatch")
        elif kind.startswith("tool."):
            _canonical_uuid(payload.get("effectID"))
            if (
                payload.get("phase") != kind.removeprefix("tool.")
                or type(payload.get("toolCallID")) is not str
            ):
                raise ValueError("tool event binding mismatch")
        elif kind.startswith("approval.") or kind.startswith("question."):
            expected_kind = "approval" if kind.startswith("approval.") else "question"
            _canonical_uuid(payload.get("pauseID"))
            if payload.get("kind") != expected_kind:
                raise ValueError("pause event binding mismatch")
    except (ValueError, TypeError, KeyError) as exc:
        raise StorageDataError("invalid Task event payload") from exc


def list_task_events(
    services: SharedServices,
    owner_id: int,
    task_id: UUID,
    limit: int,
    cursor: str | None,
) -> TaskEventListResponse | None:
    """Read a forward, gap-checked page from one durable Task event window."""
    try:
        identity = get_or_create_identity(services.paths.agentcomputer_identity)
        with services.db.transaction() as conn:
            task = _get_task(services, conn, owner_id, task_id, identity.boxID)
            if task is None:
                return None
            row = services.ac_task_repo.get_in_transaction(conn, str(task_id), owner_id)
            if row is None or row.next_sequence < 1 or row.project_id != str(task.projectID):
                raise StorageDataError("invalid Task event watermark")
            after, after_event_id, through = (
                _decode_cursor(
                    cursor,
                    box_id=identity.boxID,
                    owner_id=owner_id,
                    task_id=task_id,
                    restore_epoch=identity.restoreEpoch,
                )
                if cursor is not None
                else (0, None, None)
            )
            high_water = row.next_sequence - 1
            if after > high_water or (through is not None and through > high_water):
                raise InvalidTaskEventCursor("cursor exceeds the Task event watermark")
            if after_event_id is not None:
                anchor = services.ac_event_repo.get_by_sequence_in_transaction(
                    conn, str(task_id), owner_id, after
                )
                if anchor is None or anchor.event_id != str(after_event_id):
                    raise InvalidTaskEventCursor("cursor anchor is missing or changed")
            covered = through if through is not None else high_water
            rows = services.ac_event_repo.list_page_for_task_in_transaction(
                conn,
                str(task_id),
                owner_id,
                after_sequence=after,
                through_sequence=covered,
                limit=limit + 1,
            )
            if not rows and covered > after:
                raise StorageDataError("Task event sequence has a gap")
            sequence = after
            for event in rows:
                sequence += 1
                if event.sequence != sequence:
                    raise StorageDataError("Task event sequence has a gap")
            has_more = len(rows) > limit
            page = rows[:limit]
            if not has_more and sequence != covered:
                raise StorageDataError("Task event sequence has a gap")
            items: list[TaskEventItem] = []
            run_cache: dict[str, tuple[RunView, AcRunRow, AcRunDispatchRow | None]] = {}
            for event in page:
                event_id = _canonical_uuid(event.event_id)
                run_id = _canonical_uuid(event.run_id)
                turn_id = _canonical_uuid(event.turn_id)
                if (
                    event.pk < 1
                    or event.task_id != str(task_id)
                    or event.owner_id != owner_id
                    or event.created_at < 0
                ):
                    raise StorageDataError("Task event scope mismatch")
                if event.run_id not in run_cache:
                    run = get_run_in_transaction(
                        services, conn, owner_id=owner_id, run_id=run_id, box_id=identity.boxID
                    )
                    if run is None or run.taskID != task_id or run.projectID != task.projectID:
                        raise StorageDataError("Task event Run binding mismatch")
                    run_row = services.ac_run_repo.get_in_transaction(conn, event.run_id, owner_id)
                    if run_row is None:
                        raise StorageDataError("Task event Run disappeared")
                    dispatch = services.ac_run_dispatch_repo.get_in_transaction(
                        conn, event.run_id, owner_id
                    )
                    run_cache[event.run_id] = (run, run_row, dispatch)
                run, run_row, dispatch = run_cache[event.run_id]
                if run.turnID != turn_id or run_row.turn_id != event.turn_id:
                    raise StorageDataError("Task event Turn binding mismatch")
                _validate_payload(event, run, run_row, dispatch)
                items.append(
                    TaskEventItem(
                        eventID=event_id,
                        taskID=task_id,
                        turnID=turn_id,
                        runID=run_id,
                        sequence=event.sequence,
                        occurredAt=datetime.fromtimestamp(event.created_at / 1000, UTC),
                        type=cast(EventType, event.event_type),
                    )
                )
            last_sequence = page[-1].sequence if page else after
            last_event_id = UUID(page[-1].event_id) if page else after_event_id
            next_cursor = _encode_cursor(
                box_id=identity.boxID,
                owner_id=owner_id,
                task_id=task_id,
                restore_epoch=identity.restoreEpoch,
                after_sequence=last_sequence,
                after_event_id=last_event_id,
                through_sequence=covered if has_more else None,
            )
            return TaskEventListResponse(
                items=items,
                coveredSequence=covered,
                hasMore=has_more,
                nextCursor=next_cursor,
            )
    except (sqlite3.DatabaseError, PostgresError) as exc:
        raise StorageUnavailableError("Task event storage unavailable") from exc
    except (IdentityError, InvalidTaskEventCursor, StorageDataError, StorageUnavailableError):
        raise
    except (ValueError, TypeError, OverflowError, OSError) as exc:
        raise StorageDataError("invalid stored Task event") from exc
