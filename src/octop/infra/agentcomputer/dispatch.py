"""Durable, fenced dispatch boundaries; no production worker is started here.

The installed capability profile keeps new claims unavailable. The dedicated
entry and its tests use the same fixed intent and acceptance ledger. An entry
receipt does not prove a model started, a tool ran, or execution completed.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from psycopg import Error as PostgresError
from pydantic import ValidationError

from octop.infra.agentcomputer.capabilities import get_capabilities
from octop.infra.agentcomputer.commands import (
    CommandAdmissionError,
    StorageDataError,
    StorageUnavailableError,
    canonical_json,
    content_digest,
)
from octop.infra.agentcomputer.identity import ControlIdentity, get_or_create_identity
from octop.infra.agentcomputer.models import (
    DispatchIntent,
    EngineEntryReceipt,
    EngineEntryResult,
    EngineInvocation,
    RunClaim,
)
from octop.infra.agentcomputer.runs import BoundRun, load_bound_run
from octop.infra.db.services import SharedServices


@contextmanager
def _transaction(services: SharedServices) -> Iterator[Any]:
    try:
        with services.db.transaction() as conn:
            yield conn
    except (sqlite3.DatabaseError, PostgresError) as exc:
        raise StorageUnavailableError("dispatch storage unavailable") from exc
    except StorageDataError:
        raise
    except (ValueError, TypeError) as exc:
        raise StorageDataError("invalid stored dispatch fact") from exc


def _worker_identity(services: SharedServices) -> ControlIdentity:
    identity = get_or_create_identity(services.paths.agentcomputer_identity)
    profile = get_capabilities(services)
    if profile.boxID != identity.boxID or profile.restoreEpoch != identity.restoreEpoch:
        raise StorageDataError("dispatch capability identity mismatch")
    if (
        not profile.capabilities.runs.available
        or "askForApproval" not in profile.supportedPermissionModes
    ):
        raise CommandAdmissionError("runtimeUnavailable", reason="executionUnavailable")
    return identity


def _bound(
    services: SharedServices, conn: Any, owner_id: int, run_id: UUID, box_id: UUID
) -> BoundRun:
    bound = load_bound_run(services, conn, owner_id=owner_id, run_id=run_id, box_id=box_id)
    if bound is None:
        raise CommandAdmissionError("resourceNotFound")
    return bound


def _active(services: SharedServices, conn: Any, bound: BoundRun) -> None:
    run = bound.run
    task = services.ac_task_repo.get_in_transaction(conn, run.task_id, run.owner_id)
    project = services.ac_project_repo.get_in_transaction(conn, run.project_id, run.owner_id)
    if task is None or project is None:
        raise StorageDataError("missing dispatch lane")
    if task.active_run_id != run.run_id or project.active_run_id != run.run_id:
        raise CommandAdmissionError("activeRunConflict")
    if task.preparation_state != "ready" or project.preparation_state != "ready":
        raise CommandAdmissionError("runtimeUnavailable", reason="preparationIncomplete")
    if run.reconciliation_required or run.state != "queued":
        raise CommandAdmissionError("reconciliationRequired")


def validate_dispatch_binding(
    services: SharedServices, conn: Any, bound: BoundRun
) -> RunClaim | None:
    """Validate full journal/receipt facts without inventing old missing records."""
    run = bound.run
    row = services.ac_run_dispatch_repo.get_in_transaction(conn, run.run_id, run.owner_id)
    if row is None:
        if run.dispatch_phase != "notDispatched" or run.execution_epoch != 0:
            raise StorageDataError("missing progressed run dispatch journal")
        return None
    try:
        intent = DispatchIntent.model_validate_json(row.intent_json)
        claimant_id = UUID(row.claimant_id)
        if intent.model_fields_set != set(DispatchIntent.model_fields):
            raise StorageDataError("incomplete dispatch intent")
        receipt = (
            EngineEntryReceipt.model_validate_json(row.engine_receipt_json)
            if row.engine_receipt_json is not None
            else None
        )
    except (ValueError, ValidationError) as exc:
        raise StorageDataError("invalid dispatch intent or engine receipt") from exc
    digest = content_digest(intent.model_dump(mode="json"))
    if (
        row.schema_version != 1
        or row.owner_id != run.owner_id
        or row.run_id != run.run_id
        or row.turn_id != run.turn_id
        or row.task_id != run.task_id
        or row.project_id != run.project_id
        or row.restore_epoch != run.restore_epoch
        or row.execution_epoch != run.execution_epoch
        or row.dispatch_id != str(intent.dispatchID)
        or row.engine_request_id != str(intent.engineRequestID)
        or row.intent_digest != digest
        or intent.boxID != bound.receipt.boxID
        or intent.ownerID != run.owner_id
        or str(intent.runID) != run.run_id
        or str(intent.turnID) != run.turn_id
        or str(intent.taskID) != run.task_id
        or str(intent.projectID) != run.project_id
        or intent.restoreEpoch != run.restore_epoch
        or intent.executionEpoch != run.execution_epoch
        or intent.userMessageID != bound.receipt.userMessageID
        or intent.assistantMessageID != bound.receipt.assistantMessageID
        or intent.snapshotDigest != content_digest(bound.snapshot.model_dump(mode="json"))
        or canonical_json(intent.snapshot.model_dump(mode="json"))
        != canonical_json(bound.snapshot.model_dump(mode="json"))
        or row.created_at < run.created_at
        or row.updated_at < row.created_at
        or row.revision < 1
        or run.dispatch_phase == "notDispatched"
    ):
        raise StorageDataError("dispatch intent binding mismatch")
    if row.dispatch_started_at is None:
        if run.dispatch_phase != "claimed" or row.revision != 1 or receipt is not None:
            raise StorageDataError("invalid undispatched claim")
    elif row.dispatch_started_at < row.created_at or run.dispatch_phase == "claimed":
        raise StorageDataError("invalid dispatch start fact")
    fields = (
        row.engine_receipt_id,
        row.engine_receipt_json,
        row.engine_receipt_digest,
        row.engine_accepted_at,
    )
    if receipt is None:
        if any(value is not None for value in fields) or run.dispatch_phase == "engineAccepted":
            raise StorageDataError("missing engine entry receipt")
    elif (
        receipt.model_fields_set != set(EngineEntryReceipt.model_fields)
        or any(value is None for value in fields)
        or row.engine_receipt_id != str(receipt.receiptID)
        or row.engine_receipt_digest != content_digest(receipt.model_dump(mode="json"))
        or row.engine_accepted_at != int(receipt.acceptedAt.timestamp() * 1000)
        or row.dispatch_started_at is None
        or row.engine_accepted_at < row.dispatch_started_at
        or receipt.dispatchID != intent.dispatchID
        or receipt.engineRequestID != intent.engineRequestID
        or receipt.runID != intent.runID
        or receipt.turnID != intent.turnID
        or receipt.restoreEpoch != intent.restoreEpoch
        or receipt.executionEpoch != intent.executionEpoch
        or receipt.intentDigest != digest
        or run.dispatch_phase != "engineAccepted"
    ):
        raise StorageDataError("engine entry receipt binding mismatch")
    return RunClaim(
        intent=intent,
        claimantID=claimant_id,
        runRevision=run.revision,
        dispatchRevision=row.revision,
        engineReceipt=receipt,
    )


def _match(claim: RunClaim, current: RunClaim) -> None:
    if claim.intent != current.intent or claim.claimantID != current.claimantID:
        raise CommandAdmissionError("revisionConflict")


def _epoch(identity: ControlIdentity, intent: DispatchIntent) -> None:
    if identity.boxID != intent.boxID:
        raise StorageDataError("dispatch installation mismatch")
    if identity.restoreEpoch != intent.restoreEpoch:
        raise CommandAdmissionError("restoreEpochConflict")


def _event(
    services: SharedServices,
    conn: Any,
    bound: BoundRun,
    event_type: str,
    payload: dict[str, Any],
    timestamp: int,
) -> None:
    run = bound.run
    task = services.ac_task_repo.get_in_transaction(conn, run.task_id, run.owner_id)
    if task is None:
        raise StorageDataError("missing dispatch event task")
    allocation = services.ac_task_repo.allocate_event_sequence(
        conn,
        run.task_id,
        run.owner_id,
        run.run_id,
        expected_revision=task.revision,
        updated_at=timestamp,
    )
    if allocation is None:
        raise CommandAdmissionError("activeRunConflict")
    services.ac_event_repo.insert(
        conn,
        event_id=str(uuid4()),
        task_id=run.task_id,
        owner_id=run.owner_id,
        sequence=allocation[0],
        run_id=run.run_id,
        turn_id=run.turn_id,
        event_type=event_type,
        payload_version=1,
        payload_json=canonical_json(payload),
        created_at=timestamp,
    )


def _payload(claim: RunClaim) -> dict[str, Any]:
    intent = claim.intent
    return {
        "dispatchID": str(intent.dispatchID),
        "runID": str(intent.runID),
        "turnID": str(intent.turnID),
        "restoreEpoch": intent.restoreEpoch,
        "executionEpoch": intent.executionEpoch,
        "runRevision": claim.runRevision,
    }


def get_claim(services: SharedServices, *, owner_id: int, run_id: UUID) -> RunClaim | None:
    identity = get_or_create_identity(services.paths.agentcomputer_identity)
    with _transaction(services) as conn:
        bound = load_bound_run(
            services, conn, owner_id=owner_id, run_id=run_id, box_id=identity.boxID
        )
        return validate_dispatch_binding(services, conn, bound) if bound is not None else None


def claim_run(
    services: SharedServices,
    *,
    owner_id: int,
    run_id: UUID,
    claimant_id: UUID,
    expected_revision: int,
) -> RunClaim:
    identity = _worker_identity(services)
    with _transaction(services) as conn:
        if not services.ac_command_repo.lock_owner(conn, owner_id):
            raise CommandAdmissionError("accessDenied")
        bound = _bound(services, conn, owner_id, run_id, identity.boxID)
        if bound.run.restore_epoch != identity.restoreEpoch:
            raise CommandAdmissionError("restoreEpochConflict")
        _active(services, conn, bound)
        if validate_dispatch_binding(services, conn, bound) is not None:
            raise CommandAdmissionError("revisionConflict")
        now = int(datetime.now(UTC).timestamp() * 1000)
        run = services.ac_run_repo.claim(
            conn, str(run_id), owner_id, expected_revision, identity.restoreEpoch, updated_at=now
        )
        if run is None:
            raise CommandAdmissionError("revisionConflict")
        intent = DispatchIntent(
            schemaVersion=1,
            boxID=identity.boxID,
            ownerID=owner_id,
            dispatchID=uuid4(),
            engineRequestID=uuid4(),
            runID=run_id,
            turnID=bound.receipt.turnID,
            taskID=bound.receipt.taskID,
            projectID=UUID(run.project_id),
            restoreEpoch=run.restore_epoch,
            executionEpoch=run.execution_epoch,
            userMessageID=bound.receipt.userMessageID,
            assistantMessageID=bound.receipt.assistantMessageID,
            snapshotDigest=content_digest(bound.snapshot.model_dump(mode="json")),
            snapshot=bound.snapshot,
        )
        row = services.ac_run_dispatch_repo.insert(
            conn,
            dispatch_id=str(intent.dispatchID),
            run_id=str(run_id),
            turn_id=run.turn_id,
            task_id=run.task_id,
            project_id=run.project_id,
            owner_id=owner_id,
            execution_epoch=run.execution_epoch,
            restore_epoch=run.restore_epoch,
            claimant_id=str(claimant_id),
            engine_request_id=str(intent.engineRequestID),
            intent_json=intent.model_dump_json(),
            intent_digest=content_digest(intent.model_dump(mode="json")),
            created_at=now,
            updated_at=now,
        )
        claim = RunClaim(
            intent=intent,
            claimantID=claimant_id,
            runRevision=run.revision,
            dispatchRevision=row.revision,
            engineReceipt=None,
        )
        _event(services, conn, bound, "run.claimed", _payload(claim), now)
        return claim


def _begin_dispatch(
    services: SharedServices, claim: RunClaim
) -> EngineInvocation | EngineEntryReceipt:
    identity = _worker_identity(services)
    _epoch(identity, claim.intent)
    with _transaction(services) as conn:
        if not services.ac_command_repo.lock_owner(conn, claim.intent.ownerID):
            raise CommandAdmissionError("accessDenied")
        bound = _bound(services, conn, claim.intent.ownerID, claim.intent.runID, identity.boxID)
        current = validate_dispatch_binding(services, conn, bound)
        if current is None:
            raise StorageDataError("missing dispatch claim")
        _match(claim, current)
        if current.engineReceipt is not None:
            return current.engineReceipt
        _active(services, conn, bound)
        if (
            bound.run.dispatch_phase != "claimed"
            or current.runRevision != claim.runRevision
            or current.dispatchRevision != claim.dispatchRevision
        ):
            raise CommandAdmissionError("revisionConflict")
        now = int(datetime.now(UTC).timestamp() * 1000)
        run = services.ac_run_repo.mark_dispatching(
            conn,
            str(claim.intent.runID),
            claim.intent.ownerID,
            claim.runRevision,
            claim.intent.executionEpoch,
            updated_at=now,
        )
        if run is None or not services.ac_run_dispatch_repo.start_dispatch(
            conn, run.run_id, run.owner_id, current.dispatchRevision, updated_at=now
        ):
            raise CommandAdmissionError("revisionConflict")
        updated = current.model_copy(
            update={"runRevision": run.revision, "dispatchRevision": current.dispatchRevision + 1}
        )
        _event(services, conn, bound, "run.dispatching", _payload(updated), now)
        return EngineInvocation(
            intent=claim.intent, claimantID=claim.claimantID, expectedRunRevision=run.revision
        )


def accept_engine_entry(
    services: SharedServices, invocation: EngineInvocation
) -> EngineEntryResult:
    identity = _worker_identity(services)
    _epoch(identity, invocation.intent)
    owner_id, run_id = invocation.intent.ownerID, invocation.intent.runID
    with _transaction(services) as conn:
        if not services.ac_command_repo.lock_owner(conn, owner_id):
            raise CommandAdmissionError("accessDenied")
        bound = _bound(services, conn, owner_id, run_id, identity.boxID)
        current = validate_dispatch_binding(services, conn, bound)
        if (
            current is None
            or current.intent != invocation.intent
            or current.claimantID != invocation.claimantID
        ):
            raise CommandAdmissionError("revisionConflict")
        if current.engineReceipt is not None:
            return EngineEntryResult(receipt=current.engineReceipt, replayed=True)
        _active(services, conn, bound)
        if (
            bound.run.dispatch_phase != "dispatching"
            or current.runRevision != invocation.expectedRunRevision
        ):
            raise CommandAdmissionError("revisionConflict")
        now = datetime.now(UTC)
        timestamp = int(now.timestamp() * 1000)
        receipt = EngineEntryReceipt(
            schemaVersion=1,
            receiptID=uuid4(),
            dispatchID=invocation.intent.dispatchID,
            engineRequestID=invocation.intent.engineRequestID,
            runID=run_id,
            turnID=invocation.intent.turnID,
            restoreEpoch=invocation.intent.restoreEpoch,
            executionEpoch=invocation.intent.executionEpoch,
            intentDigest=content_digest(invocation.intent.model_dump(mode="json")),
            acceptedAt=now,
        )
        run = services.ac_run_repo.mark_engine_accepted(
            conn,
            str(run_id),
            owner_id,
            current.runRevision,
            invocation.intent.executionEpoch,
            updated_at=timestamp,
        )
        if run is None or not services.ac_run_dispatch_repo.record_acceptance(
            conn,
            str(run_id),
            owner_id,
            current.dispatchRevision,
            engine_receipt_id=str(receipt.receiptID),
            engine_receipt_json=receipt.model_dump_json(),
            engine_receipt_digest=content_digest(receipt.model_dump(mode="json")),
            engine_accepted_at=timestamp,
            updated_at=timestamp,
        ):
            raise CommandAdmissionError("revisionConflict")
        _event(
            services,
            conn,
            bound,
            "run.engineAccepted",
            {
                **_payload(current.model_copy(update={"runRevision": run.revision})),
                "receiptID": str(receipt.receiptID),
            },
            timestamp,
        )
        return EngineEntryResult(receipt=receipt, replayed=False)


def validate_engine_execution(services: SharedServices, invocation: EngineInvocation) -> None:
    identity = _worker_identity(services)
    _epoch(identity, invocation.intent)
    with _transaction(services) as conn:
        if not services.ac_command_repo.lock_owner(conn, invocation.intent.ownerID):
            raise CommandAdmissionError("accessDenied")
        bound = _bound(
            services, conn, invocation.intent.ownerID, invocation.intent.runID, identity.boxID
        )
        claim = validate_dispatch_binding(services, conn, bound)
        if (
            claim is None
            or claim.intent != invocation.intent
            or claim.claimantID != invocation.claimantID
        ):
            raise CommandAdmissionError("revisionConflict")
        _active(services, conn, bound)
        if claim.engineReceipt is None or bound.run.dispatch_phase != "engineAccepted":
            raise CommandAdmissionError("revisionConflict")


def interrupt_uncertain_run(services: SharedServices, *, claim: RunClaim) -> RunClaim:
    # Recovery does not require current execution availability; it only tightens
    # the existing lane and never starts/requeues/releases anything.
    identity = get_or_create_identity(services.paths.agentcomputer_identity)
    if identity.boxID != claim.intent.boxID:
        raise StorageDataError("recovery installation mismatch")
    with _transaction(services) as conn:
        # Use the same owner -> Run order as entry confirmation. The owner lock
        # also prevents PostgreSQL FK checks from creating the opposite order.
        if not services.ac_command_repo.lock_owner_for_recovery(conn, claim.intent.ownerID):
            raise CommandAdmissionError("resourceNotFound")
        # Lock before reading so a late entry confirmation cannot invalidate the
        # recovery CAS. Disabled owners still need this tightening after handoff.
        if not services.ac_run_repo.lock_for_update(
            conn, str(claim.intent.runID), claim.intent.ownerID
        ):
            raise CommandAdmissionError("resourceNotFound")
        bound = _bound(services, conn, claim.intent.ownerID, claim.intent.runID, identity.boxID)
        current = validate_dispatch_binding(services, conn, bound)
        if current is None:
            raise StorageDataError("missing interrupted dispatch")
        _match(claim, current)
        if bound.run.state == "interrupted" and bound.run.reconciliation_required:
            return current
        if bound.run.state in {"succeeded", "failed", "stopped"}:
            return current
        if bound.run.dispatch_phase not in {"dispatching", "engineAccepted"}:
            raise CommandAdmissionError("revisionConflict")
        now = int(datetime.now(UTC).timestamp() * 1000)
        run = services.ac_run_repo.mark_interrupted(
            conn,
            bound.run.run_id,
            bound.run.owner_id,
            current.runRevision,
            current.intent.executionEpoch,
            updated_at=now,
        )
        if run is None:
            raise CommandAdmissionError("revisionConflict")
        updated = current.model_copy(update={"runRevision": run.revision})
        _event(
            services,
            conn,
            bound,
            "run.interrupted",
            {
                **_payload(updated),
                "reasonCode": "dispatchOutcomeUnknown",
                "reconciliationRequired": True,
            },
            now,
        )
        return updated


async def dispatch_run(
    services: SharedServices,
    *,
    claim: RunClaim,
    invoke: Callable[[EngineInvocation], Awaitable[None]],
) -> EngineEntryReceipt:
    # Cancelling to_thread does not stop its transaction. Observe the committed
    # result before deciding whether recovery is required, and never hand off a
    # cancelled request whose dispatching fact committed in the background.
    preparation = asyncio.create_task(asyncio.to_thread(_begin_dispatch, services, claim))
    cancelled = False
    while not preparation.done():
        try:
            await asyncio.shield(preparation)
        except asyncio.CancelledError:
            cancelled = True
    prepared = preparation.result()
    if isinstance(prepared, EngineEntryReceipt):
        if cancelled:
            raise asyncio.CancelledError
        return prepared
    try:
        if cancelled:
            raise asyncio.CancelledError
        await invoke(prepared)
        current = await asyncio.to_thread(
            get_claim, services, owner_id=claim.intent.ownerID, run_id=claim.intent.runID
        )
        if current is None or current.engineReceipt is None:
            raise StorageDataError("engine entry returned without durable receipt")
        return current.engineReceipt
    except BaseException:
        # The dispatching commit preceded handoff; an exception cannot prove the
        # engine did nothing. Persist uncertainty even on request cancellation.
        async def fence() -> None:
            current = await asyncio.to_thread(
                get_claim, services, owner_id=claim.intent.ownerID, run_id=claim.intent.runID
            )
            if current is not None:
                await asyncio.to_thread(interrupt_uncertain_run, services, claim=current)

        task = asyncio.create_task(fence())
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        task.result()
        raise
