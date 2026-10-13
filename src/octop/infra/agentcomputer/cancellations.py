"""Durable Run-scoped cancellation, without a thread-scoped stop shortcut.

Only a Run that has never begun dispatch can be stopped here. A request for a
started Run remains requested until a separately verified, Run-bound engine and
process stop exists. This module never invokes a harness or a tool.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from psycopg import Error as PostgresError
from pydantic import Field, StrictBool, StrictStr, ValidationError, field_validator

from octop.infra.agentcomputer.commands import (
    CommandAdmissionError,
    StorageDataError,
    StorageUnavailableError,
    canonical_json,
    content_digest,
)
from octop.infra.agentcomputer.dispatch import _event, validate_dispatch_binding
from octop.infra.agentcomputer.effects import validate_effect_bindings
from octop.infra.agentcomputer.identity import IdentityError, read_identity
from octop.infra.agentcomputer.models import ControlModel, PositiveRevision, RunClaim
from octop.infra.agentcomputer.runs import BoundRun, load_bound_run
from octop.infra.db.repos.ac_commands import AcCommandRow
from octop.infra.db.repos.ac_run_cancellations import AcRunCancellationRow
from octop.infra.db.services import SharedServices


class CancelRunCommand(ControlModel):
    """A user decision scoped to one path Run and one restore epoch."""

    commandID: UUID = Field(description="Command UUID for querying the original receipt.")
    idempotencyKey: StrictStr = Field(
        min_length=1, max_length=256, description="Caller retry key scoped to the owner."
    )
    expectedRestoreEpoch: PositiveRevision = Field(
        description="Current box restore epoch; stale requests are rejected before replay."
    )
    expectedRevision: PositiveRevision = Field(
        description="Current Run revision, required to fence cancellation against concurrent changes."
    )


class CancelRunReceipt(ControlModel):
    """Immutable acceptance result; requested never claims that an engine stopped."""

    schemaVersion: Literal[1]
    commandID: UUID = Field(description="Stable UUID of the accepted cancellation command.")
    boxID: UUID
    runID: UUID = Field(description="Exact Run targeted by this cancellation.")
    turnID: UUID
    taskID: UUID
    projectID: UUID
    restoreEpoch: PositiveRevision
    executionEpoch: int = Field(
        strict=True, ge=0, description="Execution epoch observed when cancellation was accepted."
    )
    expectedRevision: PositiveRevision = Field(description="Run revision supplied by the caller.")
    requestRunRevision: PositiveRevision = Field(
        description="Run revision after the cancellation request was durably recorded."
    )
    resultRunRevision: PositiveRevision = Field(
        description="Final Run revision in this receipt; may equal requestRunRevision."
    )
    status: Literal["cancelRequested", "stopped"] = Field(
        description=(
            "stopped is proven only for a Run that never entered engine handoff; "
            "cancelRequested does not prove the engine or a child process stopped."
        )
    )
    acceptedAt: datetime
    replayed: StrictBool = Field(
        description="True when the original command receipt was returned for a retry."
    )

    @field_validator("acceptedAt")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        if value.utcoffset() != UTC.utcoffset(None):
            raise ValueError("cancellation receipt time must be UTC")
        return value


def _digest(*, box_id: UUID, run_id: UUID, command: CancelRunCommand) -> str:
    return content_digest(
        {
            "digestVersion": 1,
            "protocolVersion": 1,
            "operation": "run.cancel",
            "boxID": str(box_id),
            "runID": str(run_id),
            "payload": {
                "expectedRestoreEpoch": command.expectedRestoreEpoch,
                "expectedRevision": command.expectedRevision,
            },
        }
    )


def _utc_millis(value: int) -> datetime:
    return datetime.fromtimestamp(value // 1000, UTC).replace(microsecond=(value % 1000) * 1000)


def decode_cancel_receipt(
    raw: str, *, command_id: str, box_id: UUID, epoch: int
) -> CancelRunReceipt:
    try:
        receipt = CancelRunReceipt.model_validate_json(raw)
    except ValidationError as exc:
        raise StorageDataError("invalid cancellation receipt") from exc
    if (
        receipt.model_fields_set != set(CancelRunReceipt.model_fields)
        or str(receipt.commandID) != command_id
        or receipt.boxID != box_id
        or receipt.restoreEpoch != epoch
        or receipt.replayed
    ):
        raise StorageDataError("inconsistent cancellation receipt")
    return receipt


def _event_payload(receipt: CancelRunReceipt) -> dict[str, Any]:
    return {
        "commandID": str(receipt.commandID),
        "runID": str(receipt.runID),
        "restoreEpoch": receipt.restoreEpoch,
        "executionEpoch": receipt.executionEpoch,
        "runRevision": receipt.requestRunRevision,
    }


def _finished_payload(receipt: CancelRunReceipt) -> dict[str, Any]:
    return {
        "commandID": str(receipt.commandID),
        "runID": str(receipt.runID),
        "state": "stopped",
        "runRevision": receipt.resultRunRevision,
        "reasonCode": "cancelledBeforeDispatch",
        "reconciliationRequired": False,
    }


def _check_event(event: Any, bound: BoundRun, *, payload: dict[str, Any], timestamp: int) -> None:
    if (
        event.schema_version != 1
        or event.payload_version != 1
        or event.task_id != bound.run.task_id
        or event.owner_id != bound.run.owner_id
        or event.turn_id != bound.run.turn_id
        or event.run_id != bound.run.run_id
        or event.created_at != timestamp
        or event.payload_json != canonical_json(payload)
    ):
        raise StorageDataError("cancellation event binding mismatch")


def validate_cancellation_binding(
    services: SharedServices,
    conn: Any,
    bound: BoundRun,
    claim: RunClaim | None = None,
) -> AcRunCancellationRow | None:
    """Validate journal, immutable command and events, including orphan facts."""
    run = bound.run
    row = services.ac_run_cancellation_repo.get_in_transaction(conn, run.run_id, run.owner_id)
    commands = services.ac_run_cancellation_repo.list_commands_in_transaction(
        conn, run.run_id, run.owner_id
    )
    requested_events = services.ac_event_repo.list_for_run_in_transaction(
        conn, run.run_id, run.owner_id, event_type="run.cancelRequested"
    )
    finished_events = services.ac_event_repo.list_for_run_in_transaction(
        conn, run.run_id, run.owner_id, event_type="run.finished"
    )
    cancel_finished = []
    for event in finished_events:
        try:
            payload = json.loads(event.payload_json)
        except (TypeError, ValueError) as exc:
            raise StorageDataError("invalid Run finished event") from exc
        if isinstance(payload, dict) and payload.get("reasonCode") == "cancelledBeforeDispatch":
            cancel_finished.append(event)
    if row is None:
        if commands or requested_events or cancel_finished:
            raise StorageDataError("missing cancellation journal")
        return None
    if len(commands) != 1 or len(requested_events) != 1:
        raise StorageDataError("missing or duplicated cancellation command or event")
    command = commands[0]
    if (
        services.ac_run_pause_repo.command_exists_in_transaction(conn, command.command_id)
        or services.ac_run_pause_repo.get_by_key_in_transaction(
            conn, run.owner_id, command.idempotency_key
        )
        is not None
    ):
        raise StorageDataError("cancellation command identity collides with pause decision")
    receipt = decode_cancel_receipt(
        command.receipt_json,
        command_id=command.command_id,
        box_id=bound.receipt.boxID,
        epoch=command.restore_epoch,
    )
    if (
        row.pk < 1
        or row.run_id != run.run_id
        or row.turn_id != run.turn_id
        or row.task_id != run.task_id
        or row.project_id != run.project_id
        or row.owner_id != run.owner_id
        or row.command_id != command.command_id
        or row.restore_epoch != run.restore_epoch
        or row.execution_epoch != run.execution_epoch
        or row.run_revision_at_request != receipt.requestRunRevision
        or row.revision != 1
        or row.phase not in {"requested", "stopped"}
        or row.requested_at < run.created_at
        or row.requested_at > run.updated_at
        or run.revision < row.run_revision_at_request
        or command.schema_version != 1
        or command.owner_id != run.owner_id
        or command.operation != "run.cancel"
        or command.target_id != run.run_id
        or command.restore_epoch != run.restore_epoch
        or command.created_at != row.requested_at
        or receipt.runID != UUID(run.run_id)
        or receipt.turnID != UUID(run.turn_id)
        or receipt.taskID != UUID(run.task_id)
        or receipt.projectID != UUID(run.project_id)
        or receipt.executionEpoch != row.execution_epoch
        or receipt.acceptedAt != _utc_millis(row.requested_at)
        or receipt.requestRunRevision != receipt.expectedRevision + 1
        or command.payload_digest
        != _digest(
            box_id=receipt.boxID,
            run_id=receipt.runID,
            command=CancelRunCommand(
                commandID=receipt.commandID,
                idempotencyKey=command.idempotency_key,
                expectedRestoreEpoch=receipt.restoreEpoch,
                expectedRevision=receipt.expectedRevision,
            ),
        )
    ):
        raise StorageDataError("cancellation journal binding mismatch")
    _check_event(
        requested_events[0],
        bound,
        payload=_event_payload(receipt),
        timestamp=row.requested_at,
    )
    if row.phase == "requested":
        if (
            row.stopped_at is not None
            or receipt.status != "cancelRequested"
            or receipt.resultRunRevision != row.run_revision_at_request
            or cancel_finished
            or run.state == "stopped"
        ):
            raise StorageDataError("invalid pending cancellation")
        if run.state in {"queued", "running", "waitingApproval", "waitingInput", "interrupted"}:
            task = services.ac_task_repo.get_in_transaction(conn, run.task_id, run.owner_id)
            project = services.ac_project_repo.get_in_transaction(
                conn, run.project_id, run.owner_id
            )
            if (
                task is None
                or project is None
                or task.active_run_id != run.run_id
                or project.active_run_id != run.run_id
            ):
                raise StorageDataError("pending cancellation lost its active lanes")
    elif (
        row.stopped_at is None
        or row.stopped_at != row.requested_at
        or receipt.status != "stopped"
        or receipt.resultRunRevision != row.run_revision_at_request + 1
        or run.state != "stopped"
        or run.revision < receipt.resultRunRevision
        or not (
            (run.dispatch_phase == "notDispatched" and run.execution_epoch == 0 and claim is None)
            or (
                run.dispatch_phase == "claimed"
                and run.execution_epoch > 0
                and claim is not None
                and claim.dispatchRevision == 1
                and claim.engineReceipt is None
            )
        )
        or run.reconciliation_required
        or len(cancel_finished) != 1
    ):
        raise StorageDataError("invalid stopped cancellation")
    if row.phase == "stopped":
        assert row.stopped_at is not None
        _check_event(
            cancel_finished[0],
            bound,
            payload=_finished_payload(receipt),
            timestamp=row.stopped_at,
        )
        if requested_events[0].sequence >= cancel_finished[0].sequence:
            raise StorageDataError("cancellation events out of order")
        task = services.ac_task_repo.get_in_transaction(conn, run.task_id, run.owner_id)
        project = services.ac_project_repo.get_in_transaction(conn, run.project_id, run.owner_id)
        if (
            task is None
            or project is None
            or task.active_run_id == run.run_id
            or project.active_run_id == run.run_id
        ):
            raise StorageDataError("stopped cancellation retained an active lane")
    return row


def validate_cancel_command_binding(
    services: SharedServices,
    conn: Any,
    owner_id: int,
    command: AcCommandRow,
    receipt: CancelRunReceipt,
) -> None:
    """Check an existing command receipt against the current owned Run graph."""
    if command.operation != "run.cancel" or command.owner_id != owner_id:
        raise StorageDataError("invalid cancellation command")
    bound = load_bound_run(
        services,
        conn,
        owner_id=owner_id,
        run_id=receipt.runID,
        box_id=receipt.boxID,
    )
    if bound is None:
        raise StorageDataError("missing cancellation Run")
    claim = validate_dispatch_binding(services, conn, bound)
    validate_effect_bindings(services, conn, bound, claim)
    from octop.infra.agentcomputer.pauses import validate_pause_bindings

    validate_pause_bindings(services, conn, bound, claim)
    row = validate_cancellation_binding(services, conn, bound, claim)
    if row is None or row.command_id != command.command_id:
        raise StorageDataError("cancellation command has no journal")


def cancel_run(
    services: SharedServices,
    *,
    owner_id: int,
    run_id: UUID,
    command: CancelRunCommand,
) -> CancelRunReceipt:
    """Accept one explicit Run cancellation and stop only before dispatch starts."""
    try:
        identity = read_identity(services.paths.agentcomputer_identity)
    except IdentityError as exc:
        raise StorageDataError("invalid existing cancellation identity") from exc
    except OSError as exc:
        raise StorageUnavailableError("cancellation identity unavailable") from exc
    if command.expectedRestoreEpoch != identity.restoreEpoch:
        raise CommandAdmissionError("restoreEpochConflict")
    digest = _digest(box_id=identity.boxID, run_id=run_id, command=command)
    try:
        with services.db.transaction() as conn:
            if not services.ac_command_repo.lock_owner(conn, owner_id):
                raise CommandAdmissionError("accessDenied")
            if not services.ac_run_repo.lock_for_update(conn, str(run_id), owner_id):
                raise CommandAdmissionError("resourceNotFound")
            bound = load_bound_run(
                services, conn, owner_id=owner_id, run_id=run_id, box_id=identity.boxID
            )
            if bound is None:
                raise CommandAdmissionError("resourceNotFound")
            claim = validate_dispatch_binding(services, conn, bound)
            validate_effect_bindings(services, conn, bound, claim)
            from octop.infra.agentcomputer.pauses import validate_pause_bindings

            validate_pause_bindings(services, conn, bound, claim)
            existing_cancel = validate_cancellation_binding(services, conn, bound, claim)
            original = services.ac_command_repo.get_by_key(conn, owner_id, command.idempotencyKey)
            same_id = services.ac_command_repo.get_global_in_transaction(
                conn, str(command.commandID)
            )
            if (
                services.ac_run_pause_repo.command_exists_in_transaction(
                    conn, str(command.commandID)
                )
                or services.ac_run_pause_repo.get_by_key_in_transaction(
                    conn, owner_id, command.idempotencyKey
                )
                is not None
            ):
                raise CommandAdmissionError("idempotencyConflict")
            if same_id is not None and (
                same_id.owner_id != owner_id or same_id.idempotency_key != command.idempotencyKey
            ):
                raise CommandAdmissionError("idempotencyConflict")
            if original is not None:
                if original.payload_digest != digest or original.operation != "run.cancel":
                    raise CommandAdmissionError("idempotencyConflict")
                receipt = decode_cancel_receipt(
                    original.receipt_json,
                    command_id=original.command_id,
                    box_id=identity.boxID,
                    epoch=original.restore_epoch,
                )
                if receipt.runID != run_id or receipt.restoreEpoch != command.expectedRestoreEpoch:
                    raise CommandAdmissionError("idempotencyConflict")
                validate_cancel_command_binding(services, conn, owner_id, original, receipt)
                return receipt.model_copy(update={"replayed": True})
            if same_id is not None or existing_cancel is not None:
                raise CommandAdmissionError("idempotencyConflict")
            run = bound.run
            if run.restore_epoch != identity.restoreEpoch:
                raise CommandAdmissionError("restoreEpochConflict")
            if run.reconciliation_required:
                raise CommandAdmissionError("reconciliationRequired")
            if run.state not in {"queued", "running", "waitingApproval", "waitingInput"}:
                raise CommandAdmissionError("revisionConflict")
            if run.revision != command.expectedRevision:
                raise CommandAdmissionError("revisionConflict")
            task = services.ac_task_repo.get_in_transaction(conn, run.task_id, owner_id)
            project = services.ac_project_repo.get_in_transaction(conn, run.project_id, owner_id)
            if (
                task is None
                or project is None
                or task.active_run_id != run.run_id
                or project.active_run_id != run.run_id
            ):
                raise StorageDataError("cancellation Run lost its active lanes")
            timestamp = int(datetime.now(UTC).timestamp() * 1000)
            now = _utc_millis(timestamp)
            requested = services.ac_run_repo.mark_cancel_requested(
                conn,
                run.run_id,
                owner_id,
                command.expectedRevision,
                identity.restoreEpoch,
                run.execution_epoch,
                updated_at=timestamp,
            )
            if requested is None:
                raise CommandAdmissionError("revisionConflict")
            before_dispatch = run.state == "queued" and (
                (
                    run.dispatch_phase == "notDispatched"
                    and run.execution_epoch == 0
                    and claim is None
                )
                or (
                    run.dispatch_phase == "claimed"
                    and run.execution_epoch > 0
                    and claim is not None
                    and claim.dispatchRevision == 1
                    and claim.engineReceipt is None
                )
            )
            result = requested
            if before_dispatch:
                stopped = services.ac_run_repo.mark_stopped_unstarted(
                    conn,
                    run.run_id,
                    owner_id,
                    requested.revision,
                    identity.restoreEpoch,
                    updated_at=timestamp,
                )
                if stopped is None:
                    raise CommandAdmissionError("revisionConflict")
                result = stopped
            receipt = CancelRunReceipt(
                schemaVersion=1,
                commandID=command.commandID,
                boxID=identity.boxID,
                runID=run_id,
                turnID=UUID(run.turn_id),
                taskID=UUID(run.task_id),
                projectID=UUID(run.project_id),
                restoreEpoch=run.restore_epoch,
                executionEpoch=run.execution_epoch,
                expectedRevision=command.expectedRevision,
                requestRunRevision=requested.revision,
                resultRunRevision=result.revision,
                status="stopped" if before_dispatch else "cancelRequested",
                acceptedAt=now,
                replayed=False,
            )
            if not services.ac_command_repo.insert(
                conn,
                command_id=str(command.commandID),
                owner_id=owner_id,
                idempotency_key=command.idempotencyKey,
                operation="run.cancel",
                target_id=str(run_id),
                payload_digest=digest,
                restore_epoch=run.restore_epoch,
                receipt_json=receipt.model_dump_json(),
                created_at=timestamp,
            ):
                raise CommandAdmissionError("idempotencyConflict")
            cancellation = services.ac_run_cancellation_repo.insert(
                conn,
                run_id=run.run_id,
                turn_id=run.turn_id,
                task_id=run.task_id,
                project_id=run.project_id,
                owner_id=owner_id,
                command_id=str(command.commandID),
                restore_epoch=run.restore_epoch,
                execution_epoch=run.execution_epoch,
                run_revision_at_request=requested.revision,
                phase="stopped" if before_dispatch else "requested",
                requested_at=timestamp,
                stopped_at=timestamp if before_dispatch else None,
            )
            if cancellation is None:
                raise CommandAdmissionError("idempotencyConflict")
            _event(services, conn, bound, "run.cancelRequested", _event_payload(receipt), timestamp)
            if before_dispatch:
                _event(services, conn, bound, "run.finished", _finished_payload(receipt), timestamp)
                if not services.ac_task_repo.release_run(
                    conn, run.task_id, owner_id, run.run_id, updated_at=timestamp
                ) or not services.ac_project_repo.release_run(
                    conn, run.project_id, owner_id, run.run_id, updated_at=timestamp
                ):
                    raise CommandAdmissionError("activeRunConflict")
            return receipt
    except (sqlite3.DatabaseError, PostgresError) as exc:
        raise StorageUnavailableError("cancellation storage unavailable") from exc
    except StorageDataError:
        raise
    except (ValueError, TypeError, OverflowError) as exc:
        raise StorageDataError("invalid stored cancellation fact") from exc
