"""Trusted Run completion after independently bound archive and checkpoint proof."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import Field, StrictBool, StrictStr, ValidationError, field_validator

from octop.infra.agentcomputer.commands import (
    CommandAdmissionError,
    StorageDataError,
    canonical_json,
    content_digest,
)
from octop.infra.agentcomputer.dispatch import (
    _epoch,
    _event,
    _existing_recovery_identity,
    _transaction,
    validate_dispatch_binding,
)
from octop.infra.agentcomputer.effects import _record, validate_effect_bindings
from octop.infra.agentcomputer.models import (
    ControlModel,
    EffectDigest,
    EngineInvocation,
    PositiveRevision,
    ToolEffectRecord,
)
from octop.infra.agentcomputer.pauses import validate_pause_bindings
from octop.infra.agentcomputer.runs import BoundRun, load_bound_run
from octop.infra.db.services import SharedServices


class _CompletionEvidenceBase(ControlModel):
    """Observations supplied by the trusted stream/history/checkpoint boundary."""

    historyTurnID: UUID
    historyThreadID: StrictStr = Field(min_length=1)
    historyAgentID: StrictStr = Field(min_length=1)
    historyStatus: Literal["complete"]
    historyDigest: EffectDigest
    historyAssistantMessageID: StrictStr = Field(min_length=1)
    historyAssistantContentDigest: EffectDigest
    terminalCheckpointID: UUID
    terminalCheckpointDigest: EffectDigest
    terminalAssistantMessageID: StrictStr = Field(min_length=1)
    terminalAssistantContentDigest: EffectDigest
    engineStreamEndedAt: datetime

    @field_validator("engineStreamEndedAt")
    @classmethod
    def utc_stream_end(cls, value: datetime) -> datetime:
        if value.utcoffset() != timedelta(0):
            raise ValueError("stream end must be UTC")
        return value


class CompletionEvidence(_CompletionEvidenceBase):
    schemaVersion: Literal[1]


class ToolCompletionEvidence(_CompletionEvidenceBase):
    """The one read-only tool result and final answer bound to the same Run."""

    schemaVersion: Literal[2]
    toolEffectID: UUID
    toolReceiptDigest: EffectDigest
    toolCallID: StrictStr = Field(min_length=1)
    toolName: Literal["read_project_document"]
    toolArgumentsDigest: EffectDigest
    toolMessageID: StrictStr = Field(min_length=1)
    toolContentDigest: EffectDigest


class RunCompletionReceipt(ControlModel):
    """Immutable successful completion, separate from the original queued receipt."""

    schemaVersion: Literal[1]
    receiptID: UUID
    boxID: UUID
    ownerID: PositiveRevision
    projectID: UUID
    taskID: UUID
    runID: UUID
    turnID: UUID
    dispatchID: UUID
    engineRequestID: UUID
    claimantID: UUID
    restoreEpoch: PositiveRevision
    executionEpoch: PositiveRevision
    engineReceiptID: UUID
    dispatchIntentDigest: EffectDigest
    entryExpectedRunRevision: PositiveRevision
    resultRunRevision: PositiveRevision
    evidence: CompletionEvidence | ToolCompletionEvidence
    completedAt: datetime
    replayed: StrictBool

    @field_validator("completedAt")
    @classmethod
    def utc_completion(cls, value: datetime) -> datetime:
        if value.utcoffset() != timedelta(0):
            raise ValueError("completion time must be UTC")
        return value


def _tool_fields(evidence: ToolCompletionEvidence) -> dict[str, str]:
    return {
        "toolCallID": evidence.toolCallID,
        "toolName": evidence.toolName,
        "toolArgumentsDigest": evidence.toolArgumentsDigest,
        "toolMessageID": evidence.toolMessageID,
        "toolContentDigest": evidence.toolContentDigest,
    }


def _validate_evidence(
    evidence: CompletionEvidence | ToolCompletionEvidence, invocation: EngineInvocation
) -> None:
    from octop.infra.agentcomputer.engine import _checkpoint_metadata

    intent = invocation.intent
    tool_fields = _tool_fields(evidence) if isinstance(evidence, ToolCompletionEvidence) else {}
    expected_history_digest = content_digest(
        {
            "schemaVersion": evidence.schemaVersion,
            "turnID": str(intent.turnID),
            "threadID": intent.snapshot.octopThreadID,
            "agentID": intent.snapshot.agentID,
            "status": "complete",
            "assistantMessageID": evidence.historyAssistantMessageID,
            "assistantContentDigest": evidence.historyAssistantContentDigest,
            **tool_fields,
        }
    )
    expected_checkpoint_digest = content_digest(
        {
            "schemaVersion": evidence.schemaVersion,
            "checkpointID": str(evidence.terminalCheckpointID),
            "checkpointNamespace": "",
            "metadata": _checkpoint_metadata(invocation),
            "assistantMessageID": evidence.terminalAssistantMessageID,
            "assistantContentDigest": evidence.terminalAssistantContentDigest,
            **tool_fields,
        }
    )
    if (
        evidence.model_fields_set != set(type(evidence).model_fields)
        or evidence.historyTurnID != intent.turnID
        or evidence.historyThreadID != intent.snapshot.octopThreadID
        or evidence.historyAgentID != intent.snapshot.agentID
        or evidence.historyAssistantMessageID != evidence.terminalAssistantMessageID
        or evidence.historyAssistantContentDigest != evidence.terminalAssistantContentDigest
        or evidence.historyDigest != expected_history_digest
        or evidence.terminalCheckpointDigest != expected_checkpoint_digest
    ):
        raise CommandAdmissionError("revisionConflict")


def _validated_tool_effect(
    services: SharedServices,
    conn: Any,
    invocation: EngineInvocation,
    evidence: ToolCompletionEvidence,
) -> ToolEffectRecord:
    rows = services.ac_tool_effect_repo.list_for_run_in_transaction(
        conn, str(invocation.intent.runID), invocation.intent.ownerID
    )
    if len(rows) != 1:
        raise CommandAdmissionError("revisionConflict")
    record = _record(rows[0], invocation)
    receipt = record.receipt
    output = receipt.output if receipt is not None else None
    if (
        record.phase != "terminal"
        or receipt is None
        or receipt.outcome != "succeeded"
        or receipt.reconciliationRequired
        or output is None
        or output.outcome != "succeeded"
        or output.fileChangeIDs
        or output.artifactIDs
        or record.intent.effectID != evidence.toolEffectID
        or content_digest(receipt.model_dump(mode="json")) != evidence.toolReceiptDigest
        or record.intent.action.toolCallID != evidence.toolCallID
        or record.intent.action.toolName != evidence.toolName
        or record.intent.action.argumentsDigest != evidence.toolArgumentsDigest
        or content_digest(output.resultJSON) != evidence.toolContentDigest
    ):
        raise CommandAdmissionError("revisionConflict")
    return record


def _payload(receipt: RunCompletionReceipt) -> dict[str, Any]:
    return {
        "receiptID": str(receipt.receiptID),
        "receiptDigest": content_digest(receipt.model_dump(mode="json")),
        "runID": str(receipt.runID),
        "state": "succeeded",
        "runRevision": receipt.resultRunRevision,
        "reconciliationRequired": False,
    }


def validate_completion_binding(
    services: SharedServices, conn: Any, bound: BoundRun, claim: Any
) -> RunCompletionReceipt | None:
    """Read-only validation of terminal success, its event and released lanes."""
    run = bound.run
    row = services.ac_run_terminal_repo.get_in_transaction(conn, run.run_id, run.owner_id)
    events = services.ac_event_repo.list_for_run_in_transaction(
        conn, run.run_id, run.owner_id, event_type="run.finished"
    )
    natural_events = []
    for event in events:
        try:
            payload = json.loads(event.payload_json)
        except (TypeError, ValueError) as exc:
            raise StorageDataError("invalid Run finished event") from exc
        if isinstance(payload, dict) and payload.get("state") == "succeeded":
            natural_events.append(event)
    if row is None:
        if run.state == "succeeded" or natural_events:
            raise StorageDataError("missing natural completion receipt")
        return None
    try:
        receipt = RunCompletionReceipt.model_validate_json(row.receipt_json)
        if receipt.model_fields_set != set(RunCompletionReceipt.model_fields):
            raise StorageDataError("incomplete natural completion receipt")
        _validate_evidence(
            receipt.evidence,
            EngineInvocation(
                intent=claim.intent,
                claimantID=claim.claimantID,
                expectedRunRevision=receipt.entryExpectedRunRevision,
            ),
        )
    except (ValueError, AttributeError, ValidationError, CommandAdmissionError) as exc:
        raise StorageDataError("invalid natural completion receipt") from exc
    if (
        row.pk < 1
        or row.schema_version != 1
        or run.state != "succeeded"
        or run.dispatch_phase != "engineAccepted"
        or run.reconciliation_required
        or claim is None
        or claim.engineReceipt is None
        or row.receipt_id != str(receipt.receiptID)
        or row.run_id != run.run_id
        or row.turn_id != run.turn_id
        or row.task_id != run.task_id
        or row.project_id != run.project_id
        or row.owner_id != run.owner_id
        or row.dispatch_id != str(receipt.dispatchID)
        or row.engine_request_id != str(receipt.engineRequestID)
        or row.claimant_id != str(receipt.claimantID)
        or row.restore_epoch != run.restore_epoch
        or row.execution_epoch != run.execution_epoch
        or row.receipt_json != canonical_json(receipt.model_dump(mode="json"))
        or row.receipt_digest != content_digest(receipt.model_dump(mode="json"))
        or row.completed_at != int(receipt.completedAt.timestamp() * 1000)
        or run.updated_at != row.completed_at
        or run.revision != receipt.resultRunRevision
        or receipt.resultRunRevision != receipt.entryExpectedRunRevision + 2
        or receipt.replayed
        or receipt.boxID != bound.receipt.boxID
        or receipt.ownerID != run.owner_id
        or receipt.projectID != UUID(run.project_id)
        or receipt.taskID != UUID(run.task_id)
        or receipt.runID != UUID(run.run_id)
        or receipt.turnID != UUID(run.turn_id)
        or receipt.dispatchID != claim.intent.dispatchID
        or receipt.engineRequestID != claim.intent.engineRequestID
        or receipt.claimantID != claim.claimantID
        or receipt.restoreEpoch != claim.intent.restoreEpoch
        or receipt.executionEpoch != claim.intent.executionEpoch
        or receipt.engineReceiptID != claim.engineReceipt.receiptID
        or receipt.dispatchIntentDigest != content_digest(claim.intent.model_dump(mode="json"))
        or row.completed_at < int(claim.engineReceipt.acceptedAt.timestamp() * 1000)
        or row.completed_at < int(receipt.evidence.engineStreamEndedAt.timestamp() * 1000)
        or len(events) != 1
        or len(natural_events) != 1
        or services.ac_run_pause_repo.list_for_run_in_transaction(conn, run.run_id, run.owner_id)
        or services.ac_run_cancellation_repo.get_in_transaction(conn, run.run_id, run.owner_id)
        is not None
    ):
        raise StorageDataError("natural completion binding mismatch")
    effect_rows = services.ac_tool_effect_repo.list_for_run_in_transaction(
        conn, run.run_id, run.owner_id
    )
    if isinstance(receipt.evidence, ToolCompletionEvidence):
        try:
            validate_effect_bindings(services, conn, bound, claim)
            _validated_tool_effect(
                services,
                conn,
                EngineInvocation(
                    intent=claim.intent,
                    claimantID=claim.claimantID,
                    expectedRunRevision=receipt.entryExpectedRunRevision,
                ),
                receipt.evidence,
            )
        except (CommandAdmissionError, ValueError) as exc:
            raise StorageDataError("natural completion tool binding mismatch") from exc
    elif effect_rows:
        raise StorageDataError("tool effect on no-tool completion")
    event = natural_events[0]
    accepted = services.ac_event_repo.list_for_run_in_transaction(
        conn, run.run_id, run.owner_id, event_type="run.engineAccepted"
    )
    if (
        len(accepted) != 1
        or accepted[0].sequence >= event.sequence
        or event.schema_version != 1
        or event.payload_version != 1
        or event.task_id != run.task_id
        or event.owner_id != run.owner_id
        or event.turn_id != run.turn_id
        or event.run_id != run.run_id
        or event.created_at != row.completed_at
        or event.payload_json != canonical_json(_payload(receipt))
    ):
        raise StorageDataError("natural completion event mismatch")
    task = services.ac_task_repo.get_in_transaction(conn, run.task_id, run.owner_id)
    project = services.ac_project_repo.get_in_transaction(conn, run.project_id, run.owner_id)
    if (
        task is None
        or project is None
        or task.active_run_id == run.run_id
        or project.active_run_id == run.run_id
    ):
        raise StorageDataError("natural completion retained an active lane")
    return receipt


def complete_run(
    services: SharedServices,
    invocation: EngineInvocation,
    evidence: CompletionEvidence | ToolCompletionEvidence,
) -> RunCompletionReceipt:
    """Atomically finish one trusted invocation after external proof."""
    _validate_evidence(evidence, invocation)
    identity = _existing_recovery_identity(services)
    _epoch(identity, invocation.intent)
    intent = invocation.intent
    with _transaction(services) as conn:
        if not services.ac_command_repo.lock_owner_for_recovery(conn, intent.ownerID):
            raise CommandAdmissionError("resourceNotFound")
        if not services.ac_run_repo.lock_for_update(conn, str(intent.runID), intent.ownerID):
            raise CommandAdmissionError("resourceNotFound")
        bound = load_bound_run(
            services, conn, owner_id=intent.ownerID, run_id=intent.runID, box_id=identity.boxID
        )
        if bound is None:
            raise CommandAdmissionError("resourceNotFound")
        claim = validate_dispatch_binding(services, conn, bound)
        validate_effect_bindings(services, conn, bound, claim)
        validate_pause_bindings(services, conn, bound, claim)
        from octop.infra.agentcomputer.cancellations import validate_cancellation_binding

        cancellation = validate_cancellation_binding(services, conn, bound, claim)
        if (
            claim is None
            or claim.intent != intent
            or claim.claimantID != invocation.claimantID
            or claim.engineReceipt is None
        ):
            raise CommandAdmissionError("revisionConflict")
        existing = validate_completion_binding(services, conn, bound, claim)
        if existing is not None:
            if (
                existing.evidence != evidence
                or existing.entryExpectedRunRevision != invocation.expectedRunRevision
            ):
                raise CommandAdmissionError("revisionConflict")
            return existing.model_copy(update={"replayed": True})
        run = bound.run
        tool_record = (
            _validated_tool_effect(services, conn, invocation, evidence)
            if isinstance(evidence, ToolCompletionEvidence)
            else None
        )
        if (
            cancellation is not None
            or run.restore_epoch != identity.restoreEpoch
            or run.reconciliation_required
            or run.state != "queued"
            or run.dispatch_phase != "engineAccepted"
            or run.revision != invocation.expectedRunRevision + 1
            or (
                tool_record is None
                and services.ac_tool_effect_repo.list_for_run_in_transaction(
                    conn, run.run_id, run.owner_id
                )
            )
            or services.ac_run_pause_repo.list_for_run_in_transaction(
                conn, run.run_id, run.owner_id
            )
        ):
            raise CommandAdmissionError("revisionConflict")
        timestamp = int(datetime.now(UTC).timestamp() * 1000)
        now = datetime.fromtimestamp(timestamp / 1000, UTC)
        if timestamp < int(evidence.engineStreamEndedAt.timestamp() * 1000) or timestamp < int(
            claim.engineReceipt.acceptedAt.timestamp() * 1000
        ):
            raise CommandAdmissionError("revisionConflict")
        if tool_record is None:
            succeeded = services.ac_run_repo.mark_succeeded_without_effects(
                conn,
                run.run_id,
                run.owner_id,
                run.revision,
                run.restore_epoch,
                run.execution_epoch,
                updated_at=timestamp,
            )
        else:
            succeeded = services.ac_run_repo.mark_succeeded_with_read_effect(
                conn,
                run.run_id,
                run.owner_id,
                run.revision,
                run.restore_epoch,
                run.execution_epoch,
                str(tool_record.intent.effectID),
                updated_at=timestamp,
            )
        if succeeded is None:
            raise CommandAdmissionError("revisionConflict")
        receipt = RunCompletionReceipt(
            schemaVersion=1,
            receiptID=uuid4(),
            boxID=identity.boxID,
            ownerID=run.owner_id,
            projectID=intent.projectID,
            taskID=intent.taskID,
            runID=intent.runID,
            turnID=intent.turnID,
            dispatchID=intent.dispatchID,
            engineRequestID=intent.engineRequestID,
            claimantID=invocation.claimantID,
            restoreEpoch=run.restore_epoch,
            executionEpoch=run.execution_epoch,
            engineReceiptID=claim.engineReceipt.receiptID,
            dispatchIntentDigest=content_digest(intent.model_dump(mode="json")),
            entryExpectedRunRevision=invocation.expectedRunRevision,
            resultRunRevision=succeeded.revision,
            evidence=evidence,
            completedAt=now,
            replayed=False,
        )
        services.ac_run_terminal_repo.insert(
            conn,
            receipt_id=str(receipt.receiptID),
            run_id=run.run_id,
            turn_id=run.turn_id,
            task_id=run.task_id,
            project_id=run.project_id,
            owner_id=run.owner_id,
            dispatch_id=str(intent.dispatchID),
            engine_request_id=str(intent.engineRequestID),
            claimant_id=str(invocation.claimantID),
            restore_epoch=run.restore_epoch,
            execution_epoch=run.execution_epoch,
            receipt_json=canonical_json(receipt.model_dump(mode="json")),
            receipt_digest=content_digest(receipt.model_dump(mode="json")),
            completed_at=timestamp,
        )
        _event(services, conn, bound, "run.finished", _payload(receipt), timestamp)
        if not services.ac_task_repo.release_run(
            conn, run.task_id, run.owner_id, run.run_id, updated_at=timestamp
        ) or not services.ac_project_repo.release_run(
            conn, run.project_id, run.owner_id, run.run_id, updated_at=timestamp
        ):
            raise CommandAdmissionError("activeRunConflict")
        return receipt
