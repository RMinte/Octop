"""Owned Task discovery with a scoped, canonical keyset cursor."""

from __future__ import annotations

import base64
import binascii
import json
import re
import sqlite3
from uuid import UUID

from psycopg import Error as PostgresError

from octop.infra.agentcomputer.commands import (
    StorageDataError,
    StorageUnavailableError,
    canonical_json,
)
from octop.infra.agentcomputer.identity import IdentityError, get_or_create_identity
from octop.infra.agentcomputer.models import TaskListItem, TaskListResponse
from octop.infra.agentcomputer.projects import _project_view
from octop.infra.agentcomputer.runs import get_run_in_transaction
from octop.infra.agentcomputer.tasks import _get_task
from octop.infra.db.services import SharedServices

_CURSOR_PATTERN = re.compile(r"[A-Za-z0-9_-]+\Z")
_CURSOR_FIELDS = {
    "v",
    "boxID",
    "ownerID",
    "projectID",
    "restoreEpoch",
    "createdAt",
    "taskID",
}


class InvalidTaskListCursor(ValueError):
    """The caller supplied a malformed or differently scoped page cursor."""


def _encode_cursor(
    *,
    box_id: UUID,
    owner_id: int,
    project_id: UUID,
    restore_epoch: int,
    created_at: int,
    task_id: UUID,
) -> str:
    payload = canonical_json(
        {
            "v": 1,
            "boxID": str(box_id),
            "ownerID": owner_id,
            "projectID": str(project_id),
            "restoreEpoch": restore_epoch,
            "createdAt": created_at,
            "taskID": str(task_id),
        }
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def _decode_cursor(
    cursor: str,
    *,
    box_id: UUID,
    owner_id: int,
    project_id: UUID,
    restore_epoch: int,
) -> tuple[int, str]:
    if not 1 <= len(cursor) <= 512 or _CURSOR_PATTERN.fullmatch(cursor) is None:
        raise InvalidTaskListCursor("invalid cursor encoding")
    try:
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        if base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii") != cursor:
            raise InvalidTaskListCursor("non-canonical cursor encoding")
        payload = json.loads(raw.decode("utf-8"))
        if type(payload) is not dict or set(payload) != _CURSOR_FIELDS:
            raise InvalidTaskListCursor("invalid cursor fields")
        if canonical_json(payload).encode("utf-8") != raw:
            raise InvalidTaskListCursor("non-canonical cursor payload")
        task_id = payload["taskID"]
        if (
            type(payload["v"]) is not int
            or payload["v"] != 1
            or type(payload["ownerID"]) is not int
            or payload["ownerID"] != owner_id
            or type(payload["restoreEpoch"]) is not int
            or payload["restoreEpoch"] != restore_epoch
            or type(payload["boxID"]) is not str
            or payload["boxID"] != str(box_id)
            or type(payload["projectID"]) is not str
            or payload["projectID"] != str(project_id)
            or type(payload["createdAt"]) is not int
            or not 0 <= payload["createdAt"] <= 2**63 - 1
            or type(task_id) is not str
            or str(UUID(task_id)) != task_id
        ):
            raise InvalidTaskListCursor("cursor scope or sorting key mismatch")
        return payload["createdAt"], task_id
    except (binascii.Error, UnicodeError, ValueError, TypeError) as exc:
        raise InvalidTaskListCursor("invalid cursor") from exc


def list_tasks(
    services: SharedServices,
    owner_id: int,
    project_id: UUID,
    limit: int,
    cursor: str | None,
) -> TaskListResponse | None:
    """Return one verified page, or None for a project outside this user's scope."""
    try:
        with services.db.transaction() as conn:
            project = services.ac_project_repo.get_in_transaction(conn, str(project_id), owner_id)
            if project is None:
                return None
            identity = get_or_create_identity(services.paths.agentcomputer_identity)
            if project.box_id != str(identity.boxID):
                raise StorageDataError("task project installation mismatch")
            _project_view(services, conn, owner_id, project)
            after = (
                _decode_cursor(
                    cursor,
                    box_id=identity.boxID,
                    owner_id=owner_id,
                    project_id=project_id,
                    restore_epoch=identity.restoreEpoch,
                )
                if cursor is not None
                else None
            )
            rows = services.ac_task_repo.list_page_in_transaction(
                conn, str(project_id), owner_id, after=after, limit=limit + 1
            )
            items: list[TaskListItem] = []
            for row in rows[:limit]:
                if row.created_at < 0 or row.updated_at < row.created_at:
                    raise StorageDataError("invalid Task timestamps")
                task_id = UUID(row.task_id)
                if str(task_id) != row.task_id:
                    raise StorageDataError("non-canonical task identity")
                task = _get_task(
                    services,
                    conn,
                    owner_id,
                    task_id,
                    identity.boxID,
                    include_execution_ready=True,
                )
                if task is None or task.projectID != project_id or task.taskID != task_id:
                    raise StorageDataError("task list binding mismatch")
                latest = services.ac_run_repo.latest_for_task_in_transaction(conn, row.task_id)
                latest_view = None
                if latest is not None:
                    if (
                        latest.task_id != row.task_id
                        or latest.project_id != str(project_id)
                        or latest.owner_id != owner_id
                    ):
                        raise StorageDataError("latest Run scope mismatch")
                    run_id = UUID(latest.run_id)
                    if str(run_id) != latest.run_id:
                        raise StorageDataError("non-canonical latest Run identity")
                    latest_view = get_run_in_transaction(
                        services, conn, owner_id=owner_id, run_id=run_id, box_id=identity.boxID
                    )
                    if (
                        latest_view is None
                        or latest_view.taskID != task_id
                        or latest_view.projectID != project_id
                    ):
                        raise StorageDataError("latest Run binding mismatch")
                items.append(TaskListItem(task=task, latestRun=latest_view))
            next_cursor = None
            if len(rows) > limit:
                last = rows[limit - 1]
                next_cursor = _encode_cursor(
                    box_id=identity.boxID,
                    owner_id=owner_id,
                    project_id=project_id,
                    restore_epoch=identity.restoreEpoch,
                    created_at=last.created_at,
                    task_id=items[-1].task.taskID,
                )
            return TaskListResponse(items=items, nextCursor=next_cursor)
    except (sqlite3.DatabaseError, PostgresError) as exc:
        raise StorageUnavailableError("Task list storage unavailable") from exc
    except (IdentityError, InvalidTaskListCursor, StorageDataError):
        raise
    except (ValueError, TypeError) as exc:
        raise StorageDataError("invalid stored Task list value") from exc
