"""Private, Run-bound approval and question journal.

Adjudication is a durable input fact. No production HITL route, engine resume,
tool execution, or authorization grant is installed by this module.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import ValidationError

from octop.infra.agentcomputer.capabilities import get_capabilities
from octop.infra.agentcomputer.commands import (
    CommandAdmissionError,
    StorageDataError,
    canonical_json,
    content_digest,
)
from octop.infra.agentcomputer.dispatch import (
    _event,
    _existing_recovery_identity,
    _tighten_dispatch,
    _transaction,
    validate_dispatch_binding,
)
from octop.infra.agentcomputer.effects import validate_effect_bindings
from octop.infra.agentcomputer.models import (
    ApprovalDecisionCommand,
    EngineInvocation,
    QuestionAnswerCommand,
    RunPauseContent,
    RunPauseDecisionReceipt,
    RunPauseIntent,
    RunPauseRecord,
    RunPauseStatus,
)
from octop.infra.agentcomputer.runs import BoundRun, load_bound_run
from octop.infra.db.repos.ac_run_pauses import AcRunPauseRow
from octop.infra.db.services import SharedServices

PauseKind = Literal["approval", "question"]
PauseCommand = ApprovalDecisionCommand | QuestionAnswerCommand
EVENT_TYPES = (
    "approval.requested",
    "approval.decided",
    "approval.expired",
    "question.asked",
    "question.answered",
    "question.expired",
)


def _decision_digest(command: PauseCommand) -> str:
    """A retry may replace transport identity without changing the decision."""
    body = command.model_dump(mode="json")
    del body["commandID"]
    del body["idempotencyKey"]
    return content_digest(body)


def _utc(value: int) -> datetime:
    try:
        return datetime.fromtimestamp(value / 1000, UTC)
    except (ValueError, OverflowError, OSError) as exc:
        raise StorageDataError("invalid stored pause timestamp") from exc


def _now() -> tuple[datetime, int]:
    value = int(datetime.now(UTC).timestamp() * 1000)
    return _utc(value), value


def _content(content: RunPauseContent) -> None:
    try:
        parsed = json.loads(content.payloadJSON)
        valid = (
            isinstance(parsed, dict)
            and len(content.payloadJSON.encode("utf-8")) <= 65_536
            and canonical_json(parsed) == content.payloadJSON
            and content_digest(parsed) == content.payloadDigest
        )
    except (ValueError, TypeError, UnicodeError) as exc:
        raise StorageDataError("invalid fixed pause content") from exc
    if not valid:
        raise StorageDataError("invalid fixed pause content")


def _record(row: AcRunPauseRow) -> RunPauseRecord:
    try:
        intent = RunPauseIntent.model_validate_json(row.intent_json)
        if intent.model_fields_set != set(
            RunPauseIntent.model_fields
        ) or intent.content.model_fields_set != set(RunPauseContent.model_fields):
            raise StorageDataError("incomplete pause intent")
        _content(intent.content)
        receipt = (
            RunPauseDecisionReceipt.model_validate_json(row.decision_receipt_json)
            if row.decision_receipt_json is not None
            else None
        )
        if receipt is not None and receipt.model_fields_set != set(
            RunPauseDecisionReceipt.model_fields
        ):
            raise StorageDataError("incomplete pause receipt")
        stored = RunPauseRecord.model_validate(
            {
                "intent": intent,
                "phase": row.phase,
                "revision": row.revision,
                "receipt": receipt,
                "createdAt": _utc(row.created_at),
                "updatedAt": _utc(row.updated_at),
            }
        )
    except (ValueError, ValidationError) as exc:
        raise StorageDataError("invalid stored pause") from exc
    request_values = (
        row.decision_command_id,
        row.decision_idempotency_key,
        row.decision_request_json,
        row.decision_request_digest,
        row.decision_receipt_json,
        row.decision_receipt_digest,
        row.decided_at,
    )
    decided = all(value is not None for value in request_values)
    if (
        row.schema_version != 1
        or row.pause_id != str(intent.pauseID)
        or row.kind != intent.kind
        or row.run_id != str(intent.runID)
        or row.turn_id != str(intent.turnID)
        or row.task_id != str(intent.taskID)
        or row.project_id != str(intent.projectID)
        or row.owner_id != intent.ownerID
        or row.dispatch_id != str(intent.dispatchID)
        or row.engine_request_id != str(intent.engineRequestID)
        or row.claimant_id != str(intent.claimantID)
        or row.restore_epoch != intent.restoreEpoch
        or row.execution_epoch != intent.executionEpoch
        or row.intent_digest != content_digest(intent.model_dump(mode="json"))
        or row.intent_json != canonical_json(intent.model_dump(mode="json"))
        or row.created_at > row.updated_at
        or row.expires_at != int(intent.expiresAt.timestamp() * 1000)
        or row.expires_at <= row.created_at
        or row.revision
        != (1 if row.phase == "pending" else 2 if row.phase == "decided" else (3 if decided else 2))
        or (row.phase == "pending" and any(value is not None for value in request_values))
        or (row.phase == "decided" and not decided)
        or (
            row.phase == "expired"
            and any(value is not None for value in request_values)
            and not decided
        )
    ):
        raise StorageDataError("pause journal binding mismatch")
    if decided:
        assert receipt is not None
        try:
            raw = json.loads(row.decision_request_json or "")
            command = (
                ApprovalDecisionCommand.model_validate(raw)
                if intent.kind == "approval"
                else QuestionAnswerCommand.model_validate(raw)
            )
        except (ValueError, ValidationError) as exc:
            raise StorageDataError("invalid pause decision request") from exc
        if (
            command.model_fields_set != set(type(command).model_fields)
            or row.decision_request_json != canonical_json(command.model_dump(mode="json"))
            or row.decision_request_digest != _decision_digest(command)
            or row.decision_receipt_json != canonical_json(receipt.model_dump(mode="json"))
            or row.decision_receipt_digest != content_digest(receipt.model_dump(mode="json"))
            or row.decision_command_id != str(command.commandID)
            or row.decision_idempotency_key != command.idempotencyKey
            or command.runID != intent.runID
            or command.expectedRestoreEpoch != intent.restoreEpoch
            or (
                getattr(command, "approvalID", None)
                if intent.kind == "approval"
                else getattr(command, "questionID", None)
            )
            != intent.pauseID
            or command.expectedRevision != 1
            or receipt.commandID != command.commandID
            or receipt.pauseID != intent.pauseID
            or receipt.kind != intent.kind
            or receipt.runID != intent.runID
            or receipt.turnID != intent.turnID
            or receipt.restoreEpoch != intent.restoreEpoch
            or receipt.executionEpoch != intent.executionEpoch
            or receipt.intentDigest != row.intent_digest
            or receipt.requestDigest != row.decision_request_digest
            or receipt.pauseRevision != 2
            or receipt.state != "adjudicatedOnly"
            or row.decided_at != int(receipt.decidedAt.timestamp() * 1000)
            or row.decided_at < row.created_at
        ):
            raise StorageDataError("pause decision binding mismatch")
    return stored


def _event_payload(record: RunPauseRecord) -> dict[str, Any]:
    return {
        "pauseID": str(record.intent.pauseID),
        "kind": record.intent.kind,
        "phase": record.phase,
        "pauseRevision": record.revision,
        "intentDigest": content_digest(record.intent.model_dump(mode="json")),
        "receiptDigest": (
            content_digest(record.receipt.model_dump(mode="json")) if record.receipt else None
        ),
    }


def _event_type(record: RunPauseRecord) -> str:
    if record.phase == "pending":
        return "approval.requested" if record.intent.kind == "approval" else "question.asked"
    if record.phase == "decided":
        return "approval.decided" if record.intent.kind == "approval" else "question.answered"
    return "approval.expired" if record.intent.kind == "approval" else "question.expired"


def _check_event_rows(
    services: SharedServices, conn: Any, bound: BoundRun, records: list[RunPauseRecord]
) -> None:
    rows = [
        event
        for event_type in EVENT_TYPES
        for event in services.ac_event_repo.list_for_run_in_transaction(
            conn, bound.run.run_id, bound.run.owner_id, event_type=event_type
        )
    ]
    by_pause: dict[str, list[Any]] = {}
    for event in rows:
        try:
            payload = json.loads(event.payload_json)
            pause_id = payload.get("pauseID") if isinstance(payload, dict) else None
        except (ValueError, TypeError) as exc:
            raise StorageDataError("invalid pause event payload") from exc
        if type(pause_id) is not str:
            raise StorageDataError("invalid pause event identity")
        by_pause.setdefault(pause_id, []).append(event)
    if set(by_pause) != {str(record.intent.pauseID) for record in records}:
        raise StorageDataError("orphaned or missing pause event")
    for record in records:
        events = sorted(by_pause[str(record.intent.pauseID)], key=lambda item: item.sequence)
        phases: list[RunPauseRecord] = [
            record.model_copy(update={"phase": "pending", "revision": 1, "receipt": None})
        ]
        if record.receipt is not None:
            phases.append(record.model_copy(update={"phase": "decided", "revision": 2}))
        if record.phase == "expired":
            phases.append(record)
        if len(events) != len(phases):
            raise StorageDataError("pause event count mismatch")
        last_sequence = 0
        for event, stage in zip(events, phases, strict=True):
            expected_at = (
                int(stage.createdAt.timestamp() * 1000)
                if stage.phase == "pending"
                else int(stage.receipt.decidedAt.timestamp() * 1000)
                if stage.phase == "decided" and stage.receipt is not None
                else int(stage.updatedAt.timestamp() * 1000)
            )
            if (
                event.event_type != _event_type(stage)
                or event.payload_version != 1
                or event.schema_version != 1
                or event.owner_id != bound.run.owner_id
                or event.task_id != bound.run.task_id
                or event.turn_id != bound.run.turn_id
                or event.run_id != bound.run.run_id
                or event.created_at != expected_at
                or event.sequence <= last_sequence
                or event.payload_json != canonical_json(_event_payload(stage))
            ):
                raise StorageDataError("pause event binding mismatch")
            last_sequence = event.sequence


def validate_pause_bindings(
    services: SharedServices, conn: Any, bound: BoundRun, claim: Any
) -> list[RunPauseRecord]:
    """Read-only check: missing rows or events never become a fresh request."""
    rows = services.ac_run_pause_repo.list_for_run_in_transaction(
        conn, bound.run.run_id, bound.run.owner_id
    )
    records = [_record(row) for row in rows]
    run = bound.run
    active = [record for record in records if record.phase in ("pending", "decided")]
    for row, record in zip(rows, records, strict=True):
        intent = record.intent
        if (
            claim is None
            or claim.engineReceipt is None
            or intent.boxID != bound.receipt.boxID
            or intent.workspaceID != bound.snapshot.workspaceID
            or intent.dispatchID != claim.intent.dispatchID
            or intent.engineRequestID != claim.intent.engineRequestID
            or intent.claimantID != claim.claimantID
            or intent.dispatchIntentDigest != content_digest(claim.intent.model_dump(mode="json"))
            or intent.ownerID != run.owner_id
            or intent.runID != UUID(run.run_id)
            or intent.turnID != UUID(run.turn_id)
            or intent.taskID != UUID(run.task_id)
            or intent.projectID != UUID(run.project_id)
            or intent.restoreEpoch != run.restore_epoch
            or intent.executionEpoch != run.execution_epoch
            or row.created_at < int(claim.engineReceipt.acceptedAt.timestamp() * 1000)
        ):
            raise StorageDataError("pause dispatch binding mismatch")
        if row.decision_command_id is not None and (
            services.ac_command_repo.get_global_in_transaction(conn, row.decision_command_id)
            is not None
            or services.ac_command_repo.get_by_key(
                conn, run.owner_id, row.decision_idempotency_key or ""
            )
            is not None
        ):
            raise StorageDataError("pause decision command identity collision")
    if len(active) > 1:
        raise StorageDataError("multiple active pauses")
    if active:
        pause = active[0]
        if run.state not in (
            "waitingApproval" if pause.intent.kind == "approval" else "waitingInput",
            "interrupted",
        ):
            raise StorageDataError("active pause Run state mismatch")
        if run.state == "interrupted" and not run.reconciliation_required:
            raise StorageDataError("interrupted pause lacks reconciliation")
        task = services.ac_task_repo.get_in_transaction(conn, run.task_id, run.owner_id)
        project = services.ac_project_repo.get_in_transaction(conn, run.project_id, run.owner_id)
        if (
            task is None
            or project is None
            or task.active_run_id != run.run_id
            or project.active_run_id != run.run_id
        ):
            raise StorageDataError("active pause lost Run lanes")
    elif run.state in ("waitingApproval", "waitingInput"):
        raise StorageDataError("waiting Run has no active pause")
    if any(record.phase == "expired" for record in records) and (
        run.state != "interrupted" or not run.reconciliation_required
    ):
        raise StorageDataError("expired pause has no Run fence")
    _check_event_rows(services, conn, bound, records)
    return records


def _current(
    services: SharedServices, conn: Any, *, owner_id: int, run_id: UUID
) -> tuple[BoundRun, Any]:
    identity = _existing_recovery_identity(services)
    if not services.ac_command_repo.lock_owner_for_recovery(conn, owner_id):
        raise CommandAdmissionError("resourceNotFound")
    if not services.ac_run_repo.lock_for_update(conn, str(run_id), owner_id):
        raise CommandAdmissionError("resourceNotFound")
    bound = load_bound_run(services, conn, owner_id=owner_id, run_id=run_id, box_id=identity.boxID)
    if bound is None:
        raise CommandAdmissionError("resourceNotFound")
    claim = validate_dispatch_binding(services, conn, bound)
    validate_effect_bindings(services, conn, bound, claim)
    validate_pause_bindings(services, conn, bound, claim)
    from octop.infra.agentcomputer.cancellations import validate_cancellation_binding

    validate_cancellation_binding(services, conn, bound, claim)
    return bound, claim


def open_pause(
    services: SharedServices,
    invocation: EngineInvocation,
    *,
    kind: PauseKind,
    content: RunPauseContent,
    expires_at: datetime,
) -> RunPauseRecord:
    """Trusted runtime-only pause admission; no public caller is installed."""
    _content(content)
    if expires_at.utcoffset() != UTC.utcoffset(None):
        raise ValueError("expires_at must be UTC")
    now_dt, now = _now()
    expiry = int(expires_at.timestamp() * 1000)
    if expiry <= now:
        raise CommandAdmissionError("revisionConflict")
    source = invocation.intent
    with _transaction(services) as conn:
        bound, claim = _current(services, conn, owner_id=source.ownerID, run_id=source.runID)
        if (
            claim is None
            or claim.engineReceipt is None
            or claim.intent != source
            or claim.claimantID != invocation.claimantID
        ):
            raise CommandAdmissionError("revisionConflict")
        if bound.run.revision != invocation.expectedRunRevision:
            raise CommandAdmissionError("revisionConflict")
        identity = _existing_recovery_identity(services)
        profile = get_capabilities(services)
        if identity.restoreEpoch != source.restoreEpoch:
            raise CommandAdmissionError("restoreEpochConflict")
        if not services.ac_command_repo.lock_owner(conn, source.ownerID):
            raise CommandAdmissionError("accessDenied")
        if (
            profile.boxID != identity.boxID
            or profile.restoreEpoch != identity.restoreEpoch
            or not profile.capabilities.runs.available
            or source.snapshot.request.permissionMode not in profile.supportedPermissionModes
        ):
            raise CommandAdmissionError("runtimeUnavailable", reason="approvalUnavailable")
        task = services.ac_task_repo.get_in_transaction(conn, bound.run.task_id, source.ownerID)
        project = services.ac_project_repo.get_in_transaction(
            conn, bound.run.project_id, source.ownerID
        )
        if (
            bound.run.state != "running"
            or bound.run.reconciliation_required
            or task is None
            or project is None
            or task.active_run_id != bound.run.run_id
            or project.active_run_id != bound.run.run_id
            or services.ac_run_pause_repo.get_by_run_in_transaction(
                conn, bound.run.run_id, source.ownerID
            )
            is not None
        ):
            raise CommandAdmissionError("reconciliationRequired")
        if (
            services.ac_tool_effect_repo.get_pending_in_transaction(
                conn, bound.run.run_id, source.ownerID
            )
            is not None
        ):
            raise CommandAdmissionError("reconciliationRequired")
        from octop.infra.agentcomputer.cancellations import validate_cancellation_binding

        if validate_cancellation_binding(services, conn, bound, claim) is not None:
            raise CommandAdmissionError("revisionConflict")
        intent = RunPauseIntent(
            schemaVersion=1,
            pauseID=uuid4(),
            kind=kind,
            boxID=identity.boxID,
            ownerID=source.ownerID,
            runID=source.runID,
            turnID=source.turnID,
            taskID=source.taskID,
            projectID=source.projectID,
            workspaceID=source.snapshot.workspaceID,
            dispatchID=source.dispatchID,
            engineRequestID=source.engineRequestID,
            claimantID=invocation.claimantID,
            restoreEpoch=source.restoreEpoch,
            executionEpoch=source.executionEpoch,
            dispatchIntentDigest=content_digest(source.model_dump(mode="json")),
            content=content,
            expiresAt=_utc(expiry),
        )
        row = services.ac_run_pause_repo.insert(
            conn,
            pause_id=str(intent.pauseID),
            kind=kind,
            run_id=str(source.runID),
            turn_id=str(source.turnID),
            task_id=str(source.taskID),
            project_id=str(source.projectID),
            owner_id=source.ownerID,
            dispatch_id=str(source.dispatchID),
            engine_request_id=str(source.engineRequestID),
            claimant_id=str(invocation.claimantID),
            restore_epoch=source.restoreEpoch,
            execution_epoch=source.executionEpoch,
            intent_json=canonical_json(intent.model_dump(mode="json")),
            intent_digest=content_digest(intent.model_dump(mode="json")),
            created_at=now,
            expires_at=expiry,
        )
        if row is None:
            raise CommandAdmissionError("revisionConflict")
        waiting = services.ac_run_repo.mark_waiting(
            conn,
            bound.run.run_id,
            source.ownerID,
            bound.run.revision,
            source.executionEpoch,
            state="waitingApproval" if kind == "approval" else "waitingInput",
            updated_at=now,
        )
        if waiting is None:
            raise CommandAdmissionError("revisionConflict")
        record = _record(row)
        _event(services, conn, bound, _event_type(record), _event_payload(record), now)
        return record


def validate_pause_read_binding(
    services: SharedServices, conn: Any, *, owner_id: int, row: AcRunPauseRow, box_id: UUID
) -> RunPauseRecord:
    """Validate an owned pause and its Run journals in the caller's transaction."""
    bound = load_bound_run(
        services, conn, owner_id=owner_id, run_id=UUID(row.run_id), box_id=box_id
    )
    if bound is None:
        raise StorageDataError("orphaned pause Run")
    claim = validate_dispatch_binding(services, conn, bound)
    validate_effect_bindings(services, conn, bound, claim)
    records = validate_pause_bindings(services, conn, bound, claim)
    from octop.infra.agentcomputer.cancellations import validate_cancellation_binding

    validate_cancellation_binding(services, conn, bound, claim)
    record = _record(row)
    if record not in records:
        raise StorageDataError("orphaned pause record")
    return record


def get_pause(services: SharedServices, *, owner_id: int, pause_id: UUID) -> RunPauseRecord | None:
    """Owned private query; historical decided/expired facts remain readable."""
    identity = _existing_recovery_identity(services)
    with _transaction(services) as conn:
        row = services.ac_run_pause_repo.get_in_transaction(conn, str(pause_id), owner_id)
        if row is None:
            return None
        return validate_pause_read_binding(
            services, conn, owner_id=owner_id, row=row, box_id=identity.boxID
        )


def get_run_pause_status(
    services: SharedServices, *, owner_id: int, run_id: UUID
) -> tuple[bool, RunPauseStatus | None]:
    """Read one owned Run's validated pause status without exposing its private intent."""
    with _transaction(services) as conn:
        # A missing or foreign Run is indistinguishable even if installation
        # identity is damaged; read it only after the owned row is found.
        if services.ac_run_repo.get_in_transaction(conn, str(run_id), owner_id) is None:
            return False, None
        identity = _existing_recovery_identity(services)
        bound = load_bound_run(
            services, conn, owner_id=owner_id, run_id=run_id, box_id=identity.boxID
        )
        if bound is None:
            raise StorageDataError("owned Run disappeared during pause lookup")
        claim = validate_dispatch_binding(services, conn, bound)
        validate_effect_bindings(services, conn, bound, claim)
        records = validate_pause_bindings(services, conn, bound, claim)
        from octop.infra.agentcomputer.cancellations import validate_cancellation_binding

        validate_cancellation_binding(services, conn, bound, claim)
        if len(records) > 1:
            raise StorageDataError("multiple Run pauses")
        if not records:
            return True, None
        record = records[0]
        return True, RunPauseStatus(
            pauseID=record.intent.pauseID,
            kind=record.intent.kind,
            runID=record.intent.runID,
            turnID=record.intent.turnID,
            phase=record.phase,
            revision=record.revision,
            restoreEpoch=record.intent.restoreEpoch,
            executionEpoch=record.intent.executionEpoch,
            expiresAt=record.intent.expiresAt,
            createdAt=record.createdAt,
            updatedAt=record.updatedAt,
            decisionCommandID=(record.receipt.commandID if record.receipt is not None else None),
        )


def _decision(
    services: SharedServices,
    *,
    owner_id: int,
    command: PauseCommand,
    kind: PauseKind,
    content: RunPauseContent,
    authorize: Callable[[RunPauseIntent], bool],
) -> RunPauseDecisionReceipt:
    _content(content)
    pause_id = (
        getattr(command, "approvalID", None)
        if kind == "approval"
        else getattr(command, "questionID", None)
    )
    if not isinstance(pause_id, UUID):
        raise ValueError("decision target kind mismatch")
    request_data = command.model_dump(mode="json")
    request_json = canonical_json(request_data)
    request_digest = _decision_digest(command)
    with _transaction(services) as conn:
        bound, claim = _current(services, conn, owner_id=owner_id, run_id=command.runID)
        if command.expectedRestoreEpoch != _existing_recovery_identity(services).restoreEpoch:
            raise CommandAdmissionError("restoreEpochConflict")
        row = services.ac_run_pause_repo.get_in_transaction(conn, str(pause_id), owner_id)
        if row is None:
            raise CommandAdmissionError("resourceNotFound")
        record = _record(row)
        intent = record.intent
        if kind != intent.kind or intent.runID != command.runID:
            raise CommandAdmissionError("revisionConflict")
        if row.decision_command_id is not None:
            if (
                row.decision_idempotency_key == command.idempotencyKey
                and row.decision_request_digest == request_digest
                and record.receipt is not None
            ):
                if command.commandID != record.receipt.commandID and (
                    services.ac_command_repo.get_global_in_transaction(conn, str(command.commandID))
                    is not None
                    or services.ac_run_pause_repo.command_exists_in_transaction(
                        conn, str(command.commandID)
                    )
                ):
                    raise CommandAdmissionError("revisionConflict")
                return record.receipt
            raise CommandAdmissionError("revisionConflict")
        if (
            claim is None
            or claim.engineReceipt is None
            or record.phase != "pending"
            or record.revision != command.expectedRevision
            or command.expectedRestoreEpoch != intent.restoreEpoch
            or _existing_recovery_identity(services).restoreEpoch != intent.restoreEpoch
            or bound.run.state != ("waitingApproval" if kind == "approval" else "waitingInput")
            or bound.run.reconciliation_required
            or _now()[1] >= row.expires_at
            or content != intent.content
        ):
            raise CommandAdmissionError("revisionConflict")
        if not services.ac_command_repo.lock_owner(conn, owner_id):
            raise CommandAdmissionError("accessDenied")
        identity = _existing_recovery_identity(services)
        profile = get_capabilities(services)
        if (
            profile.boxID != identity.boxID
            or profile.restoreEpoch != identity.restoreEpoch
            or not profile.capabilities.runs.available
            or bound.snapshot.request.permissionMode not in profile.supportedPermissionModes
        ):
            raise CommandAdmissionError("runtimeUnavailable", reason="approvalUnavailable")
        # A cancellation is a separate exact-Run command; it wins over a late decision.
        from octop.infra.agentcomputer.cancellations import validate_cancellation_binding

        if validate_cancellation_binding(services, conn, bound, claim) is not None:
            raise CommandAdmissionError("revisionConflict")
        if (
            services.ac_command_repo.get_global_in_transaction(conn, str(command.commandID))
            is not None
        ):
            raise CommandAdmissionError("revisionConflict")
        if services.ac_command_repo.get_by_key(conn, owner_id, command.idempotencyKey) is not None:
            raise CommandAdmissionError("revisionConflict")
        by_key = services.ac_run_pause_repo.get_by_key_in_transaction(
            conn, owner_id, command.idempotencyKey
        )
        if (
            services.ac_run_pause_repo.command_exists_in_transaction(conn, str(command.commandID))
            or by_key is not None
        ):
            raise CommandAdmissionError("revisionConflict")
        allowed = authorize(intent)
        if type(allowed) is not bool or not allowed:
            raise CommandAdmissionError("accessDenied")
        decided_at, now = _now()
        if now >= row.expires_at:
            raise CommandAdmissionError("revisionConflict")
        receipt = RunPauseDecisionReceipt(
            schemaVersion=1,
            receiptID=uuid4(),
            commandID=command.commandID,
            pauseID=intent.pauseID,
            kind=kind,
            runID=intent.runID,
            turnID=intent.turnID,
            restoreEpoch=intent.restoreEpoch,
            executionEpoch=intent.executionEpoch,
            intentDigest=row.intent_digest,
            requestDigest=request_digest,
            pauseRevision=record.revision + 1,
            state="adjudicatedOnly",
            decidedAt=decided_at,
        )
        decided = services.ac_run_pause_repo.record_decision(
            conn,
            pause_id=str(pause_id),
            owner_id=owner_id,
            expected_revision=record.revision,
            command_id=str(command.commandID),
            idempotency_key=command.idempotencyKey,
            request_json=request_json,
            request_digest=request_digest,
            receipt_json=canonical_json(receipt.model_dump(mode="json")),
            receipt_digest=content_digest(receipt.model_dump(mode="json")),
            decided_at=now,
        )
        if decided is None:
            raise CommandAdmissionError("revisionConflict")
        adjudicated = _record(decided)
        _event(services, conn, bound, _event_type(adjudicated), _event_payload(adjudicated), now)
        return receipt


def expire_pause(services: SharedServices, *, owner_id: int, pause_id: UUID) -> bool:
    """Fence a startup-pending pause; never resume a checkpoint from its decision."""
    with _transaction(services) as conn:
        row = services.ac_run_pause_repo.get_in_transaction(conn, str(pause_id), owner_id)
        if row is None:
            return False
        bound, claim = _current(services, conn, owner_id=owner_id, run_id=UUID(row.run_id))
        current = _record(row)
        if current.phase == "expired":
            return False
        if bound.run.state != "interrupted":
            if claim is None:
                raise StorageDataError("missing pause dispatch claim")
            _tighten_dispatch(services, conn, bound, claim, "pauseExpired")
        elif not bound.run.reconciliation_required:
            raise StorageDataError("interrupted pause lacks reconciliation")
        now = _now()[1]
        expired = services.ac_run_pause_repo.mark_expired(
            conn, str(pause_id), owner_id, current.revision, updated_at=now
        )
        if expired is None:
            raise CommandAdmissionError("revisionConflict")
        record = _record(expired)
        _event(services, conn, bound, _event_type(record), _event_payload(record), now)
        return True
