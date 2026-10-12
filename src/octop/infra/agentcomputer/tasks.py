"""Internal atomic Task/thread/session registration; production execution is closed.

Only isolated ready fixtures call register_task until project execution validation
and managed engine ingress are available. The HTTP adapter exposes reads only.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from psycopg import Error as PostgresError
from pydantic import ValidationError

from octop.infra.agentcomputer.commands import (
    CommandAdmissionError,
    StorageDataError,
    StorageUnavailableError,
    content_digest,
    decode_task_receipt,
)
from octop.infra.agentcomputer.identity import get_or_create_identity
from octop.infra.agentcomputer.models import (
    ProjectConfiguration,
    TaskCreateCommand,
    TaskCreateReceipt,
    TaskView,
)
from octop.infra.agentcomputer.projects import _intent_for
from octop.infra.db.services import SharedServices
from octop.infra.gateway.threads import ThreadRegistry
from octop.infra.utils.ulid import new_ulid


def task_command_digest(box_id: UUID, command: TaskCreateCommand) -> str:
    return content_digest(
        {
            "digestVersion": 1,
            "protocolVersion": 1,
            "boxID": str(box_id),
            "operation": "task.create",
            "payload": command.model_dump(mode="json", exclude={"commandID", "idempotencyKey"}),
        }
    )


def _project_agent(
    services: SharedServices, conn: Any, owner_id: int, project_id: UUID, box_id: UUID
) -> str:
    project = services.ac_project_repo.get_in_transaction(conn, str(project_id), owner_id)
    if project is None or project.default_agent_id is None or project.schema_version != 1:
        raise StorageDataError("missing task project binding")
    intent = _intent_for(services, project_id, owner_id, conn=conn)
    agent = services.agent_repo.get_in_transaction(conn, project.default_agent_id)
    if (
        intent.boxID != box_id
        or agent is None
        or agent.agent_id != intent.plannedAgentID
        or agent.user_id != owner_id
        or agent.managed_by != "agentcomputer"
        or agent.managed_project_id != str(project_id)
        or agent.kind != "expert"
        or agent.is_shared
        or agent.template_name is not None
        or agent.skill_package_ids != "[]"
        or agent.knowledge_base_ids != "[]"
        or agent.mcp_servers != "[]"
    ):
        raise StorageDataError("invalid task project agent")
    try:
        configuration = ProjectConfiguration.model_validate_json(project.config_json)
        config = json.loads(agent.config_json) if agent.config_json is not None else None
    except (ValidationError, ValueError) as exc:
        raise StorageDataError("invalid task agent configuration") from exc
    if (
        agent.default_model != configuration.modelRef
        or agent.system_prompt != configuration.instructions
        or config
        != {
            "backend": {"type": "filesystem", "root_dir": intent.serverRoot, "virtual_mode": True},
            "workspace_dir": f"/.octop/workspaces/{agent.agent_id}",
            "system_files_path": ".octop",
        }
    ):
        raise StorageDataError("task agent configuration binding mismatch")
    return agent.agent_id


def _get_task(
    services: SharedServices, conn: Any, owner_id: int, task_id: UUID, box_id: UUID
) -> TaskView | None:
    task = services.ac_task_repo.get_in_transaction(conn, str(task_id), owner_id)
    if task is None:
        return None
    if task.schema_version != 1 or task.octop_thread_id is None:
        raise StorageDataError("missing task thread binding")
    try:
        project_id = UUID(task.project_id)
        view = TaskView.model_validate(
            {
                "taskID": task_id,
                "projectID": project_id,
                "sessionID": UUID(task.session_id),
                "title": task.title,
                "taskRevision": task.revision,
                "preparationState": task.preparation_state,
            }
        )
    except (ValidationError, ValueError) as exc:
        raise StorageDataError("invalid task projection") from exc
    agent_id = _project_agent(services, conn, owner_id, project_id, box_id)
    key = ThreadRegistry.task_key(agent_id=agent_id, task_id=str(task_id))
    thread = services.thread_repo.get_in_transaction(conn, task.octop_thread_id)
    # Legacy SessionRow decoding can fill missing fields; inspect the stored
    # binding directly so it cannot repair a damaged Task mapping while reading.
    session = services.session_repo.get_raw_in_transaction(conn, key)
    if (
        thread is None
        or session is None
        or thread.agent_id != agent_id
        or thread.user_id != owner_id
        or thread.channel_type != "agentcomputer"
        or thread.session_key != key
        or session["agent_id"] != agent_id
        or session["user_id"] != owner_id
        or session["thread_id"] != task.octop_thread_id
        or session["channel_type"] != "agentcomputer"
        or session["chat_type"] != "dm"
        or session["channel_subject_id"] != str(task_id)
        or session["channel_chat_type"] != "dm"
        or session["channel_id"] is not None
    ):
        raise StorageDataError("task thread or session binding mismatch")
    try:
        metadata = json.loads(session["channel_metadata"])
    except (TypeError, ValueError) as exc:
        raise StorageDataError("invalid task session metadata") from exc
    if (
        not isinstance(metadata, dict)
        or type(metadata.get("user_id")) is not int
        or metadata
        != {"channel_type": "agentcomputer", "user_id": owner_id, "task_id": str(task_id)}
    ):
        raise StorageDataError("task session metadata mismatch")
    return view


def get_task(services: SharedServices, owner_id: int, task_id: UUID) -> TaskView | None:
    try:
        identity = get_or_create_identity(services.paths.agentcomputer_identity)
        with services.db.transaction() as conn:
            return _get_task(services, conn, owner_id, task_id, identity.boxID)
    except (sqlite3.DatabaseError, PostgresError) as exc:
        raise StorageUnavailableError("task storage unavailable") from exc


def validate_task_receipt_binding(
    services: SharedServices, owner_id: int, receipt: TaskCreateReceipt, *, conn: Any = None
) -> None:
    try:
        if conn is None:
            with services.db.transaction() as connection:
                validate_task_receipt_binding(services, owner_id, receipt, conn=connection)
            return
        view = _get_task(services, conn, owner_id, receipt.taskID, receipt.boxID)
    except (sqlite3.DatabaseError, PostgresError) as exc:
        raise StorageUnavailableError("task receipt storage unavailable") from exc
    if (
        view is None
        or view.projectID != receipt.projectID
        or view.sessionID != receipt.sessionID
        or view.taskRevision < receipt.taskRevision
    ):
        raise StorageDataError("task receipt binding mismatch")


def register_task(
    services: SharedServices, *, owner_id: int, command: TaskCreateCommand
) -> TaskCreateReceipt:
    """Commit a complete mapping in one transaction, without external preparation.

    No HTTP, CLI, IM or Cron writer calls this function. A future production
    adapter must validate project execution capabilities before registration.
    """
    try:
        return _register_task(services, owner_id, command)
    except (sqlite3.DatabaseError, PostgresError) as exc:
        raise StorageUnavailableError("task registration storage unavailable") from exc


def _register_task(
    services: SharedServices, owner_id: int, command: TaskCreateCommand
) -> TaskCreateReceipt:
    identity = get_or_create_identity(services.paths.agentcomputer_identity)
    if command.expectedRestoreEpoch != identity.restoreEpoch:
        raise CommandAdmissionError("restoreEpochConflict")
    digest = task_command_digest(identity.boxID, command)
    with services.db.transaction() as conn:
        if not services.ac_command_repo.lock_owner(conn, owner_id):
            raise CommandAdmissionError("accessDenied")
        original = services.ac_command_repo.get_by_key(conn, owner_id, command.idempotencyKey)
        same_id = services.ac_command_repo.get_global_in_transaction(conn, str(command.commandID))
        if same_id is not None and (
            same_id.owner_id != owner_id or same_id.idempotency_key != command.idempotencyKey
        ):
            raise CommandAdmissionError("idempotencyConflict")
        if original is not None:
            if original.operation != "task.create" or original.payload_digest != digest:
                raise CommandAdmissionError("idempotencyConflict")
            receipt = decode_task_receipt(
                original.receipt_json,
                command_id=original.command_id,
                box_id=identity.boxID,
                epoch=original.restore_epoch,
            )
            if (
                receipt.taskID != command.taskID
                or receipt.projectID != command.projectID
                or receipt.restoreEpoch != command.expectedRestoreEpoch
                or original.target_id != str(command.taskID)
                or original.schema_version != 1
                or original.created_at != int(receipt.acceptedAt.timestamp() * 1000)
            ):
                raise StorageDataError("inconsistent original task command")
            validate_task_receipt_binding(services, owner_id, receipt, conn=conn)
            return receipt.model_copy(update={"replayed": True})
        project = services.ac_project_repo.get_in_transaction(
            conn, str(command.projectID), owner_id
        )
        if project is None:
            raise CommandAdmissionError("resourceNotFound")
        if project.preparation_state != "ready":
            raise CommandAdmissionError("runtimeUnavailable", reason="projectNotReady")
        if project.revision != command.expectedProjectRevision:
            raise CommandAdmissionError("revisionConflict")
        agent_id = _project_agent(services, conn, owner_id, command.projectID, identity.boxID)
        preparation = services.ac_project_preparation_repo.get_in_transaction(
            conn, str(command.projectID), owner_id
        )
        agent = services.agent_repo.get_in_transaction(conn, agent_id)
        if (
            preparation is None
            or preparation.schema_version != 1
            or preparation.agent_state != "verified"
            or preparation.workspace_state != "verified"
            or preparation.reason_code is not None
            or agent is None
            or agent.enabled != 0
            or agent.last_state != "stopped"
        ):
            raise CommandAdmissionError("runtimeUnavailable", reason="projectNotReady")
        now = datetime.now(UTC)
        timestamp = int(now.timestamp() * 1000)
        thread_id = "thr_" + new_ulid()
        session_id = uuid4()
        key = ThreadRegistry.task_key(agent_id=agent_id, task_id=str(command.taskID))
        if (
            services.session_repo.get_raw_in_transaction(conn, key) is not None
            or services.thread_repo.get_in_transaction(conn, thread_id) is not None
        ):
            raise CommandAdmissionError("resourceConflict")
        task = services.ac_task_repo.insert(
            conn,
            task_id=str(command.taskID),
            project_id=str(command.projectID),
            owner_id=owner_id,
            session_id=str(session_id),
            octop_thread_id=None,
            title=command.title,
            created_at=timestamp,
            updated_at=timestamp,
            preparation_state="pending",
        )
        if task is None:
            raise CommandAdmissionError("resourceConflict")
        registry = ThreadRegistry(
            session_repo=services.session_repo, thread_repo=services.thread_repo
        )
        registry.register_task_binding_in_transaction(
            conn,
            agent_id=agent_id,
            user_id=owner_id,
            task_id=str(command.taskID),
            thread_id=thread_id,
            title=command.title,
            created_at=timestamp // 1000,
        )
        if not services.ac_task_repo.bind_thread(
            conn, str(command.taskID), owner_id, thread_id, updated_at=timestamp
        ):
            raise StorageDataError("task thread confirmation failed")
        receipt = TaskCreateReceipt(
            boxID=identity.boxID,
            commandID=command.commandID,
            acceptedAt=now,
            restoreEpoch=identity.restoreEpoch,
            projectID=command.projectID,
            taskID=command.taskID,
            sessionID=session_id,
            taskRevision=1,
        )
        validate_task_receipt_binding(services, owner_id, receipt, conn=conn)
        if not services.ac_command_repo.insert(
            conn,
            command_id=str(command.commandID),
            owner_id=owner_id,
            idempotency_key=command.idempotencyKey,
            operation="task.create",
            target_id=str(command.taskID),
            payload_digest=digest,
            restore_epoch=identity.restoreEpoch,
            receipt_json=receipt.model_dump_json(),
            created_at=timestamp,
        ):
            raise CommandAdmissionError("idempotencyConflict")
    return receipt
