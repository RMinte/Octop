"""Durable, atomic registration without model calls or a queue consumer.

The guarded HTTP adapter shares this transaction kernel. Production admission
remains closed until full project readiness and managed engine dispatch are
available; isolated ready fixtures verify persistence without execution.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID, uuid4

from psycopg import Error as PostgresError
from pydantic import ValidationError

from octop.infra.agentcomputer.identity import ControlIdentity, get_or_create_identity
from octop.infra.agentcomputer.models import (
    CommandReceipt,
    NewMessageCommand,
    ProjectConfiguration,
    ProjectCreateReceipt,
    RunPauseDecisionReceipt,
    TaskCreateReceipt,
    TurnSnapshot,
)
from octop.infra.db.services import SharedServices

if TYPE_CHECKING:
    from octop.infra.agentcomputer.cancellations import CancelRunReceipt
    from octop.infra.db.repos.ac_commands import AcCommandRow


AdmissionErrorCode = Literal[
    "resourceNotFound",
    "accessDenied",
    "restoreEpochConflict",
    "idempotencyConflict",
    "revisionConflict",
    "activeRunConflict",
    "reconciliationRequired",
    "capabilityUnsupported",
    "runtimeUnavailable",
    "resourceConflict",
]


class CommandAdmissionError(Exception):
    def __init__(self, code: AdmissionErrorCode, *, reason: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.reason = reason


class StorageDataError(ValueError):
    """Stored control data failed validation; never regenerate its facts."""


class StorageUnavailableError(Exception):
    """The control database could not be read; the original request is unknown."""


def canonical_json(value: object) -> str:
    """Protocol v1: sorted object keys, UTF-8, exact strings and array order."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def content_digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def command_digest(*, box_id: UUID, task_id: UUID, command: NewMessageCommand) -> str:
    # Transport identities do not alter the semantic request. Epoch, target and
    # revision preconditions do: changing them is a new decision, not a retry.
    payload = command.model_dump(mode="json", exclude={"commandID", "idempotencyKey"})
    return _request_digest(box_id=box_id, task_id=task_id, payload=payload)


def _request_digest(*, box_id: UUID, task_id: UUID, payload: dict[str, Any]) -> str:
    return content_digest(
        {
            "digestVersion": 1,
            "protocolVersion": 1,
            "boxID": str(box_id),
            "operation": "turn.newMessage",
            "taskID": str(task_id),
            "payload": payload,
        }
    )


def _decode_receipt(raw: str, *, command_id: str, box_id: UUID, epoch: int) -> CommandReceipt:
    try:
        receipt = CommandReceipt.model_validate_json(raw)
    except ValidationError as exc:
        raise StorageDataError("invalid command receipt") from exc
    if (
        receipt.model_fields_set != set(CommandReceipt.model_fields)
        or str(receipt.commandID) != command_id
        or receipt.boxID != box_id
        or receipt.restoreEpoch != epoch
        or receipt.replayed
        or receipt.acceptedAt.tzinfo is None
    ):
        raise StorageDataError("inconsistent command receipt")
    return receipt


def decode_turn_receipt(
    raw: str, command_id: str, box_id: UUID, restore_epoch: int
) -> CommandReceipt:
    return _decode_receipt(raw, command_id=command_id, box_id=box_id, epoch=restore_epoch)


def validate_turn_receipt_binding(
    services: SharedServices,
    receipt: CommandReceipt,
    row: AcCommandRow,
    owner_id: int,
    *,
    conn: Any = None,
) -> None:
    """Validate the original acceptance graph without replacing it with current state."""
    try:
        if conn is None:
            with services.db.transaction() as connection:
                validate_turn_receipt_binding(services, receipt, row, owner_id, conn=connection)
            return
        task_id = str(receipt.taskID)
        timestamp = int(receipt.acceptedAt.timestamp() * 1000)
        task = services.ac_task_repo.get_in_transaction(conn, task_id, owner_id)
        turn = services.ac_turn_repo.get_in_transaction(conn, str(receipt.turnID), owner_id)
        run = services.ac_run_repo.get_in_transaction(conn, str(receipt.runID), owner_id)
        event = services.ac_event_repo.get_by_sequence_in_transaction(
            conn, task_id, owner_id, receipt.acceptedSequence
        )
        accepted_events = services.ac_event_repo.list_for_run_in_transaction(
            conn, str(receipt.runID), owner_id
        )
        if (
            row.owner_id != owner_id
            or row.command_id != str(receipt.commandID)
            or row.operation != "turn.newMessage"
            or row.schema_version != 1
            or row.target_id != task_id
            or row.restore_epoch != receipt.restoreEpoch
            or row.created_at != timestamp
            or receipt.replayed
            or task is None
            or turn is None
            or run is None
            or event is None
        ):
            raise StorageDataError("missing or inconsistent turn receipt binding")
        project = services.ac_project_repo.get_in_transaction(conn, task.project_id, owner_id)
        thread = services.thread_repo.get_in_transaction(conn, task.octop_thread_id or "")
        if project is None:
            raise StorageDataError("missing turn project")
        agent = services.agent_repo.get_in_transaction(conn, project.default_agent_id or "")
        if (
            project.schema_version != 1
            or project.box_id != str(receipt.boxID)
            or task.schema_version != 1
            or task.owner_id != owner_id
            or task.revision < receipt.taskRevision
            or task.next_sequence <= receipt.acceptedSequence
            or turn.schema_version != 1
            or turn.owner_id != owner_id
            or turn.context_mode != "newMessage"
            or turn.task_id != task_id
            or turn.project_id != project.project_id
            or turn.user_message_id != str(receipt.userMessageID)
            or turn.assistant_message_id != str(receipt.assistantMessageID)
            or turn.created_at != timestamp
            or run.schema_version != 1
            or run.owner_id != owner_id
            or run.task_id != task_id
            or run.project_id != project.project_id
            or run.turn_id != turn.turn_id
            or run.restore_epoch != receipt.restoreEpoch
            or run.created_at != timestamp
            or thread is None
            or thread.agent_id != project.default_agent_id
            or thread.user_id != owner_id
            or agent is None
            or agent.user_id != owner_id
        ):
            raise StorageDataError("turn receipt scope mismatch")
        try:
            snapshot = TurnSnapshot.model_validate_json(turn.snapshot_json)
            if (
                snapshot.model_fields_set != set(TurnSnapshot.model_fields)
                or snapshot.request.model_fields_set != set(type(snapshot.request).model_fields)
                or snapshot.projectConfiguration.model_fields_set
                != set(ProjectConfiguration.model_fields)
            ):
                raise StorageDataError("incomplete original snapshot")
            snapshot_data = snapshot.model_dump(mode="json")
            snapshot_digest = content_digest(snapshot_data)
            payload = json.loads(event.payload_json)
        except StorageDataError:
            raise
        except (ValueError, ValidationError) as exc:
            raise StorageDataError("invalid original turn snapshot or event") from exc
        if (
            turn.snapshot_digest != snapshot_digest
            or str(snapshot.projectID) != project.project_id
            or str(snapshot.workspaceID) != project.workspace_id
            or snapshot.agentID != project.default_agent_id
            or snapshot.octopThreadID != task.octop_thread_id
            or snapshot.projectRevision > project.revision
            or snapshot.request.expectedProjectRevision != snapshot.projectRevision
            or snapshot.request.expectedRestoreEpoch != receipt.restoreEpoch
            or snapshot.request.userMessageID != receipt.userMessageID
            or snapshot.request.permissionMode != "askForApproval"
            or snapshot.request.inputArtifactIDs
            or snapshot.request.selectedSkillIDs
            or _request_digest(
                box_id=receipt.boxID,
                task_id=receipt.taskID,
                payload=snapshot.request.model_dump(mode="json"),
            )
            != row.payload_digest
            or event.event_type != "run.accepted"
            or event.task_id != task_id
            or event.owner_id != owner_id
            or event.sequence != receipt.acceptedSequence
            or event.payload_version != 1
            or event.schema_version != 1
            or event.run_id != str(receipt.runID)
            or event.turn_id != str(receipt.turnID)
            or event.created_at != timestamp
            or len(accepted_events) != 1
            or accepted_events[0].event_id != event.event_id
        ):
            raise StorageDataError("original acceptance fact mismatch")
        expected_payload = {
            **receipt.model_dump(mode="json", exclude={"replayed"}),
            "configDigest": content_digest(snapshot.projectConfiguration.model_dump(mode="json")),
            "inputSnapshotDigest": snapshot_digest,
        }
        try:
            valid_payload = canonical_json(payload) == canonical_json(expected_payload)
        except ValueError as exc:
            raise StorageDataError("invalid accepted event payload") from exc
        if not valid_payload:
            raise StorageDataError("accepted event payload mismatch")
    except (sqlite3.DatabaseError, PostgresError) as exc:
        raise StorageUnavailableError("turn receipt storage unavailable") from exc
    except StorageDataError:
        raise
    except (ValueError, TypeError) as exc:
        raise StorageDataError("invalid stored turn receipt value") from exc


def get_command_receipt(
    services: SharedServices, owner_id: int, command_id: UUID
) -> (
    CommandReceipt
    | ProjectCreateReceipt
    | TaskCreateReceipt
    | CancelRunReceipt
    | RunPauseDecisionReceipt
    | None
):
    try:
        row = services.ac_command_repo.get(str(command_id), owner_id)
    except (sqlite3.DatabaseError, PostgresError) as exc:
        raise StorageUnavailableError("command storage unavailable") from exc
    except StorageDataError:
        raise
    except (ValueError, TypeError) as exc:
        raise StorageDataError("invalid stored command value") from exc
    if row is None:
        try:
            with services.db.transaction() as conn:
                pause_row = services.ac_run_pause_repo.get_by_command_in_transaction(
                    conn, str(command_id), owner_id
                )
                if pause_row is None:
                    return None
                from octop.infra.agentcomputer.dispatch import _existing_recovery_identity
                from octop.infra.agentcomputer.pauses import validate_pause_read_binding

                identity = _existing_recovery_identity(services)
                record = validate_pause_read_binding(
                    services, conn, owner_id=owner_id, row=pause_row, box_id=identity.boxID
                )
                if record.receipt is None or record.receipt.commandID != command_id:
                    raise StorageDataError("missing pause decision receipt")
                return record.receipt
        except (sqlite3.DatabaseError, PostgresError) as exc:
            raise StorageUnavailableError("pause decision receipt storage unavailable") from exc
        except StorageDataError:
            raise
        except (ValueError, TypeError) as exc:
            raise StorageDataError("invalid stored pause decision receipt value") from exc
    identity = get_or_create_identity(services.paths.agentcomputer_identity)
    try:
        with services.db.transaction() as conn:
            row = services.ac_command_repo.get_in_transaction(conn, str(command_id), owner_id)
            if row is None:
                return None
            if services.ac_run_pause_repo.command_exists_in_transaction(conn, str(command_id)):
                raise StorageDataError("command and pause decision identity collision")
            # A historic epoch is readable after a restore. The original result must
            # not be rewritten to the current epoch or treated as a fresh acceptance.
            if row.operation == "task.create":
                from octop.infra.agentcomputer.tasks import validate_task_receipt_binding

                task_receipt = decode_task_receipt(
                    row.receipt_json,
                    command_id=row.command_id,
                    box_id=identity.boxID,
                    epoch=row.restore_epoch,
                )
                if (
                    row.schema_version != 1
                    or row.target_id != str(task_receipt.taskID)
                    or row.created_at != int(task_receipt.acceptedAt.timestamp() * 1000)
                ):
                    raise StorageDataError("inconsistent task receipt metadata")
                validate_task_receipt_binding(services, owner_id, task_receipt, conn=conn)
                return task_receipt
            if row.operation == "project.create":
                project_receipt = decode_project_receipt(
                    row.receipt_json,
                    command_id=row.command_id,
                    box_id=identity.boxID,
                    epoch=row.restore_epoch,
                )
                if (
                    row.schema_version != 1
                    or row.target_id != str(project_receipt.projectID)
                    or row.created_at != int(project_receipt.acceptedAt.timestamp() * 1000)
                ):
                    raise StorageDataError("inconsistent project receipt metadata")
                validate_project_receipt_binding(services, owner_id, project_receipt, conn=conn)
                return project_receipt
            if row.operation == "run.cancel":
                from octop.infra.agentcomputer.cancellations import (
                    decode_cancel_receipt,
                    validate_cancel_command_binding,
                )

                cancel_receipt = decode_cancel_receipt(
                    row.receipt_json,
                    command_id=row.command_id,
                    box_id=identity.boxID,
                    epoch=row.restore_epoch,
                )
                validate_cancel_command_binding(services, conn, owner_id, row, cancel_receipt)
                return cancel_receipt
            receipt = _decode_receipt(
                row.receipt_json,
                command_id=row.command_id,
                box_id=identity.boxID,
                epoch=row.restore_epoch,
            )
            if (
                row.schema_version != 1
                or row.operation != "turn.newMessage"
                or row.target_id != str(receipt.taskID)
                or row.created_at != int(receipt.acceptedAt.timestamp() * 1000)
            ):
                raise StorageDataError("inconsistent command metadata")
            validate_turn_receipt_binding(services, receipt, row, owner_id, conn=conn)
            return receipt
    except (sqlite3.DatabaseError, PostgresError) as exc:
        raise StorageUnavailableError("command receipt storage unavailable") from exc
    except StorageDataError:
        raise
    except (ValueError, TypeError) as exc:
        raise StorageDataError("invalid stored command receipt value") from exc


def decode_task_receipt(
    raw: str, *, command_id: str, box_id: UUID, epoch: int
) -> TaskCreateReceipt:
    try:
        receipt = TaskCreateReceipt.model_validate_json(raw)
    except ValidationError as exc:
        raise StorageDataError("invalid task receipt") from exc
    if (
        receipt.model_fields_set != set(TaskCreateReceipt.model_fields)
        or str(receipt.commandID) != command_id
        or receipt.boxID != box_id
        or receipt.restoreEpoch != epoch
        or receipt.replayed
        or receipt.taskRevision != 1
    ):
        raise StorageDataError("inconsistent task receipt")
    return receipt


def decode_project_receipt(
    raw: str, *, command_id: str, box_id: UUID, epoch: int
) -> ProjectCreateReceipt:
    try:
        receipt = ProjectCreateReceipt.model_validate_json(raw)
    except ValidationError as exc:
        raise StorageDataError("invalid project receipt") from exc
    if (
        receipt.model_fields_set != set(ProjectCreateReceipt.model_fields)
        or str(receipt.commandID) != command_id
        or receipt.boxID != box_id
        or receipt.restoreEpoch != epoch
        or receipt.replayed
    ):
        raise StorageDataError("inconsistent project receipt")
    return receipt


def validate_project_receipt_binding(
    services: SharedServices, owner_id: int, receipt: ProjectCreateReceipt, *, conn: Any = None
) -> None:
    try:
        if conn is None:
            with services.db.connect() as connection:
                validate_project_receipt_binding(services, owner_id, receipt, conn=connection)
            return
        project = services.ac_project_repo.get_in_transaction(
            conn, str(receipt.projectID), owner_id
        )
        preparation = services.ac_project_preparation_repo.get_in_transaction(
            conn, str(receipt.projectID), owner_id
        )
    except (sqlite3.DatabaseError, PostgresError) as exc:
        raise StorageUnavailableError("project receipt storage unavailable") from exc
    if (
        project is None
        or preparation is None
        or project.box_id != str(receipt.boxID)
        or project.workspace_id != str(receipt.workspaceID)
        or preparation.preparation_id != str(receipt.preparationID)
        or preparation.restore_epoch != receipt.restoreEpoch
        or receipt.projectRevision != 1
    ):
        raise StorageDataError("project receipt binding mismatch")


def admit_new_message(
    services: SharedServices, *, owner_id: int, task_id: UUID, command: NewMessageCommand
) -> CommandReceipt:
    """Internal registration wrapper; no engine or production execution gate is implied."""
    identity = get_or_create_identity(services.paths.agentcomputer_identity)
    if command.expectedRestoreEpoch != identity.restoreEpoch:
        raise CommandAdmissionError("restoreEpochConflict")
    try:
        with services.db.transaction() as conn:
            if not services.ac_command_repo.lock_owner(conn, owner_id):
                raise CommandAdmissionError("accessDenied")
            receipt = admit_new_message_in_transaction(
                services,
                conn,
                owner_id=owner_id,
                task_id=task_id,
                command=command,
                identity=identity,
            )
    except StorageDataError:
        raise
    except (ValueError, TypeError) as exc:
        raise StorageDataError("invalid stored registration value") from exc
    return receipt


def admit_new_message_in_transaction(
    services: SharedServices,
    conn: Any,
    *,
    owner_id: int,
    task_id: UUID,
    command: NewMessageCommand,
    identity: ControlIdentity,
) -> CommandReceipt:
    """Register using the caller's transaction, verified epoch and locked owner.

    Production ingress validates its capabilities and managed mapping before
    calling this kernel. A returned receipt is visible only after the caller commits.
    """
    digest = command_digest(box_id=identity.boxID, task_id=task_id, command=command)
    try:
        original = services.ac_command_repo.get_by_key(conn, owner_id, command.idempotencyKey)
        same_id = services.ac_command_repo.get_global_in_transaction(conn, str(command.commandID))
    except StorageDataError:
        raise
    except (ValueError, TypeError) as exc:
        raise StorageDataError("invalid stored command value") from exc
    if same_id is not None and (
        same_id.owner_id != owner_id or same_id.idempotency_key != command.idempotencyKey
    ):
        raise CommandAdmissionError("idempotencyConflict")
    if original is not None:
        if original.payload_digest != digest:
            raise CommandAdmissionError("idempotencyConflict")
        receipt = _decode_receipt(
            original.receipt_json,
            command_id=original.command_id,
            box_id=identity.boxID,
            epoch=original.restore_epoch,
        )
        if (
            original.schema_version != 1
            or original.operation != "turn.newMessage"
            or original.target_id != str(receipt.taskID)
            or receipt.taskID != task_id
            or receipt.userMessageID != command.userMessageID
            or receipt.restoreEpoch != command.expectedRestoreEpoch
            or original.created_at != int(receipt.acceptedAt.timestamp() * 1000)
        ):
            raise StorageDataError("inconsistent command metadata")
        validate_turn_receipt_binding(services, receipt, original, owner_id, conn=conn)
        return receipt.model_copy(update={"replayed": True})
    if (
        command.inputArtifactIDs
        or command.selectedSkillIDs
        or command.permissionMode != "askForApproval"
    ):
        raise CommandAdmissionError("capabilityUnsupported")
    try:
        task = services.ac_task_repo.get_in_transaction(conn, str(task_id), owner_id)
        if task is None:
            raise CommandAdmissionError("resourceNotFound")
        project = services.ac_project_repo.get_in_transaction(conn, task.project_id, owner_id)
    except StorageDataError:
        raise
    except (ValueError, TypeError) as exc:
        raise StorageDataError("invalid stored task or project value") from exc
    if (
        project is None
        or project.box_id != str(identity.boxID)
        or project.schema_version != 1
        or task.schema_version != 1
    ):
        raise StorageDataError("invalid project binding")
    if (
        project.preparation_state != "ready"
        or task.preparation_state != "ready"
        or not project.default_agent_id
        or not task.octop_thread_id
    ):
        raise CommandAdmissionError("runtimeUnavailable", reason="preparationIncomplete")
    try:
        thread = services.thread_repo.get_in_transaction(conn, task.octop_thread_id)
        agent = services.agent_repo.get_in_transaction(conn, project.default_agent_id)
    except StorageDataError:
        raise
    except (ValueError, TypeError) as exc:
        raise StorageDataError("invalid stored thread or agent value") from exc
    if (
        thread is None
        or thread.agent_id != project.default_agent_id
        or thread.user_id != owner_id
        or agent is None
        or agent.user_id != owner_id
    ):
        raise StorageDataError("invalid turn scope")
    if project.revision != command.expectedProjectRevision:
        raise CommandAdmissionError("revisionConflict")
    if services.ac_turn_repo.has_user_message(
        conn, task.task_id, owner_id, str(command.userMessageID)
    ):
        raise CommandAdmissionError("idempotencyConflict", reason="userMessageAlreadyRegistered")
    active_id = task.active_run_id or project.active_run_id
    if active_id:
        try:
            active = services.ac_run_repo.get_in_transaction(conn, active_id, owner_id)
        except StorageDataError:
            raise
        except (ValueError, TypeError) as exc:
            raise StorageDataError("invalid stored active Run value") from exc
        if active is None:
            raise StorageDataError("invalid active run binding")
        raise CommandAdmissionError(
            "reconciliationRequired" if active.reconciliation_required else "activeRunConflict"
        )
    try:
        config = ProjectConfiguration.model_validate_json(project.config_json)
    except ValidationError as exc:
        raise StorageDataError("invalid project configuration") from exc
    if (
        config.model_fields_set != set(ProjectConfiguration.model_fields)
        or content_digest(config.model_dump(mode="json")) != project.config_digest
    ):
        raise StorageDataError("project configuration digest mismatch")
    now = datetime.now(UTC)
    timestamp = int(now.timestamp() * 1000)
    turn_id, run_id, assistant_id, event_id = (str(uuid4()) for _ in range(4))
    snapshot = {
        "schemaVersion": 1,
        "projectID": project.project_id,
        "projectRevision": project.revision,
        "workspaceID": project.workspace_id,
        "agentID": project.default_agent_id,
        "octopThreadID": task.octop_thread_id,
        "projectConfiguration": config.model_dump(mode="json"),
        "request": command.model_dump(mode="json", exclude={"commandID", "idempotencyKey"}),
    }
    services.ac_turn_repo.insert(
        conn,
        turn_id=turn_id,
        task_id=task.task_id,
        project_id=project.project_id,
        owner_id=owner_id,
        user_message_id=str(command.userMessageID),
        assistant_message_id=assistant_id,
        context_mode="newMessage",
        snapshot_json=canonical_json(snapshot),
        snapshot_digest=content_digest(snapshot),
        created_at=timestamp,
    )
    services.ac_run_repo.insert(
        conn,
        run_id=run_id,
        turn_id=turn_id,
        task_id=task.task_id,
        project_id=project.project_id,
        owner_id=owner_id,
        state="queued",
        dispatch_phase="notDispatched",
        execution_epoch=0,
        restore_epoch=identity.restoreEpoch,
        revision=1,
        reconciliation_required=False,
        created_at=timestamp,
        updated_at=timestamp,
    )
    if not services.ac_project_repo.acquire_run(
        conn, project.project_id, owner_id, run_id, project.revision, updated_at=timestamp
    ):
        raise CommandAdmissionError("activeRunConflict")
    if not services.ac_task_repo.acquire_run(
        conn, task.task_id, owner_id, run_id, updated_at=timestamp
    ):
        raise CommandAdmissionError("activeRunConflict")
    receipt = CommandReceipt(
        boxID=identity.boxID,
        commandID=command.commandID,
        acceptedAt=now,
        restoreEpoch=identity.restoreEpoch,
        taskID=task_id,
        turnID=UUID(turn_id),
        runID=UUID(run_id),
        userMessageID=command.userMessageID,
        assistantMessageID=UUID(assistant_id),
        taskRevision=task.revision + 1,
        acceptedSequence=task.next_sequence,
    )
    services.ac_event_repo.insert(
        conn,
        event_id=event_id,
        task_id=task.task_id,
        owner_id=owner_id,
        sequence=task.next_sequence,
        run_id=run_id,
        turn_id=turn_id,
        event_type="run.accepted",
        payload_version=1,
        payload_json=canonical_json(
            {
                **receipt.model_dump(mode="json", exclude={"replayed"}),
                "configDigest": project.config_digest,
                "inputSnapshotDigest": content_digest(snapshot),
            }
        ),
        created_at=timestamp,
    )
    inserted = services.ac_command_repo.insert(
        conn,
        command_id=str(command.commandID),
        owner_id=owner_id,
        idempotency_key=command.idempotencyKey,
        operation="turn.newMessage",
        target_id=task.task_id,
        payload_digest=digest,
        restore_epoch=identity.restoreEpoch,
        receipt_json=receipt.model_dump_json(),
        created_at=timestamp,
    )
    if not inserted:
        raise CommandAdmissionError("idempotencyConflict")
    return receipt
