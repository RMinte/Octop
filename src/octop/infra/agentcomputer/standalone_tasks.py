"""Projectless Task admission and its own stopped Agent, workspace and thread."""

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from psycopg import Error as PostgresError
from pydantic import ValidationError

from octop.infra.agentcomputer.capabilities import text_task_available
from octop.infra.agentcomputer.commands import (
    CommandAdmissionError,
    StorageDataError,
    StorageUnavailableError,
    canonical_json,
    content_digest,
)
from octop.infra.agentcomputer.identity import IdentityError, get_or_create_identity
from octop.infra.agentcomputer.models import (
    ProjectConfiguration,
    StandaloneTaskCreateCommand,
    StandaloneTaskCreateReceipt,
    TaskView,
)
from octop.infra.agentcomputer.preparation_lock import preparation_lock
from octop.infra.agentcomputer.projects import _read_claim
from octop.infra.agents.manager import AgentCreateSpec, AgentManager
from octop.infra.db.repos.ac_tasks import AcTaskRow
from octop.infra.db.services import SharedServices
from octop.infra.errors import OctopError
from octop.infra.gateway.threads import ThreadRegistry
from octop.infra.utils.ulid import new_ulid


def _task_root(services: SharedServices, workspace_id: UUID) -> Path:
    return services.paths.root.resolve() / "agentcomputer-task-workspaces" / str(workspace_id)


def _task_configuration(task: AcTaskRow) -> ProjectConfiguration:
    try:
        if task.standalone_config_json is None:
            raise StorageDataError("missing Task configuration")
        configuration = ProjectConfiguration.model_validate_json(task.standalone_config_json)
    except ValidationError as exc:
        raise StorageDataError("invalid Task configuration") from exc
    if (
        configuration.model_fields_set != set(ProjectConfiguration.model_fields)
        or content_digest(configuration.model_dump(mode="json")) != task.standalone_config_digest
    ):
        raise StorageDataError("Task configuration digest mismatch")
    return configuration


def _task_binding(
    services: SharedServices, task: AcTaskRow, owner_id: int, box_id: UUID
) -> tuple[UUID, UUID, str, ProjectConfiguration]:
    if (
        task.owner_id != owner_id
        or task.project_id is not None
        or task.standalone_box_id != str(box_id)
        or task.schema_version != 1
        or task.standalone_workspace_id is None
        or task.standalone_agent_id is None
        or re.fullmatch(r"ac-[0-9a-f]{32}", task.standalone_agent_id) is None
    ):
        raise StorageDataError("invalid standalone Task scope")
    try:
        task_id = UUID(task.task_id)
        workspace_id = UUID(task.standalone_workspace_id)
        session_id = UUID(task.session_id)
    except ValueError as exc:
        raise StorageDataError("invalid standalone Task identity") from exc
    if (
        str(task_id) != task.task_id
        or str(workspace_id) != task.standalone_workspace_id
        or str(session_id) != task.session_id
    ):
        raise StorageDataError("non-canonical standalone Task identity")
    configuration = _task_configuration(task)
    return workspace_id, session_id, task.standalone_agent_id, configuration


def standalone_task_view(
    services: SharedServices, conn: Any, owner_id: int, task: AcTaskRow, box_id: UUID
) -> TaskView:
    if task.created_at < 0 or task.updated_at < task.created_at:
        raise StorageDataError("invalid Task timestamps")
    workspace_id, session_id, agent_id, configuration = _task_binding(
        services, task, owner_id, box_id
    )
    if task.preparation_state == "ready":
        agent = services.agent_repo.get_in_transaction(conn, agent_id)
        expected_config = {
            "backend": {
                "type": "filesystem",
                "root_dir": str(_task_root(services, workspace_id)),
                "virtual_mode": True,
            },
            "workspace_dir": f"/.octop/workspaces/{agent_id}",
            "system_files_path": ".octop",
        }
        try:
            actual_config = (
                json.loads(agent.config_json)
                if agent is not None and agent.config_json is not None
                else None
            )
        except ValueError as exc:
            raise StorageDataError("invalid standalone Agent configuration") from exc
        if (
            agent is None
            or agent.user_id != owner_id
            or agent.managed_by != "agentcomputer"
            or agent.managed_task_id != task.task_id
            or agent.managed_project_id is not None
            or agent.default_model != configuration.modelRef
            or agent.system_prompt != configuration.instructions
            or agent.kind != "expert"
            or agent.is_shared
            or agent.enabled != 0
            or agent.last_state != "stopped"
            or actual_config != expected_config
            or task.octop_thread_id is None
        ):
            raise StorageDataError("invalid standalone Task Agent binding")
        from octop.infra.agentcomputer.tasks import _verify_task_thread

        _verify_task_thread(services, conn, owner_id, task, agent_id)
    elif task.octop_thread_id is not None:
        raise StorageDataError("unfinished standalone Task has a thread")
    try:
        return TaskView.model_validate(
            {
                "taskID": UUID(task.task_id),
                "projectID": None,
                "sessionID": session_id,
                "title": task.title,
                "taskRevision": task.revision,
                "preparationState": task.preparation_state,
                "executionReady": task.preparation_state == "ready"
                and text_task_available(services, model_ref=configuration.modelRef),
                "createdAt": datetime.fromtimestamp(task.created_at / 1000, UTC),
                "updatedAt": datetime.fromtimestamp(task.updated_at / 1000, UTC),
            }
        )
    except (ValidationError, ValueError, OverflowError, OSError) as exc:
        raise StorageDataError("invalid standalone Task projection") from exc


def standalone_task_command_digest(box_id: UUID, command: StandaloneTaskCreateCommand) -> str:
    return content_digest(
        {
            "digestVersion": 1,
            "protocolVersion": 1,
            "boxID": str(box_id),
            "operation": "task.createStandalone",
            "payload": command.model_dump(mode="json", exclude={"commandID", "idempotencyKey"}),
        }
    )


def decode_standalone_task_receipt(
    raw: str, *, command_id: str, box_id: UUID, epoch: int
) -> StandaloneTaskCreateReceipt:
    try:
        receipt = StandaloneTaskCreateReceipt.model_validate_json(raw)
    except ValidationError as exc:
        raise StorageDataError("invalid standalone Task receipt") from exc
    if (
        receipt.model_fields_set != set(StandaloneTaskCreateReceipt.model_fields)
        or str(receipt.commandID) != command_id
        or receipt.boxID != box_id
        or receipt.restoreEpoch != epoch
        or receipt.replayed
        or receipt.taskRevision != 1
    ):
        raise StorageDataError("inconsistent standalone Task receipt")
    return receipt


def validate_standalone_task_receipt(
    services: SharedServices,
    owner_id: int,
    receipt: StandaloneTaskCreateReceipt,
    *,
    conn: Any,
) -> AcTaskRow:
    task = services.ac_task_repo.get_in_transaction(conn, str(receipt.taskID), owner_id)
    if task is None:
        raise StorageDataError("missing standalone Task")
    _task_binding(services, task, owner_id, receipt.boxID)
    if (
        task.session_id != str(receipt.sessionID)
        or task.revision < receipt.taskRevision
        or task.created_at != int(receipt.acceptedAt.timestamp() * 1000)
    ):
        raise StorageDataError("standalone Task receipt binding mismatch")
    standalone_task_view(services, conn, owner_id, task, receipt.boxID)
    return task


def _accept_standalone_task(
    services: SharedServices, owner_id: int, command: StandaloneTaskCreateCommand
) -> StandaloneTaskCreateReceipt:
    identity = get_or_create_identity(services.paths.agentcomputer_identity)
    if command.expectedRestoreEpoch != identity.restoreEpoch:
        raise CommandAdmissionError("restoreEpochConflict")
    digest = standalone_task_command_digest(identity.boxID, command)
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
            if original.operation != "task.createStandalone" or original.payload_digest != digest:
                raise CommandAdmissionError("idempotencyConflict")
            receipt = decode_standalone_task_receipt(
                original.receipt_json,
                command_id=original.command_id,
                box_id=identity.boxID,
                epoch=original.restore_epoch,
            )
            if (
                receipt.taskID != command.taskID
                or original.target_id != str(command.taskID)
                or original.schema_version != 1
                or original.created_at != int(receipt.acceptedAt.timestamp() * 1000)
            ):
                raise StorageDataError("inconsistent standalone Task command")
            validate_standalone_task_receipt(services, owner_id, receipt, conn=conn)
            return receipt.model_copy(update={"replayed": True})
        model_ref = services.config.agentcomputer_text_model_ref
        if not model_ref or model_ref != model_ref.strip():
            raise CommandAdmissionError("runtimeUnavailable", reason="executionUnavailable")
        configuration = ProjectConfiguration(schemaVersion=1, instructions="", modelRef=model_ref)
        config_data = configuration.model_dump(mode="json")
        now = datetime.now(UTC)
        timestamp = int(now.timestamp() * 1000)
        session_id, workspace_id = uuid4(), uuid4()
        agent_id = "ac-" + uuid4().hex
        task = services.ac_task_repo.insert(
            conn,
            task_id=str(command.taskID),
            project_id=None,
            owner_id=owner_id,
            session_id=str(session_id),
            octop_thread_id=None,
            title=command.title,
            created_at=timestamp,
            updated_at=timestamp,
            preparation_state="pending",
            standalone_box_id=str(identity.boxID),
            standalone_workspace_id=str(workspace_id),
            standalone_agent_id=agent_id,
            standalone_config_json=canonical_json(config_data),
            standalone_config_digest=content_digest(config_data),
        )
        if task is None:
            raise CommandAdmissionError("resourceConflict")
        receipt = StandaloneTaskCreateReceipt(
            boxID=identity.boxID,
            commandID=command.commandID,
            acceptedAt=now,
            restoreEpoch=identity.restoreEpoch,
            taskID=command.taskID,
            sessionID=session_id,
            taskRevision=1,
        )
        validate_standalone_task_receipt(services, owner_id, receipt, conn=conn)
        if not services.ac_command_repo.insert(
            conn,
            command_id=str(command.commandID),
            owner_id=owner_id,
            idempotency_key=command.idempotencyKey,
            operation="task.createStandalone",
            target_id=str(command.taskID),
            payload_digest=digest,
            restore_epoch=identity.restoreEpoch,
            receipt_json=receipt.model_dump_json(),
            created_at=timestamp,
        ):
            raise CommandAdmissionError("idempotencyConflict")
    return receipt


def _claim_task_root(
    services: SharedServices, task: AcTaskRow, box_id: UUID, owner_id: int
) -> Path:
    workspace_id, _, agent_id, _ = _task_binding(services, task, owner_id, box_id)
    root = _task_root(services, workspace_id)
    if root.resolve() != root:
        raise StorageDataError("Task workspace root changed")
    claim = (
        services.paths.agentcomputer_identity.parent
        / "task-workspace-claims"
        / (str(workspace_id) + ".json")
    )
    if claim.resolve() != claim:
        raise StorageDataError("Task workspace claim path changed")
    body = canonical_json(
        {
            "schemaVersion": 1,
            "boxID": str(box_id),
            "taskID": task.task_id,
            "workspaceID": str(workspace_id),
            "agentID": agent_id,
            "ownerID": owner_id,
            "configurationDigest": task.standalone_config_digest,
        }
    )
    if claim.exists():
        if _read_claim(claim) != body:
            raise StorageDataError("Task workspace claim mismatch")
    else:
        if root.exists():
            raise StorageDataError("unclaimed Task workspace already exists")
        claim.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", dir=claim.parent, delete=False, encoding="utf-8"
            ) as stream:
                temporary = Path(stream.name)
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, claim)
            except FileExistsError:
                if _read_claim(claim) != body:
                    raise StorageDataError("Task workspace claim collision") from None
            if os.name == "posix":
                directory = os.open(claim.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.resolve() != root:
        raise StorageDataError("Task workspace root escaped")
    return root


def _finish_task_preparation(
    services: SharedServices, owner_id: int, task_id: UUID, box_id: UUID
) -> None:
    timestamp = int(datetime.now(UTC).timestamp() * 1000)
    with services.db.transaction() as conn:
        task = services.ac_task_repo.get_in_transaction(conn, str(task_id), owner_id)
        if task is None or task.preparation_state != "pending":
            raise StorageDataError("standalone Task preparation state changed")
        _, _, agent_id, _ = _task_binding(services, task, owner_id, box_id)
        thread_id = "thr_" + new_ulid()
        registry = ThreadRegistry(
            session_repo=services.session_repo, thread_repo=services.thread_repo
        )
        registry.register_task_binding_in_transaction(
            conn,
            agent_id=agent_id,
            user_id=owner_id,
            task_id=str(task_id),
            thread_id=thread_id,
            title=task.title,
            created_at=timestamp // 1000,
        )
        if not services.ac_task_repo.bind_thread(
            conn, str(task_id), owner_id, thread_id, updated_at=timestamp
        ):
            raise StorageDataError("standalone Task thread confirmation failed")
        ready = services.ac_task_repo.get_in_transaction(conn, str(task_id), owner_id)
        if ready is None:
            raise StorageDataError("standalone Task disappeared")
        standalone_task_view(services, conn, owner_id, ready, box_id)


def _fail_task_preparation(services: SharedServices, owner_id: int, task_id: UUID) -> None:
    timestamp = int(datetime.now(UTC).timestamp() * 1000)
    with services.db.transaction() as conn:
        services.ac_task_repo.fail_preparation(conn, str(task_id), owner_id, updated_at=timestamp)


async def _prepare_standalone_task(
    services: SharedServices,
    registry: AgentManager,
    owner_id: int,
    receipt: StandaloneTaskCreateReceipt,
) -> None:
    lock_path = (
        services.paths.agentcomputer_identity.parent
        / "preparation-locks"
        / ("task-" + str(receipt.taskID) + ".lock")
    )
    context = preparation_lock(lock_path)
    acquired = await asyncio.to_thread(context.__enter__)
    try:
        if not acquired:
            return
        with services.db.transaction() as conn:
            task = validate_standalone_task_receipt(services, owner_id, receipt, conn=conn)
            if task.preparation_state == "failed":
                if not services.ac_task_repo.retry_preparation(
                    conn,
                    task.task_id,
                    owner_id,
                    updated_at=int(datetime.now(UTC).timestamp() * 1000),
                ):
                    raise StorageDataError("standalone Task retry state changed")
                refreshed = services.ac_task_repo.get_in_transaction(conn, task.task_id, owner_id)
                if refreshed is None:
                    raise StorageDataError("standalone Task disappeared during retry")
                task = refreshed
        if task.preparation_state != "pending":
            return
        try:
            workspace_id, _, agent_id, configuration = _task_binding(
                services, task, owner_id, receipt.boxID
            )
            root = await asyncio.to_thread(
                _claim_task_root, services, task, receipt.boxID, owner_id
            )
            existing = await asyncio.to_thread(registry.get_row, agent_id)
            if existing is None:
                await registry.create_task_stopped(
                    AgentCreateSpec(
                        agent_id=agent_id,
                        user_id=owner_id,
                        name=agent_id,
                        default_model=configuration.modelRef,
                        system_prompt=configuration.instructions,
                        config={
                            "backend": {
                                "type": "filesystem",
                                "root_dir": str(root),
                                "virtual_mode": True,
                            },
                            "workspace_dir": f"/.octop/workspaces/{agent_id}",
                        },
                    ),
                    task_id=str(receipt.taskID),
                )
            workspace = await asyncio.to_thread(
                registry.workspace_for_task_preparation, agent_id, str(receipt.taskID)
            )
            marker = canonical_json(
                {
                    "schemaVersion": 1,
                    "boxID": str(receipt.boxID),
                    "taskID": str(receipt.taskID),
                    "workspaceID": str(workspace_id),
                    "agentID": agent_id,
                    "configurationDigest": task.standalone_config_digest,
                }
            )
            marker_path = ".octop/agentcomputer-task.json"
            if await asyncio.to_thread(workspace.exists, marker_path):
                actual = await asyncio.to_thread(workspace.read_text, marker_path, limit=65_537)
                if actual != marker:
                    raise StorageDataError("standalone Task marker mismatch")
            else:
                await asyncio.to_thread(workspace.write_text, marker_path, marker, force=False)
                if (
                    await asyncio.to_thread(workspace.read_text, marker_path, limit=65_537)
                    != marker
                ):
                    raise StorageDataError("standalone Task marker confirmation failed")
            await asyncio.to_thread(
                _finish_task_preparation, services, owner_id, receipt.taskID, receipt.boxID
            )
        except (OctopError, StorageDataError, OSError):
            await asyncio.to_thread(_fail_task_preparation, services, owner_id, receipt.taskID)
    finally:
        await asyncio.to_thread(context.__exit__, None, None, None)


async def create_standalone_task(
    services: SharedServices,
    registry: AgentManager,
    *,
    owner_id: int,
    command: StandaloneTaskCreateCommand,
) -> StandaloneTaskCreateReceipt:
    try:
        receipt = await asyncio.to_thread(_accept_standalone_task, services, owner_id, command)
    except (sqlite3.DatabaseError, PostgresError) as exc:
        raise StorageUnavailableError("standalone Task registration unavailable") from exc
    except (IdentityError, StorageDataError):
        raise
    except (ValueError, TypeError) as exc:
        raise StorageDataError("invalid stored standalone Task value") from exc
    operation = asyncio.create_task(_prepare_standalone_task(services, registry, owner_id, receipt))
    cancelled = False
    while True:
        try:
            await asyncio.shield(operation)
            break
        except (sqlite3.DatabaseError, PostgresError) as exc:
            raise StorageUnavailableError("standalone Task preparation unavailable") from exc
        except asyncio.CancelledError:
            cancelled = True
            if operation.cancelled():
                raise
    if cancelled:
        raise asyncio.CancelledError
    return receipt
