"""Guarded transport ingress sharing the atomic registration kernel."""

from __future__ import annotations

import sqlite3
from uuid import UUID

from psycopg import Error as PostgresError
from pydantic import ValidationError

from octop.infra.agentcomputer.capabilities import get_capabilities, text_task_available
from octop.infra.agentcomputer.commands import (
    CommandAdmissionError,
    StorageDataError,
    StorageUnavailableError,
    admit_new_message_in_transaction,
    content_digest,
)
from octop.infra.agentcomputer.identity import get_or_create_identity
from octop.infra.agentcomputer.models import (
    CommandReceipt,
    NewMessageCommand,
    ProjectConfiguration,
    StandaloneNewMessageCommand,
)
from octop.infra.agentcomputer.tasks import _get_task
from octop.infra.db.services import SharedServices


def submit_new_message(
    services: SharedServices,
    *,
    owner_id: int,
    task_id: UUID,
    command: NewMessageCommand | StandaloneNewMessageCommand,
) -> CommandReceipt:
    """Validate owned managed bindings and availability before new registration.

    Existing accepted commands replay their original facts even if admission is
    now closed. Restore epochs are checked before any replay. All database
    checks and writes use the same connection under the authenticated owner lock.
    """
    identity = get_or_create_identity(services.paths.agentcomputer_identity)
    profile = get_capabilities(services)
    if profile.boxID != identity.boxID or profile.restoreEpoch != identity.restoreEpoch:
        raise StorageDataError("capability identity mismatch")
    if command.expectedRestoreEpoch != identity.restoreEpoch:
        raise CommandAdmissionError("restoreEpochConflict")
    try:
        with services.db.transaction() as conn:
            if not services.ac_command_repo.lock_owner(conn, owner_id):
                raise CommandAdmissionError("accessDenied")
            task = _get_task(services, conn, owner_id, task_id, identity.boxID)
            if task is None:
                raise CommandAdmissionError("resourceNotFound")
            previous = services.ac_command_repo.get_by_key(conn, owner_id, command.idempotencyKey)
            collision = services.ac_command_repo.get_global_in_transaction(
                conn, str(command.commandID)
            )
            # The shared kernel validates every original receipt and collision.
            # Replaying is not a new admission or an engine re-dispatch.
            if previous is not None or collision is not None:
                return admit_new_message_in_transaction(
                    services,
                    conn,
                    owner_id=owner_id,
                    task_id=task_id,
                    command=command,
                    identity=identity,
                )
            if task.projectID is None:
                if not isinstance(command, StandaloneNewMessageCommand):
                    raise CommandAdmissionError("runtimeUnavailable", reason="executionUnavailable")
                stored = services.ac_task_repo.get_in_transaction(conn, str(task_id), owner_id)
                if stored is None:
                    raise StorageDataError("missing standalone submission Task")
                from octop.infra.agentcomputer.standalone_tasks import _task_configuration

                configuration = _task_configuration(stored)
                if not text_task_available(services, model_ref=configuration.modelRef):
                    raise CommandAdmissionError("runtimeUnavailable", reason="executionUnavailable")
                if command.permissionMode not in profile.supportedPermissionModes:
                    raise CommandAdmissionError(
                        "capabilityUnsupported", reason="permissionModeUnsupported"
                    )
                return admit_new_message_in_transaction(
                    services,
                    conn,
                    owner_id=owner_id,
                    task_id=task_id,
                    command=command,
                    identity=identity,
                )
            if not isinstance(command, NewMessageCommand):
                raise CommandAdmissionError("capabilityUnsupported")
            project = services.ac_project_repo.get_in_transaction(
                conn, str(task.projectID), owner_id
            )
            if project is None:
                raise StorageDataError("missing submission project")
            try:
                configuration = ProjectConfiguration.model_validate_json(project.config_json)
            except ValidationError as exc:
                raise StorageDataError("invalid submission project configuration") from exc
            if (
                configuration.model_fields_set != set(ProjectConfiguration.model_fields)
                or content_digest(configuration.model_dump(mode="json")) != project.config_digest
            ):
                raise StorageDataError("submission project configuration digest mismatch")
            if not text_task_available(services, model_ref=configuration.modelRef):
                raise CommandAdmissionError("runtimeUnavailable", reason="executionUnavailable")
            if command.permissionMode not in profile.supportedPermissionModes:
                raise CommandAdmissionError(
                    "capabilityUnsupported", reason="permissionModeUnsupported"
                )
            return admit_new_message_in_transaction(
                services,
                conn,
                owner_id=owner_id,
                task_id=task_id,
                command=command,
                identity=identity,
            )
    except (sqlite3.DatabaseError, PostgresError) as exc:
        raise StorageUnavailableError("turn submission storage unavailable") from exc
    except StorageDataError:
        raise
    except (ValueError, TypeError) as exc:
        # The request is already typed; failures here concern stored mapping
        # rows, whose numeric types SQLite affinity cannot fully enforce.
        raise StorageDataError("invalid persisted submission binding") from exc
