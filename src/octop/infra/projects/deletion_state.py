"""Reject fresh work while an owner's project is stopping for deletion."""

from __future__ import annotations

from typing import Any

from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError


def assert_project_writable(
    services: SharedServices, conn: Any, owner_id: int, project_id: str | None
) -> None:
    if project_id is not None and services.project_repo.is_deleting(conn, owner_id, project_id):
        raise OctopError.localized(ErrorCode.PROJECT_TASKS_BUSY, status=409)


def assert_task_writable(services: SharedServices, conn: Any, owner_id: int, task_id: str) -> None:
    task = services.project_task_repo.get_in_transaction(conn, task_id, owner_id)
    if task is None:
        raise OctopError.localized(ErrorCode.NOT_FOUND)
    assert_project_writable(services, conn, owner_id, task.project_id)
