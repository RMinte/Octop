"""Guarded transport ingress sharing the atomic registration kernel.

Production discovery keeps runs unavailable. No request, setting, or environment
flag can bypass this gate, and this module never dispatches an engine.
"""

from __future__ import annotations

import sqlite3
from uuid import UUID

from psycopg import Error as PostgresError

from octop.infra.agentcomputer.capabilities import get_capabilities
from octop.infra.agentcomputer.commands import (
    CommandAdmissionError,
    StorageDataError,
    StorageUnavailableError,
    admit_new_message_in_transaction,
)
from octop.infra.agentcomputer.identity import get_or_create_identity
from octop.infra.agentcomputer.models import CommandReceipt, NewMessageCommand
from octop.infra.agentcomputer.tasks import _get_task
from octop.infra.db.services import SharedServices


def submit_new_message(
    services: SharedServices, *, owner_id: int, task_id: UUID, command: NewMessageCommand
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
            if _get_task(services, conn, owner_id, task_id, identity.boxID) is None:
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
            if not profile.capabilities.runs.available:
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
