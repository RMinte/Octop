"""Private tool journal; neither installs a tool adapter nor opens execution.

A trusted caller supplies fixed adapter inputs. The production harness still
rejects tools. Journal receipts report adapter results, not native exactly-once
execution, verified file rollback, published artifacts, or a completed Run.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from octop.infra.agentcomputer.capabilities import get_capabilities
from octop.infra.agentcomputer.commands import (
    CommandAdmissionError,
    StorageDataError,
    canonical_json,
    content_digest,
)
from octop.infra.agentcomputer.dispatch import (
    _active,
    _bound,
    _epoch,
    _event,
    _existing_recovery_identity,
    _tighten_dispatch,
    _transaction,
    validate_dispatch_binding,
)
from octop.infra.agentcomputer.models import (
    EngineInvocation,
    RunClaim,
    ToolEffectAction,
    ToolEffectIntent,
    ToolEffectOutput,
    ToolEffectPreparation,
    ToolEffectReceipt,
    ToolEffectRecord,
)
from octop.infra.agentcomputer.runs import BoundRun
from octop.infra.db.services import SharedServices

MAX_EFFECT_JSON_BYTES = 65_536


def _json_digest(text: str, digest: str) -> None:
    try:
        value = json.loads(text)
        valid = (
            isinstance(value, dict)
            and len(text.encode("utf-8")) <= MAX_EFFECT_JSON_BYTES
            and canonical_json(value) == text
            and content_digest(value) == digest
        )
    except (ValueError, TypeError, UnicodeError) as exc:
        raise StorageDataError("invalid canonical tool effect payload") from exc
    if not valid:
        raise StorageDataError("invalid canonical tool effect payload")


def _operation(invocation: EngineInvocation, action: ToolEffectAction) -> str:
    return content_digest(
        {
            "dispatchIntentDigest": content_digest(invocation.intent.model_dump(mode="json")),
            "claimantID": str(invocation.claimantID),
            "action": action.model_dump(mode="json"),
        }
    )


def _now() -> tuple[datetime, int]:
    timestamp = int(datetime.now(UTC).timestamp() * 1000)
    return datetime.fromtimestamp(timestamp / 1000, UTC), timestamp


def _utc(timestamp: int) -> datetime:
    try:
        return datetime.fromtimestamp(timestamp / 1000, UTC)
    except (ValueError, OverflowError, OSError) as exc:
        raise StorageDataError("invalid stored tool effect timestamp") from exc


def _current(
    services: SharedServices, conn: Any, invocation: EngineInvocation
) -> tuple[BoundRun, RunClaim]:
    identity = _existing_recovery_identity(services)
    intent = invocation.intent
    if identity.boxID != intent.boxID:
        raise StorageDataError("tool effect installation mismatch")
    if not services.ac_command_repo.lock_owner_for_recovery(conn, intent.ownerID):
        raise CommandAdmissionError("resourceNotFound")
    if not services.ac_run_repo.lock_for_update(conn, str(intent.runID), intent.ownerID):
        raise CommandAdmissionError("resourceNotFound")
    bound = _bound(services, conn, intent.ownerID, intent.runID, identity.boxID)
    claim = validate_dispatch_binding(services, conn, bound)
    if (
        claim is None
        or claim.intent != intent
        or claim.claimantID != invocation.claimantID
        or claim.engineReceipt is None
    ):
        raise CommandAdmissionError("revisionConflict")
    validate_effect_bindings(services, conn, bound, claim)
    return bound, claim


def _authorize_current(
    services: SharedServices, conn: Any, bound: BoundRun, claim: RunClaim
) -> None:
    identity = _existing_recovery_identity(services)
    _epoch(identity, claim.intent)
    if not services.ac_command_repo.lock_owner(conn, claim.intent.ownerID):
        raise CommandAdmissionError("accessDenied")
    profile = get_capabilities(services)
    if profile.boxID != identity.boxID or profile.restoreEpoch != identity.restoreEpoch:
        raise StorageDataError("tool effect capability identity mismatch")
    if (
        not profile.capabilities.runs.available
        or claim.intent.snapshot.request.permissionMode not in profile.supportedPermissionModes
    ):
        raise CommandAdmissionError("runtimeUnavailable", reason="toolExecutionUnavailable")
    _active(services, conn, bound, claim)


def _record(row: Any, invocation: EngineInvocation) -> ToolEffectRecord:
    intent = ToolEffectIntent.model_validate_json(row.intent_json)
    action = intent.action
    if intent.model_fields_set != set(
        ToolEffectIntent.model_fields
    ) or action.model_fields_set != set(ToolEffectAction.model_fields):
        raise StorageDataError("incomplete tool effect intent")
    _json_digest(action.argumentsJSON, action.argumentsDigest)
    source = invocation.intent
    binding = {
        "effect_id": str(intent.effectID),
        "run_id": str(source.runID),
        "turn_id": str(source.turnID),
        "task_id": str(source.taskID),
        "project_id": str(source.projectID),
        "owner_id": source.ownerID,
        "dispatch_id": str(source.dispatchID),
        "engine_request_id": str(source.engineRequestID),
        "claimant_id": str(invocation.claimantID),
        "restore_epoch": source.restoreEpoch,
        "execution_epoch": source.executionEpoch,
        "tool_call_id": action.toolCallID,
        "execution_attempt": 1,
        "schema_version": 1,
        "intent_digest": content_digest(intent.model_dump(mode="json")),
    }
    if (
        any(getattr(row, key) != value for key, value in binding.items())
        or intent.boxID != source.boxID
        or intent.ownerID != source.ownerID
        or intent.projectID != source.projectID
        or intent.taskID != source.taskID
        or intent.turnID != source.turnID
        or intent.runID != source.runID
        or intent.workspaceID != source.snapshot.workspaceID
        or intent.assistantMessageID != source.assistantMessageID
        or intent.dispatchID != source.dispatchID
        or intent.engineRequestID != source.engineRequestID
        or intent.claimantID != invocation.claimantID
        or intent.restoreEpoch != source.restoreEpoch
        or intent.executionEpoch != source.executionEpoch
        or intent.dispatchIntentDigest != content_digest(source.model_dump(mode="json"))
        or intent.operationDigest != _operation(invocation, action)
        or row.created_at < 0
        or row.updated_at < row.created_at
        or (row.dispatch_started_at is not None and row.dispatch_started_at < row.created_at)
        or (row.dispatch_started_at is not None and row.updated_at < row.dispatch_started_at)
    ):
        raise StorageDataError("tool effect intent binding mismatch")
    fields = (row.terminal_json, row.terminal_digest, row.terminal_at)
    receipt = (
        ToolEffectReceipt.model_validate_json(row.terminal_json)
        if row.terminal_json is not None
        else None
    )
    if receipt is not None:
        if (
            receipt.model_fields_set != set(ToolEffectReceipt.model_fields)
            or any(value is None for value in fields)
            or row.phase != "terminal"
            or row.terminal_digest != content_digest(receipt.model_dump(mode="json"))
            or row.terminal_at != int(receipt.completedAt.timestamp() * 1000)
            or row.terminal_at < row.created_at
            or row.updated_at != row.terminal_at
            or receipt.effectID != intent.effectID
            or receipt.runID != intent.runID
            or receipt.turnID != intent.turnID
            or receipt.restoreEpoch != intent.restoreEpoch
            or receipt.executionEpoch != intent.executionEpoch
            or receipt.intentDigest != row.intent_digest
            or receipt.operationDigest != intent.operationDigest
            or (receipt.outcome == "notDispatched") != (row.dispatch_started_at is None)
            or (row.dispatch_started_at is not None and row.terminal_at < row.dispatch_started_at)
        ):
            raise StorageDataError("invalid tool effect terminal receipt")
        if receipt.output is not None:
            if receipt.output.model_fields_set != set(ToolEffectOutput.model_fields):
                raise StorageDataError("incomplete tool effect output")
            _json_digest(receipt.output.resultJSON, receipt.output.resultDigest)
    elif any(value is not None for value in fields) or row.phase == "terminal":
        raise StorageDataError("missing tool effect terminal receipt")
    revisions = {
        "prepared": 1,
        "dispatching": 2,
        "terminal": 2 if row.dispatch_started_at is None else 3,
        "outcomeUnknown": 2 if row.dispatch_started_at is None else 3,
    }
    if (
        row.phase not in revisions
        or row.revision != revisions[row.phase]
        or (row.phase == "prepared" and row.dispatch_started_at is not None)
        or (row.phase == "dispatching" and row.dispatch_started_at is None)
    ):
        raise StorageDataError("invalid tool effect phase")
    return ToolEffectRecord(
        intent=intent,
        phase=row.phase,
        revision=row.revision,
        dispatchStartedAt=(
            _utc(row.dispatch_started_at) if row.dispatch_started_at is not None else None
        ),
        receipt=receipt,
        createdAt=_utc(row.created_at),
        updatedAt=_utc(row.updated_at),
    )


def validate_effect_bindings(
    services: SharedServices, conn: Any, bound: BoundRun, claim: RunClaim | None
) -> None:
    """Validate journal facts on the same connection as the original Run graph."""
    rows = services.ac_tool_effect_repo.list_for_run_in_transaction(
        conn, bound.run.run_id, bound.run.owner_id
    )
    if rows and (claim is None or claim.engineReceipt is None):
        raise StorageDataError("tool effect without engine acceptance")
    if claim is None:
        _validate_effect_events(services, conn, bound, [])
        return
    invocation = EngineInvocation(
        intent=claim.intent, claimantID=claim.claimantID, expectedRunRevision=claim.runRevision
    )
    records = []
    for row in rows:
        record = _record(row, invocation)
        records.append(record)
        assert claim.engineReceipt is not None
        if row.created_at < int(claim.engineReceipt.acceptedAt.timestamp() * 1000):
            raise StorageDataError("tool effect precedes engine acceptance")
        if record.phase != "terminal" and bound.run.state in {"succeeded", "failed", "stopped"}:
            raise StorageDataError("unresolved tool effect has a terminal Run")
        if record.phase != "terminal" or (
            record.receipt is not None and record.receipt.reconciliationRequired
        ):
            task = services.ac_task_repo.get_in_transaction(
                conn, bound.run.task_id, bound.run.owner_id
            )
            project = services.ac_project_repo.get_in_transaction(
                conn, bound.run.project_id, bound.run.owner_id
            )
            if (
                task is None
                or project is None
                or task.active_run_id != bound.run.run_id
                or project.active_run_id != bound.run.run_id
            ):
                raise StorageDataError("tool effect lost its active lanes")
        if (
            record.phase == "outcomeUnknown"
            or (record.receipt is not None and record.receipt.reconciliationRequired)
        ) and not bound.run.reconciliation_required:
            raise StorageDataError("tool effect uncertainty lost its Run fence")
    _validate_effect_events(services, conn, bound, records)


def _validate_effect_events(
    services: SharedServices, conn: Any, bound: BoundRun, records: list[ToolEffectRecord]
) -> None:
    """An orphan immutable event is missing journal evidence, never a new call."""
    by_id = {str(record.intent.effectID): record for record in records}
    events: dict[tuple[str, str], Any] = {}
    for phase in ("prepared", "dispatching", "terminal", "outcomeUnknown"):
        for event in services.ac_event_repo.list_for_run_in_transaction(
            conn, bound.run.run_id, bound.run.owner_id, event_type="tool." + phase
        ):
            payload = json.loads(event.payload_json)
            effect_id = payload.get("effectID") if isinstance(payload, dict) else None
            if not isinstance(effect_id, str) or effect_id not in by_id:
                raise StorageDataError("tool event is missing its original journal")
            key = (effect_id, phase)
            if key in events or event.schema_version != 1 or event.payload_version != 1:
                raise StorageDataError("invalid or duplicated tool effect event")
            events[key] = event
    for record in records:
        steps: list[tuple[str, int, ToolEffectReceipt | None, datetime]] = [
            ("prepared", 1, None, record.createdAt)
        ]
        if record.dispatchStartedAt is not None:
            steps.append(("dispatching", 2, None, record.dispatchStartedAt))
        if record.phase in {"terminal", "outcomeUnknown"}:
            steps.append((record.phase, record.revision, record.receipt, record.updatedAt))
        previous_sequence = 0
        for phase, revision, receipt, timestamp in steps:
            event = events.pop((str(record.intent.effectID), phase), None)
            expected = record.model_copy(
                update={"phase": phase, "revision": revision, "receipt": receipt}
            )
            if (
                event is None
                or event.owner_id != bound.run.owner_id
                or event.task_id != bound.run.task_id
                or event.turn_id != bound.run.turn_id
                or event.created_at != int(timestamp.timestamp() * 1000)
                or event.sequence <= previous_sequence
                or canonical_json(json.loads(event.payload_json))
                != canonical_json(_effect_payload(expected))
            ):
                raise StorageDataError("tool effect event binding mismatch")
            previous_sequence = event.sequence
    if events:
        raise StorageDataError("tool event contradicts its journal phase")


def _owned(
    services: SharedServices, conn: Any, invocation: EngineInvocation, effect_id: UUID
) -> ToolEffectRecord:
    row = services.ac_tool_effect_repo.get_in_transaction(
        conn, str(effect_id), invocation.intent.ownerID
    )
    if row is None:
        raise CommandAdmissionError("resourceNotFound")
    return _record(row, invocation)


def _effect_event(
    services: SharedServices,
    conn: Any,
    bound: BoundRun,
    record: ToolEffectRecord,
    timestamp: int,
) -> None:
    _event(
        services,
        conn,
        bound,
        "tool." + record.phase,
        _effect_payload(record),
        timestamp,
    )


def _effect_payload(record: ToolEffectRecord) -> dict[str, Any]:
    return {
        "effectID": str(record.intent.effectID),
        "toolCallID": record.intent.action.toolCallID,
        "operationDigest": record.intent.operationDigest,
        "phase": record.phase,
        "effectRevision": record.revision,
        "outcome": record.receipt.outcome if record.receipt is not None else None,
        "receiptDigest": (
            content_digest(record.receipt.model_dump(mode="json"))
            if record.receipt is not None
            else None
        ),
        "reconciliationRequired": record.phase == "outcomeUnknown"
        or (record.receipt is not None and record.receipt.reconciliationRequired),
    }


def _fence_run(services: SharedServices, conn: Any, bound: BoundRun, claim: RunClaim) -> None:
    if bound.run.state in {"succeeded", "failed", "stopped"}:
        raise StorageDataError("tool uncertainty cannot overwrite a terminal Run")
    if bound.run.state == "interrupted" and bound.run.reconciliation_required:
        return
    _tighten_dispatch(services, conn, bound, claim, "toolEffectOutcomeUnknown")


def prepare_effect(
    services: SharedServices, invocation: EngineInvocation, action: ToolEffectAction
) -> ToolEffectPreparation:
    """Commit the fixed intent before any callback; replay never grants dispatch."""
    _json_digest(action.argumentsJSON, action.argumentsDigest)
    with _transaction(services) as conn:
        bound, claim = _current(services, conn, invocation)
        row = services.ac_tool_effect_repo.get_by_call_in_transaction(
            conn,
            str(invocation.intent.runID),
            invocation.intent.ownerID,
            action.toolCallID,
            action.executionAttempt,
        )
        if row is not None:
            record = _record(row, invocation)
            if record.intent.operationDigest != _operation(invocation, action):
                raise CommandAdmissionError("revisionConflict")
            return ToolEffectPreparation(record=record, replayed=True)
        _authorize_current(services, conn, bound, claim)
        if (
            services.ac_tool_effect_repo.get_pending_in_transaction(
                conn, bound.run.run_id, bound.run.owner_id
            )
            is not None
        ):
            raise CommandAdmissionError("reconciliationRequired")
        source = invocation.intent
        intent = ToolEffectIntent(
            schemaVersion=1,
            effectID=uuid4(),
            boxID=source.boxID,
            ownerID=source.ownerID,
            projectID=source.projectID,
            taskID=source.taskID,
            turnID=source.turnID,
            runID=source.runID,
            workspaceID=source.snapshot.workspaceID,
            assistantMessageID=source.assistantMessageID,
            dispatchID=source.dispatchID,
            engineRequestID=source.engineRequestID,
            claimantID=invocation.claimantID,
            restoreEpoch=source.restoreEpoch,
            executionEpoch=source.executionEpoch,
            dispatchIntentDigest=content_digest(source.model_dump(mode="json")),
            operationDigest=_operation(invocation, action),
            action=action,
        )
        _, timestamp = _now()
        row = services.ac_tool_effect_repo.insert(
            conn,
            effect_id=str(intent.effectID),
            run_id=str(intent.runID),
            turn_id=str(intent.turnID),
            task_id=str(intent.taskID),
            project_id=str(intent.projectID),
            owner_id=intent.ownerID,
            dispatch_id=str(intent.dispatchID),
            engine_request_id=str(intent.engineRequestID),
            claimant_id=str(intent.claimantID),
            restore_epoch=intent.restoreEpoch,
            execution_epoch=intent.executionEpoch,
            tool_call_id=action.toolCallID,
            execution_attempt=action.executionAttempt,
            intent_json=intent.model_dump_json(),
            intent_digest=content_digest(intent.model_dump(mode="json")),
            created_at=timestamp,
            updated_at=timestamp,
        )
        if row is None:
            raise CommandAdmissionError("revisionConflict")
        record = _record(row, invocation)
        _effect_event(services, conn, bound, record, timestamp)
        return ToolEffectPreparation(record=record, replayed=False)


def begin_effect(
    services: SharedServices,
    invocation: EngineInvocation,
    effect_id: UUID,
    expected_revision: int,
) -> ToolEffectRecord:
    """Only this CAS winner may hand off once; the commit precedes the handler."""
    with _transaction(services) as conn:
        bound, claim = _current(services, conn, invocation)
        _authorize_current(services, conn, bound, claim)
        current = _owned(services, conn, invocation, effect_id)
        if current.phase != "prepared" or current.revision != expected_revision:
            raise CommandAdmissionError("revisionConflict")
        _, timestamp = _now()
        row = services.ac_tool_effect_repo.mark_dispatching(
            conn, str(effect_id), invocation.intent.ownerID, expected_revision, updated_at=timestamp
        )
        if row is None:
            raise CommandAdmissionError("revisionConflict")
        record = _record(row, invocation)
        _effect_event(services, conn, bound, record, timestamp)
        return record


def _finish(
    services: SharedServices,
    conn: Any,
    invocation: EngineInvocation,
    bound: BoundRun,
    claim: RunClaim,
    current: ToolEffectRecord,
    output: ToolEffectOutput | None,
    reason_code: Literal["authorizationDenied", "callerCancelled"] | None,
) -> ToolEffectReceipt:
    now, timestamp = _now()
    receipt = ToolEffectReceipt(
        schemaVersion=1,
        receiptID=uuid4(),
        effectID=current.intent.effectID,
        runID=current.intent.runID,
        turnID=current.intent.turnID,
        restoreEpoch=current.intent.restoreEpoch,
        executionEpoch=current.intent.executionEpoch,
        intentDigest=content_digest(current.intent.model_dump(mode="json")),
        operationDigest=current.intent.operationDigest,
        outcome=output.outcome if output is not None else "notDispatched",
        reasonCode=reason_code,
        output=output,
        reconciliationRequired=output is not None
        and (output.outcome == "failed" or output.effects == "unknown"),
        completedAt=now,
    )
    row = services.ac_tool_effect_repo.record_terminal(
        conn,
        str(current.intent.effectID),
        current.intent.ownerID,
        current.revision,
        expected_phase="prepared" if output is None else "dispatching",
        terminal_json=receipt.model_dump_json(),
        terminal_digest=content_digest(receipt.model_dump(mode="json")),
        terminal_at=timestamp,
        updated_at=timestamp,
    )
    if row is None:
        raise CommandAdmissionError("revisionConflict")
    record = _record(row, invocation)
    _effect_event(services, conn, bound, record, timestamp)
    if receipt.reconciliationRequired:
        _fence_run(services, conn, bound, claim)
    return receipt


def complete_effect(
    services: SharedServices,
    invocation: EngineInvocation,
    effect_id: UUID,
    output: ToolEffectOutput,
) -> ToolEffectReceipt:
    _json_digest(output.resultJSON, output.resultDigest)
    with _transaction(services) as conn:
        bound, claim = _current(services, conn, invocation)
        current = _owned(services, conn, invocation, effect_id)
        if current.receipt is not None:
            if current.receipt.output != output:
                raise CommandAdmissionError("revisionConflict")
            return current.receipt
        if current.phase != "dispatching":
            raise CommandAdmissionError("reconciliationRequired")
        return _finish(services, conn, invocation, bound, claim, current, output, None)


def reject_effect(
    services: SharedServices,
    invocation: EngineInvocation,
    effect_id: UUID,
    reason_code: Literal["authorizationDenied", "callerCancelled"],
) -> ToolEffectReceipt:
    with _transaction(services) as conn:
        bound, claim = _current(services, conn, invocation)
        current = _owned(services, conn, invocation, effect_id)
        if current.receipt is not None:
            if (
                current.receipt.outcome != "notDispatched"
                or current.receipt.reasonCode != reason_code
            ):
                raise CommandAdmissionError("revisionConflict")
            return current.receipt
        if current.phase != "prepared":
            raise CommandAdmissionError("revisionConflict")
        return _finish(services, conn, invocation, bound, claim, current, None, reason_code)


def _recover_current(
    services: SharedServices, conn: Any, owner_id: int, effect_id: UUID
) -> tuple[BoundRun, RunClaim, EngineInvocation, ToolEffectRecord]:
    initial = services.ac_tool_effect_repo.get_in_transaction(conn, str(effect_id), owner_id)
    if initial is None:
        raise CommandAdmissionError("resourceNotFound")
    identity = _existing_recovery_identity(services)
    if not services.ac_command_repo.lock_owner_for_recovery(conn, owner_id):
        raise StorageDataError("missing tool effect owner")
    if not services.ac_run_repo.lock_for_update(conn, initial.run_id, owner_id):
        raise StorageDataError("missing tool effect run")
    bound = _bound(services, conn, owner_id, UUID(initial.run_id), identity.boxID)
    claim = validate_dispatch_binding(services, conn, bound)
    if claim is None or claim.engineReceipt is None:
        raise StorageDataError("missing tool effect dispatch acceptance")
    validate_effect_bindings(services, conn, bound, claim)
    invocation = EngineInvocation(
        intent=claim.intent, claimantID=claim.claimantID, expectedRunRevision=claim.runRevision
    )
    return bound, claim, invocation, _owned(services, conn, invocation, effect_id)


def get_effect(
    services: SharedServices, *, owner_id: int, effect_id: UUID
) -> ToolEffectRecord | None:
    """Read existing facts, including old epochs and disabled owners; never repair."""
    with _transaction(services) as conn:
        if services.ac_tool_effect_repo.get_in_transaction(conn, str(effect_id), owner_id) is None:
            return None
        _, _, _, record = _recover_current(services, conn, owner_id, effect_id)
        return record


def interrupt_effect(
    services: SharedServices, *, owner_id: int, effect_id: UUID
) -> ToolEffectRecord:
    """Conservatively keep the lane; unknown is never converted into a late result."""
    return _interrupt_effect(services, owner_id=owner_id, effect_id=effect_id)[0]


def recover_effect(
    services: SharedServices, *, owner_id: int, effect_id: UUID, box_id: UUID
) -> bool:
    """Startup-only tightening, including Runs already fenced by dispatch recovery."""
    return _interrupt_effect(services, owner_id=owner_id, effect_id=effect_id, box_id=box_id)[1]


def _interrupt_effect(
    services: SharedServices,
    *,
    owner_id: int,
    effect_id: UUID,
    box_id: UUID | None = None,
) -> tuple[ToolEffectRecord, bool]:
    with _transaction(services) as conn:
        bound, claim, invocation, current = _recover_current(services, conn, owner_id, effect_id)
        if box_id is not None and current.intent.boxID != box_id:
            raise StorageDataError("tool effect recovery installation mismatch")
        if current.phase in {"terminal", "outcomeUnknown"}:
            return current, False
        _, timestamp = _now()
        row = services.ac_tool_effect_repo.mark_outcome_unknown(
            conn, str(effect_id), owner_id, current.revision, updated_at=timestamp
        )
        if row is None:
            raise CommandAdmissionError("revisionConflict")
        record = _record(row, invocation)
        _effect_event(services, conn, bound, record, timestamp)
        _fence_run(services, conn, bound, claim)
        return record, True
