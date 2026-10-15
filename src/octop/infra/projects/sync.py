"""Independent project deltas and lazy task-list pagination."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Literal

from pydantic import Field, ValidationError

from octop.infra.agentcomputer.identity import IdentityError, get_or_create_identity
from octop.infra.db.repos.project_sync import ListChange, task_scope
from octop.infra.db.repos.project_tasks import ProjectTaskRow
from octop.infra.db.repos.projects import ProjectRow
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects.models import ProjectModel, ProjectView
from octop.infra.projects.service import _view
from octop.infra.projects.tasks import TaskSummary, summary


class ProjectChange(ProjectModel):
    operation: Literal["upsert"] = "upsert"
    project_id: str
    data: ProjectView


class ProjectSyncPage(ProjectModel):
    mode: Literal["snapshot", "delta"]
    base_sync_cursor: str | None
    next_sync_cursor: str | None
    next_page_cursor: str | None
    changes: list[ProjectChange]


class TaskListPage(ProjectModel):
    items: list[TaskSummary]
    next_page_cursor: str | None = Field(description="历史任务下一页位置；null 表示已到末尾。")
    sync_cursor: str | None = Field(description="仅首页返回的增量基线，不表示历史任务已全部下载。")


class TaskChange(ProjectModel):
    operation: Literal["upsert"] = "upsert"
    task_id: str
    list_revision: int
    data: TaskSummary


class TaskSyncPage(ProjectModel):
    base_sync_cursor: str
    next_sync_cursor: str | None = Field(description="本批最后一页才返回可提交的同步位置。")
    next_page_cursor: str | None = Field(description="本批变更下一页，与历史任务分页游标不可混用。")
    changes: list[TaskChange]


class _Cursor(ProjectModel):
    position: int = Field(default=0, ge=0)
    cutoff: int = Field(default=0, ge=0)
    after: str = Field(default="", max_length=256)
    updated_at: int = Field(default=0, ge=0)


def _reset() -> OctopError:
    return OctopError.localized(ErrorCode.PROJECT_SYNC_RESET)


@dataclass(frozen=True)
class _Codec:
    context: str
    key: bytes

    def _signature(self, kind: str, raw: bytes) -> bytes:
        # Bind scope and purpose without carrying either in the URL.
        message = self.context.encode() + b"\0" + kind.encode() + b"\0" + raw
        return hmac.new(self.key, message, hashlib.sha256).digest()[: 8 if kind == "sync" else 16]

    def encode(self, kind: Literal["sync", "history", "delta"], **values: int | str) -> str:
        cursor = _Cursor.model_validate(values)
        if kind == "sync":
            raw = cursor.position.to_bytes(max(1, (cursor.position.bit_length() + 7) // 8), "big")
        elif kind == "history":
            raw = cursor.updated_at.to_bytes(8, "big") + cursor.after.encode()
        else:
            raw = (
                cursor.position.to_bytes(8, "big")
                + cursor.cutoff.to_bytes(8, "big")
                + cursor.after.encode()
            )
        return base64.urlsafe_b64encode(raw + self._signature(kind, raw)).decode().rstrip("=")

    def decode(self, token: str, kind: Literal["sync", "history", "delta"]) -> _Cursor:
        try:
            if len(token) > 4096:
                raise ValueError("cursor too long")
            data = base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True)
            if base64.urlsafe_b64encode(data).decode().rstrip("=") != token:
                raise ValueError("cursor encoding")
            signature_size = 8 if kind == "sync" else 16
            raw, signature = data[:-signature_size], data[-signature_size:]
            if not hmac.compare_digest(signature, self._signature(kind, raw)):
                raise ValueError("cursor signature")
            if kind == "sync":
                if not 1 <= len(raw) <= 8 or (len(raw) > 1 and raw[0] == 0):
                    raise ValueError("cursor size")
                return _Cursor(position=int.from_bytes(raw, "big"))
            offset = 8 if kind == "history" else 16
            if len(raw) < offset:
                raise ValueError("cursor size")
            after = raw[offset:].decode()
            if kind == "history":
                return _Cursor(updated_at=int.from_bytes(raw[:8], "big"), after=after)
            return _Cursor(
                position=int.from_bytes(raw[:8], "big"),
                cutoff=int.from_bytes(raw[8:16], "big"),
                after=after,
            )
        except (ValueError, TypeError, ValidationError, binascii.Error) as exc:
            raise _reset() from exc


def _codec(services: SharedServices, owner_id: int, scope: str) -> _Codec:
    try:
        identity = get_or_create_identity(services.paths.agentcomputer_identity)
    except (IdentityError, OSError) as exc:
        raise OctopError.localized(ErrorCode.CONTROL_PLANE_BUSY) from exc
    secret = services.secret_repo.get("jwt")
    assert secret is not None
    return _Codec(
        context=f"v2:{identity.boxID}:{identity.restoreEpoch}:{owner_id}:{scope}",
        key=hmac.new(secret, b"octop-list-sync-v2", hashlib.sha256).digest(),
    )


def task_page(
    services: SharedServices,
    owner_id: int,
    project_id: str | None,
    *,
    limit: int = 50,
    page_cursor: str | None = None,
) -> TaskListPage:
    codec = _codec(services, owner_id, task_scope(project_id))
    cursor = codec.decode(page_cursor, "history") if page_cursor is not None else None
    boundary = (cursor.updated_at, cursor.after) if cursor else None
    with services.db.read_snapshot() as conn:
        if (
            project_id is not None
            and services.project_repo.get_in_transaction(conn, project_id, owner_id) is None
        ):
            raise OctopError.localized(ErrorCode.NOT_FOUND)
        head = services.project_sync_repo.head(conn, owner_id, task_scope(project_id))
        rows = services.project_task_repo.page(conn, owner_id, project_id, limit + 1, boundary)
    items = rows[:limit]
    next_page = None
    if len(rows) > limit:
        last = items[-1]
        next_page = codec.encode("history", updated_at=last.updated_at, after=last.task_id)
    return TaskListPage(
        items=[summary(row) for row in items],
        next_page_cursor=next_page,
        sync_cursor=codec.encode("sync", position=head) if cursor is None else None,
    )


def _delta(
    services: SharedServices,
    owner_id: int,
    scope: str,
    codec: _Codec,
    sync_cursor: str,
    page_cursor: str | None,
    limit: int,
) -> tuple[list[ListChange], str | None, str | None]:
    base = codec.decode(sync_cursor, "sync")
    page = codec.decode(page_cursor, "delta") if page_cursor is not None else None
    with services.db.read_snapshot() as conn:
        if (
            scope.startswith("tasks:")
            and scope != "tasks:"
            and services.project_repo.get_in_transaction(
                conn, scope.removeprefix("tasks:"), owner_id
            )
            is None
        ):
            raise OctopError.localized(ErrorCode.NOT_FOUND)
        head = services.project_sync_repo.head(conn, owner_id, scope)
        cutoff = head if page is None else page.cutoff
        if base.position > cutoff or cutoff > head or (page and page.position != base.position):
            raise _reset()
        rows = services.project_sync_repo.changes(
            conn,
            owner_id,
            scope,
            base.position,
            cutoff,
            page.after if page else "",
            limit + 1,
        )
    more = len(rows) > limit
    selected = rows[:limit]
    return (
        selected,
        None if more else codec.encode("sync", position=cutoff),
        codec.encode("delta", position=base.position, cutoff=cutoff, after=selected[-1].entity_id)
        if more
        else None,
    )


def project_sync(
    services: SharedServices,
    owner_id: int,
    *,
    sync_cursor: str | None = None,
    page_cursor: str | None = None,
    limit: int = 50,
) -> ProjectSyncPage:
    codec = _codec(services, owner_id, "projects")
    if sync_cursor is None:
        if page_cursor is not None:
            raise _reset()
        with services.db.read_snapshot() as conn:
            head = services.project_sync_repo.head(conn, owner_id, "projects")
            projects = services.project_repo.list_in_transaction(conn, owner_id)
        return ProjectSyncPage(
            mode="snapshot",
            base_sync_cursor=None,
            next_sync_cursor=codec.encode("sync", position=head),
            next_page_cursor=None,
            changes=[ProjectChange(project_id=row.project_id, data=_view(row)) for row in projects],
        )
    rows, next_sync, next_page = _delta(
        services,
        owner_id,
        "projects",
        codec,
        sync_cursor,
        page_cursor,
        limit,
    )
    return ProjectSyncPage(
        mode="delta",
        base_sync_cursor=sync_cursor,
        next_sync_cursor=next_sync,
        next_page_cursor=next_page,
        changes=[
            ProjectChange(
                project_id=row.entity_id,
                data=_view(ProjectRow.from_row(json.loads(row.payload_json))),
            )
            for row in rows
        ],
    )


def task_sync(
    services: SharedServices,
    owner_id: int,
    project_id: str | None,
    *,
    sync_cursor: str,
    page_cursor: str | None = None,
    limit: int = 50,
) -> TaskSyncPage:
    scope = task_scope(project_id)
    codec = _codec(services, owner_id, scope)
    rows, next_sync, next_page = _delta(
        services,
        owner_id,
        scope,
        codec,
        sync_cursor,
        page_cursor,
        limit,
    )
    changes = []
    for row in rows:
        data = summary(ProjectTaskRow.from_row(json.loads(row.payload_json)))
        changes.append(
            TaskChange(task_id=row.entity_id, list_revision=data.list_revision, data=data)
        )
    return TaskSyncPage(
        base_sync_cursor=sync_cursor,
        next_sync_cursor=next_sync,
        next_page_cursor=next_page,
        changes=changes,
    )
