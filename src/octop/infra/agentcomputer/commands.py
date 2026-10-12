"""Durable, atomic registration; no model calls, queue consumer or HTTP writes.

Production submission remains disabled while project/thread provisioning and
the managed engine ingress are absent. Tests prepare explicit ready fixtures
to verify this transaction before those external operations are introduced.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from psycopg import Error as PostgresError
from pydantic import ValidationError

from octop.infra.agentcomputer.identity import get_or_create_identity
from octop.infra.agentcomputer.models import (
    CommandReceipt,
    NewMessageCommand,
    ProjectConfiguration,
)
from octop.infra.db.services import SharedServices

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


def get_command_receipt(
    services: SharedServices, owner_id: int, command_id: UUID
) -> CommandReceipt | None:
    try:
        row = services.ac_command_repo.get(str(command_id), owner_id)
    except (sqlite3.DatabaseError, PostgresError) as exc:
        raise StorageUnavailableError("command storage unavailable") from exc
    if row is None:
        return None
    identity = get_or_create_identity(services.paths.agentcomputer_identity)
    # A historic epoch is readable after a restore. The original result must
    # not be rewritten to the current epoch or treated as a fresh acceptance.
    receipt = _decode_receipt(
        row.receipt_json, command_id=row.command_id, box_id=identity.boxID, epoch=row.restore_epoch
    )
    if (
        row.schema_version != 1
        or row.operation != "turn.newMessage"
        or row.target_id != str(receipt.taskID)
        or row.created_at != int(receipt.acceptedAt.timestamp() * 1000)
    ):
        raise StorageDataError("inconsistent command metadata")
    return receipt


def admit_new_message(
    services: SharedServices, *, owner_id: int, task_id: UUID, command: NewMessageCommand
) -> CommandReceipt:
    """Register a new-message Turn atomically in an already prepared mapping.

    This internal kernel is not reachable from HTTP, IM, Cron or CLI ingress.
    A future adapter must validate execution capabilities before calling it.
    Returning a receipt never starts the engine or proves external effects.
    """
    identity = get_or_create_identity(services.paths.agentcomputer_identity)
    if command.expectedRestoreEpoch != identity.restoreEpoch:
        raise CommandAdmissionError("restoreEpochConflict")
    digest = command_digest(box_id=identity.boxID, task_id=task_id, command=command)
    with services.db.transaction() as conn:
        # PostgreSQL locks the authenticated owner row; SQLite's transaction
        # already holds its write reservation. No external work occurs here.
        if not services.ac_command_repo.lock_owner(conn, owner_id):
            raise CommandAdmissionError("accessDenied")
        original = services.ac_command_repo.get_by_key(conn, owner_id, command.idempotencyKey)
        same_id = services.ac_command_repo.get_global_in_transaction(conn, str(command.commandID))
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
            return receipt.model_copy(update={"replayed": True})
        if (
            command.inputArtifactIDs
            or command.selectedSkillIDs
            or command.permissionMode != "askForApproval"
        ):
            raise CommandAdmissionError("capabilityUnsupported")
        task = services.ac_task_repo.get_in_transaction(conn, str(task_id), owner_id)
        if task is None:
            raise CommandAdmissionError("resourceNotFound")
        project = services.ac_project_repo.get_in_transaction(conn, task.project_id, owner_id)
        if project is None or UUID(project.box_id) != identity.boxID:
            raise StorageDataError("invalid project binding")
        if (
            project.preparation_state != "ready"
            or task.preparation_state != "ready"
            or not project.default_agent_id
            or not task.octop_thread_id
        ):
            raise CommandAdmissionError("runtimeUnavailable", reason="preparationIncomplete")
        if project.revision != command.expectedProjectRevision:
            raise CommandAdmissionError("revisionConflict")
        if services.ac_turn_repo.has_user_message(
            conn, task.task_id, owner_id, str(command.userMessageID)
        ):
            raise CommandAdmissionError(
                "idempotencyConflict", reason="userMessageAlreadyRegistered"
            )
        active_id = task.active_run_id or project.active_run_id
        if active_id:
            active = services.ac_run_repo.get_in_transaction(conn, active_id, owner_id)
            if active is None:
                raise StorageDataError("invalid active run binding")
            raise CommandAdmissionError(
                "reconciliationRequired" if active.reconciliation_required else "activeRunConflict"
            )
        try:
            config = ProjectConfiguration.model_validate_json(project.config_json)
        except ValidationError as exc:
            raise StorageDataError("invalid project configuration") from exc
        if content_digest(config.model_dump(mode="json")) != project.config_digest:
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
    # Receipt becomes visible only after every record and deferred FK commits.
    return receipt
