"""A single control-database read position for an owned Task and its next events."""

from __future__ import annotations

import sqlite3
from typing import Annotated
from uuid import UUID

from psycopg import Error as PostgresError
from pydantic import Field, StrictInt

from octop.infra.agentcomputer.commands import StorageDataError, StorageUnavailableError
from octop.infra.agentcomputer.event_listing import (
    _canonical_uuid,
    _encode_cursor,
    _validate_payload,
)
from octop.infra.agentcomputer.identity import IdentityError, get_or_create_identity
from octop.infra.agentcomputer.models import ControlModel, RunView, TaskView
from octop.infra.agentcomputer.runs import get_run_in_transaction
from octop.infra.agentcomputer.tasks import _get_task
from octop.infra.db.services import SharedServices


class TaskControlSnapshot(ControlModel):
    task: TaskView
    latestRun: RunView | None = Field(
        description="Latest fully verified Run in the same control-database snapshot, if any."
    )
    coveredSequence: Annotated[StrictInt, Field(ge=0)] = Field(
        description="All Task control-event sequence numbers through this value are committed."
    )
    nextCursor: str = Field(
        description="Opaque cursor for /tasks/{taskID}/events after coveredSequence."
    )


def get_task_control_snapshot(
    services: SharedServices, owner_id: int, task_id: UUID
) -> TaskControlSnapshot | None:
    """Read a recoverable control baseline and its continuation in one DB snapshot."""
    try:
        identity = get_or_create_identity(services.paths.agentcomputer_identity)
        with services.db.read_snapshot() as conn:
            task = _get_task(
                services, conn, owner_id, task_id, identity.boxID, include_execution_ready=True
            )
            if task is None:
                return None
            row = services.ac_task_repo.get_in_transaction(conn, str(task_id), owner_id)
            if row is None or row.next_sequence < 1 or row.project_id != str(task.projectID):
                raise StorageDataError("invalid Task control watermark")
            covered = row.next_sequence - 1
            count, first, last = services.ac_event_repo.span_for_task_in_transaction(
                conn, str(task_id), owner_id
            )
            if (
                count != covered
                or first != (1 if covered else None)
                or last != (covered if covered else None)
            ):
                raise StorageDataError("Task control event sequence has a gap")

            latest_row = services.ac_run_repo.latest_for_task_in_transaction(conn, str(task_id))
            latest: RunView | None = None
            if latest_row is not None:
                latest_id = _canonical_uuid(latest_row.run_id)
                if (
                    latest_row.owner_id != owner_id
                    or latest_row.task_id != str(task_id)
                    or latest_row.project_id != str(task.projectID)
                ):
                    raise StorageDataError("latest Run scope mismatch")
                latest = get_run_in_transaction(
                    services, conn, owner_id=owner_id, run_id=latest_id, box_id=identity.boxID
                )
                if (
                    latest is None
                    or latest.taskID != task_id
                    or latest.projectID != task.projectID
                    or latest.runID != latest_id
                ):
                    raise StorageDataError("latest Run graph mismatch")
            if (latest is None) != (covered == 0):
                raise StorageDataError("Task event and Run graph mismatch")

            anchor_id = None
            if covered:
                anchor = services.ac_event_repo.get_by_sequence_in_transaction(
                    conn, str(task_id), owner_id, covered
                )
                if anchor is None:
                    raise StorageDataError("missing Task event anchor")
                anchor_id = _canonical_uuid(anchor.event_id)
                anchor_run_id = _canonical_uuid(anchor.run_id)
                anchor_run = get_run_in_transaction(
                    services, conn, owner_id=owner_id, run_id=anchor_run_id, box_id=identity.boxID
                )
                anchor_run_row = services.ac_run_repo.get_in_transaction(
                    conn, anchor.run_id, owner_id
                )
                if (
                    anchor_run is None
                    or anchor_run_row is None
                    or anchor.task_id != str(task_id)
                    or anchor.owner_id != owner_id
                    or anchor.sequence != covered
                    or anchor_run.taskID != task_id
                    or anchor_run.projectID != task.projectID
                    or anchor_run.turnID != _canonical_uuid(anchor.turn_id)
                ):
                    raise StorageDataError("Task event anchor scope mismatch")
                dispatch = services.ac_run_dispatch_repo.get_in_transaction(
                    conn, anchor.run_id, owner_id
                )
                _validate_payload(anchor, anchor_run, anchor_run_row, dispatch)

            cursor = _encode_cursor(
                box_id=identity.boxID,
                owner_id=owner_id,
                task_id=task_id,
                restore_epoch=identity.restoreEpoch,
                after_sequence=covered,
                after_event_id=anchor_id,
                through_sequence=None,
            )
            return TaskControlSnapshot(
                task=task,
                latestRun=latest,
                coveredSequence=covered,
                nextCursor=cursor,
            )
    except (sqlite3.DatabaseError, PostgresError) as exc:
        raise StorageUnavailableError("Task control snapshot storage unavailable") from exc
    except (IdentityError, StorageDataError, StorageUnavailableError):
        raise
    except (ValueError, TypeError, OverflowError, OSError) as exc:
        raise StorageDataError("invalid stored Task control snapshot") from exc
