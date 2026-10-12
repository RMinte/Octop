"""Durable project intent and stopped local resources; execution remains disabled."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import stat
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from psycopg import Error as PostgresError
from pydantic import ValidationError

from octop.infra.agentcomputer.commands import (
    CommandAdmissionError,
    StorageDataError,
    StorageUnavailableError,
    canonical_json,
    content_digest,
    decode_project_receipt,
    validate_project_receipt_binding,
)
from octop.infra.agentcomputer.identity import IdentityError, get_or_create_identity
from octop.infra.agentcomputer.models import (
    ProjectConfiguration,
    ProjectCreateCommand,
    ProjectCreateReceipt,
    ProjectPreparationIntent,
    ProjectView,
)
from octop.infra.agentcomputer.preparation_lock import preparation_lock
from octop.infra.agents.manager import AgentCreateSpec, AgentManager
from octop.infra.db.services import SharedServices
from octop.infra.errors import OctopError


def project_command_digest(box_id: UUID, command: ProjectCreateCommand) -> str:
    return content_digest(
        {
            "digestVersion": 1,
            "protocolVersion": 1,
            "boxID": str(box_id),
            "operation": "project.create",
            "payload": command.model_dump(mode="json", exclude={"commandID", "idempotencyKey"}),
        }
    )


def _server_root(services: SharedServices, workspace_id: UUID) -> Path:
    return services.paths.root.resolve() / "agentcomputer-workspaces" / str(workspace_id)


def _accept_project(
    services: SharedServices, owner_id: int, command: ProjectCreateCommand
) -> ProjectCreateReceipt:
    identity = get_or_create_identity(services.paths.agentcomputer_identity)
    if command.expectedRestoreEpoch != identity.restoreEpoch:
        raise CommandAdmissionError("restoreEpochConflict")
    digest = project_command_digest(identity.boxID, command)
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
            if original.payload_digest != digest or original.operation != "project.create":
                raise CommandAdmissionError("idempotencyConflict")
            receipt = decode_project_receipt(
                original.receipt_json,
                command_id=original.command_id,
                box_id=identity.boxID,
                epoch=original.restore_epoch,
            )
            if (
                receipt.projectID != command.projectID
                or receipt.restoreEpoch != command.expectedRestoreEpoch
                or original.target_id != str(command.projectID)
                or original.schema_version != 1
                or original.created_at != int(receipt.acceptedAt.timestamp() * 1000)
            ):
                raise StorageDataError("inconsistent original project command")
            validate_project_receipt_binding(services, owner_id, receipt, conn=conn)
            return receipt.model_copy(update={"replayed": True})
        workspace_id, preparation_id = uuid4(), uuid4()
        planned_agent_id = "ac-" + uuid4().hex
        now = datetime.now(UTC)
        timestamp = int(now.timestamp() * 1000)
        config = command.configuration.model_dump(mode="json")
        config_digest = content_digest(config)
        project = services.ac_project_repo.insert(
            conn,
            project_id=str(command.projectID),
            owner_id=owner_id,
            box_id=str(identity.boxID),
            workspace_id=str(workspace_id),
            name=command.name,
            config_json=canonical_json(config),
            config_digest=config_digest,
            created_at=timestamp,
            updated_at=timestamp,
        )
        if project is None:
            raise CommandAdmissionError("resourceConflict")
        intent = ProjectPreparationIntent(
            schemaVersion=1,
            boxID=identity.boxID,
            projectID=command.projectID,
            workspaceID=workspace_id,
            ownerID=owner_id,
            plannedAgentID=planned_agent_id,
            serverRoot=str(_server_root(services, workspace_id)),
            configurationDigest=config_digest,
        )
        services.ac_project_preparation_repo.insert(
            conn,
            preparation_id=str(preparation_id),
            project_id=str(command.projectID),
            owner_id=owner_id,
            planned_agent_id=planned_agent_id,
            intent_json=intent.model_dump_json(),
            intent_digest=content_digest(intent.model_dump(mode="json")),
            restore_epoch=identity.restoreEpoch,
            created_at=timestamp,
            updated_at=timestamp,
        )
        receipt = ProjectCreateReceipt(
            boxID=identity.boxID,
            commandID=command.commandID,
            acceptedAt=now,
            restoreEpoch=identity.restoreEpoch,
            projectID=command.projectID,
            workspaceID=workspace_id,
            preparationID=preparation_id,
            projectRevision=1,
        )
        if not services.ac_command_repo.insert(
            conn,
            command_id=str(command.commandID),
            owner_id=owner_id,
            idempotency_key=command.idempotencyKey,
            operation="project.create",
            target_id=str(command.projectID),
            payload_digest=digest,
            restore_epoch=identity.restoreEpoch,
            receipt_json=receipt.model_dump_json(),
            created_at=timestamp,
        ):
            raise CommandAdmissionError("idempotencyConflict")
    return receipt


def _intent_for(
    services: SharedServices, project_id: UUID, owner_id: int, *, conn: Any = None
) -> ProjectPreparationIntent:
    if conn is None:
        with services.db.connect() as connection:
            return _intent_for(services, project_id, owner_id, conn=connection)
    project = services.ac_project_repo.get_in_transaction(conn, str(project_id), owner_id)
    preparation = services.ac_project_preparation_repo.get_in_transaction(
        conn, str(project_id), owner_id
    )
    if project is None or preparation is None:
        raise StorageDataError("missing project preparation")
    try:
        intent = ProjectPreparationIntent.model_validate_json(preparation.intent_json)
    except ValidationError as exc:
        raise StorageDataError("invalid project intent") from exc
    try:
        configuration = ProjectConfiguration.model_validate_json(project.config_json)
    except ValidationError as exc:
        raise StorageDataError("invalid saved project configuration") from exc
    if (
        preparation.schema_version != 1
        or preparation.restore_epoch < 1
        or preparation.revision < 1
        or intent.model_fields_set != set(ProjectPreparationIntent.model_fields)
        or content_digest(intent.model_dump(mode="json")) != preparation.intent_digest
        or intent.projectID != project_id
        or intent.ownerID != owner_id
        or str(intent.workspaceID) != project.workspace_id
        or str(intent.boxID) != project.box_id
        or intent.plannedAgentID != preparation.planned_agent_id
        or intent.configurationDigest != project.config_digest
        or configuration.model_fields_set != set(ProjectConfiguration.model_fields)
        or content_digest(configuration.model_dump(mode="json")) != project.config_digest
    ):
        raise StorageDataError("project intent binding mismatch")
    return intent


def _get_project(services: SharedServices, owner_id: int, project_id: UUID) -> ProjectView | None:
    with services.db.transaction() as conn:
        project = services.ac_project_repo.get_in_transaction(conn, str(project_id), owner_id)
        if project is None:
            return None
        preparation = services.ac_project_preparation_repo.get_in_transaction(
            conn, str(project_id), owner_id
        )
        if preparation is None:
            raise StorageDataError("missing preparation journal")
        _intent_for(services, project_id, owner_id, conn=conn)
        try:
            return ProjectView.model_validate(
                {
                    "projectID": project_id,
                    "workspaceID": project.workspace_id,
                    "preparationID": preparation.preparation_id,
                    "name": project.name,
                    "projectRevision": project.revision,
                    "configDigest": project.config_digest,
                    "preparationRevision": preparation.revision,
                    "preparationState": project.preparation_state,
                    "steps": {
                        "agent": preparation.agent_state,
                        "workspace": preparation.workspace_state,
                    },
                    "reasonCode": preparation.reason_code,
                    "retryable": preparation.retryable,
                }
            )
        except (ValueError, ValidationError) as exc:
            raise StorageDataError("invalid project projection") from exc


def get_project(services: SharedServices, owner_id: int, project_id: UUID) -> ProjectView | None:
    try:
        return _get_project(services, owner_id, project_id)
    except (sqlite3.DatabaseError, PostgresError) as exc:
        raise StorageUnavailableError("project storage unavailable") from exc
    except (IdentityError, StorageDataError):
        raise
    except (ValueError, TypeError) as exc:
        raise StorageDataError("invalid stored project value") from exc


def _read_claim(path: Path) -> str:
    if not stat.S_ISREG(path.lstat().st_mode):
        raise StorageDataError("invalid workspace claim")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "r", encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or (os.name == "posix" and info.st_mode & 0o077):
            raise StorageDataError("invalid workspace claim permissions")
        try:
            text = stream.read(65_537)
        except UnicodeError as exc:
            raise StorageDataError("invalid workspace claim encoding") from exc
        if len(text) > 65_536:
            raise StorageDataError("invalid workspace claim size")
        return text


def _claim_root(services: SharedServices, intent: ProjectPreparationIntent) -> None:
    root = _server_root(services, intent.workspaceID)
    if str(root) != intent.serverRoot or root.resolve() != root:
        raise StorageDataError("workspace root changed")
    claim = (
        services.paths.agentcomputer_identity.parent
        / "workspace-claims"
        / (str(intent.workspaceID) + ".json")
    )
    if claim.resolve() != claim:
        raise StorageDataError("workspace claim path changed")
    body = canonical_json(intent.model_dump(mode="json"))
    if claim.exists():
        if _read_claim(claim) != body:
            raise StorageDataError("workspace ownership mismatch")
    else:
        if root.exists():
            raise StorageDataError("existing workspace has no ownership claim")
        claim.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", dir=claim.parent, delete=False, encoding="utf-8"
            ) as stream:
                tmp_path = Path(stream.name)
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(tmp_path, claim)
            except FileExistsError:
                if _read_claim(claim) != body:
                    raise StorageDataError("workspace claim collision") from None
            if os.name == "posix":
                directory = os.open(claim.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            if tmp_path:
                tmp_path.unlink(missing_ok=True)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.resolve() != root:
        raise StorageDataError("workspace root escaped")


def _progress(
    services: SharedServices,
    intent: ProjectPreparationIntent,
    step: str | None,
    *,
    reason: str | None = None,
    retryable: bool = False,
) -> None:
    timestamp = int(datetime.now(UTC).timestamp() * 1000)
    with services.db.transaction() as conn:
        project_id = str(intent.projectID)
        if reason is not None:
            services.ac_project_preparation_repo.fail(
                conn, project_id, intent.ownerID, reason, retryable, updated_at=timestamp
            )
            services.ac_project_repo.set_preparation(
                conn, project_id, intent.ownerID, "failed", updated_at=timestamp
            )
        elif step == "agent":
            if not services.ac_project_repo.bind_agent(
                conn, project_id, intent.ownerID, intent.plannedAgentID, updated_at=timestamp
            ):
                raise StorageDataError("project agent binding changed")
            services.ac_project_preparation_repo.confirm_agent(
                conn, project_id, intent.ownerID, updated_at=timestamp
            )
        elif step == "workspace":
            services.ac_project_preparation_repo.confirm_workspace(
                conn, project_id, intent.ownerID, updated_at=timestamp
            )
        else:
            services.ac_project_repo.set_preparation(
                conn, project_id, intent.ownerID, "preparing", updated_at=timestamp
            )
            services.ac_project_preparation_repo.clear_reason(
                conn, project_id, intent.ownerID, updated_at=timestamp
            )


async def _prepare_project(
    services: SharedServices,
    registry: AgentManager,
    owner_id: int,
    command: ProjectCreateCommand,
    receipt: ProjectCreateReceipt,
) -> None:
    path = (
        services.paths.agentcomputer_identity.parent
        / "preparation-locks"
        / (str(receipt.projectID) + ".lock")
    )
    context = preparation_lock(path)
    acquired = await asyncio.to_thread(context.__enter__)
    try:
        if not acquired:
            return
        intent = await asyncio.to_thread(_intent_for, services, receipt.projectID, owner_id)
        if intent.boxID != receipt.boxID or receipt.workspaceID != intent.workspaceID:
            raise StorageDataError("receipt intent mismatch")
        await asyncio.to_thread(_progress, services, intent, None)
        try:
            await asyncio.to_thread(_claim_root, services, intent)
            existing = await asyncio.to_thread(registry.get_row, intent.plannedAgentID)
            if existing is None:
                await registry.create_stopped(
                    AgentCreateSpec(
                        agent_id=intent.plannedAgentID,
                        user_id=owner_id,
                        name=intent.plannedAgentID,
                        default_model=command.configuration.modelRef,
                        system_prompt=command.configuration.instructions,
                        config={
                            "backend": {
                                "type": "filesystem",
                                "root_dir": intent.serverRoot,
                                "virtual_mode": True,
                            },
                            "workspace_dir": "/.octop/workspaces/" + intent.plannedAgentID,
                        },
                    ),
                    project_id=str(intent.projectID),
                )
            # The special workspace entry validates the durable marker, owner,
            # actual root and stopped state, including lost creation confirmation.
            workspace = await asyncio.to_thread(
                registry.workspace_for_project_preparation,
                intent.plannedAgentID,
                str(intent.projectID),
            )
            await asyncio.to_thread(_progress, services, intent, "agent")
            marker = {
                "schemaVersion": 1,
                "projectID": str(intent.projectID),
                "workspaceID": str(intent.workspaceID),
                "boxID": str(intent.boxID),
                "agentID": intent.plannedAgentID,
                "configurationDigest": intent.configurationDigest,
            }
            marker_text = canonical_json(marker)
            marker_path = ".octop/agentcomputer-project.json"
            if await asyncio.to_thread(workspace.exists, marker_path):
                actual = await asyncio.to_thread(workspace.read_text, marker_path, limit=65_537)
                if actual != marker_text:
                    raise StorageDataError("workspace marker mismatch")
            else:
                await asyncio.to_thread(workspace.write_text, marker_path, marker_text, force=False)
                if (
                    await asyncio.to_thread(workspace.read_text, marker_path, limit=65_537)
                    != marker_text
                ):
                    raise StorageDataError("workspace confirmation failed")
            await asyncio.to_thread(_progress, services, intent, "workspace")
            usable = await asyncio.to_thread(
                registry.providers.is_model_ref_usable, command.configuration.modelRef
            )
            await asyncio.to_thread(
                _progress,
                services,
                intent,
                None,
                reason="executionValidationPending" if usable else "providerUnavailable",
                retryable=not usable,
            )
        except OctopError:
            await asyncio.to_thread(
                _progress,
                services,
                intent,
                None,
                reason="resourcePreparationRejected",
                retryable=False,
            )
        except StorageDataError:
            await asyncio.to_thread(
                _progress, services, intent, None, reason="resourceBindingInvalid", retryable=False
            )
        except OSError:
            await asyncio.to_thread(
                _progress, services, intent, None, reason="workspaceUnavailable", retryable=True
            )
    finally:
        await asyncio.to_thread(context.__exit__, None, None, None)


async def create_project(
    services: SharedServices,
    registry: AgentManager,
    *,
    owner_id: int,
    command: ProjectCreateCommand,
) -> ProjectCreateReceipt:
    try:
        receipt = await asyncio.to_thread(_accept_project, services, owner_id, command)
    except (sqlite3.DatabaseError, PostgresError) as exc:
        raise StorageUnavailableError("project registration unavailable") from exc
    except (IdentityError, StorageDataError):
        raise
    except (ValueError, TypeError) as exc:
        raise StorageDataError("invalid stored project registration value") from exc
    # Preparation belongs to this request; cancellation does not release its
    # cross-process lock while an owned resource operation is still finishing.
    task = asyncio.create_task(_prepare_project(services, registry, owner_id, command, receipt))
    cancelled = False
    while True:
        try:
            await asyncio.shield(task)
            break
        except (sqlite3.DatabaseError, PostgresError) as exc:
            raise StorageUnavailableError("project preparation storage unavailable") from exc
        except (IdentityError, StorageDataError):
            raise
        except (ValueError, TypeError) as exc:
            raise StorageDataError("invalid stored project preparation value") from exc
        except asyncio.CancelledError:
            cancelled = True
            if task.cancelled():
                raise
    if cancelled:
        raise asyncio.CancelledError
    return receipt
