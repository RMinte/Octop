"""Task-owned workspace and human workbench scope, independent of expert storage."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import mimetypes
import os
from collections.abc import Awaitable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import parse_qs, urlsplit
from weakref import WeakValueDictionary

from octop_harness.backends import resolve_backend
from octop_harness.backends.workspace import BackendWorkspace
from pydantic import Field

from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects.context import task_backend
from octop.infra.projects.models import ProjectModel
from octop.infra.projects.service import _allowed_root
from octop.infra.utils.host_dirs import assert_safe_host_path

_locks: WeakValueDictionary[tuple[str, ...], asyncio.Lock] = WeakValueDictionary()


def scope_lock(*key: str) -> asyncio.Lock:
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock


async def workspace_io[T](operation: Awaitable[T]) -> T:
    try:
        return await operation
    except (PermissionError, ValueError) as exc:
        raise OctopError.localized(ErrorCode.FORBIDDEN) from exc
    except FileNotFoundError as exc:
        raise OctopError.localized(ErrorCode.NOT_FOUND) from exc


class WorkbenchCapabilities(ProjectModel):
    files: bool = True
    browser: bool
    terminal: bool
    recording: bool
    tool_ui: bool = True


class WorkbenchView(ProjectModel):
    task_id: str
    scope_revision: str
    server_directory: str
    client_directory: str | None
    capabilities: WorkbenchCapabilities
    read_only: bool


class ScopeRequest(ProjectModel):
    expected_scope_revision: str = Field(min_length=1, max_length=128)


class FileWrite(ScopeRequest):
    path: str = Field(min_length=1, max_length=4096)
    content: str = Field(max_length=8 * 1024 * 1024)
    expected_revision: str | None = None


class PathRequest(ScopeRequest):
    path: str = Field(min_length=1, max_length=4096)


class FileMove(PathRequest):
    destination: str = Field(min_length=1, max_length=4096)


class WorkspaceFile(ProjectModel):
    path: str
    content: str
    size: int
    revision: str


class WorkspaceEntry(ProjectModel):
    path: str
    name: str
    is_dir: bool
    size: int = 0
    modified_time: str = ""
    children: list[WorkspaceEntry] = Field(default_factory=list)
    modified_at: Any | None = None


@dataclass(frozen=True)
class WorkbenchScope:
    view: WorkbenchView
    workspace: BackendWorkspace
    runtime_dir: Path

    @property
    def browser_profile(self) -> str:
        return (
            "task-"
            + hashlib.sha256(self.view.task_id.encode()).hexdigest()[:16]
            + "-"
            + self.view.scope_revision[:24]
        )


def path_in_workspace(path: str, *, mutable: bool = False) -> str:
    value = path.strip().replace("\\", "/")
    if ":" in value or PureWindowsPath(value).drive or any(ord(c) < 32 for c in value):
        raise OctopError.localized(ErrorCode.FORBIDDEN)
    relative = PurePosixPath(value.lstrip("/") or ".")
    if ".." in relative.parts or (relative.parts and relative.parts[0].casefold() == ".octop"):
        raise OctopError.localized(ErrorCode.FORBIDDEN)
    if relative.parts and relative.parts[0].casefold() == "_builtin_skills" and mutable:
        raise OctopError.localized(ErrorCode.FORBIDDEN)
    if mutable and str(relative) == ".":
        raise OctopError.localized(ErrorCode.FORBIDDEN)
    return str(relative)


async def public_path(scope: WorkbenchScope, path: str, *, mutable: bool = False) -> str:
    relative = path_in_workspace(path, mutable=mutable)

    def check() -> None:
        # Task workspaces use the local project backend. Resolve metadata only;
        # content I/O remains on BackendWorkspace, including its path guards.
        root = Path(scope.view.server_directory).resolve()
        target = (root / relative).resolve()
        try:
            resolved = target.relative_to(root)
        except ValueError as exc:
            raise OctopError.localized(ErrorCode.FORBIDDEN) from exc
        if resolved.parts and resolved.parts[0].casefold() == ".octop":
            raise OctopError.localized(ErrorCode.FORBIDDEN)
        if mutable and (not resolved.parts or resolved.parts[0].casefold() == "_builtin_skills"):
            raise OctopError.localized(ErrorCode.FORBIDDEN)

    await asyncio.to_thread(check)
    return relative


def resolve(
    services: SharedServices,
    owner_id: int,
    task_id: str,
    *,
    expected_scope_revision: str | None = None,
    mutable: bool = False,
    permission: str | None = None,
) -> WorkbenchScope:
    task = services.project_task_repo.get(task_id, owner_id)
    user = services.user_repo.get(owner_id)
    if task is None or user is None or user.disabled:
        raise OctopError.localized(ErrorCode.NOT_FOUND)

    def can(key: str) -> bool:
        return user.role == "admin" or key in user.permissions

    if permission is not None and not can(permission):
        raise OctopError.localized(ErrorCode.FORBIDDEN)
    project = services.project_repo.get(task.project_id, owner_id) if task.project_id else None
    if task.project_id and project is None:
        raise OctopError.localized(ErrorCode.NOT_FOUND)
    read_only = task.archived or bool(project and project.archived)
    if mutable and read_only:
        raise OctopError.localized(ErrorCode.TASK_ARCHIVED)
    current = (
        services.project_task_turn_repo.get(task.active_turn_id) if task.active_turn_id else None
    )
    raw = json.loads(current.configuration_json) if current else {}
    directory = raw.get("directory") if current else project.directory if project else None
    client = (
        raw.get("client_directory") if current else project.client_directory if project else None
    )
    runtime_dir = services.paths.root / "project-tasks" / task_id
    if directory:
        try:
            workspace_dir = assert_safe_host_path(
                str(directory),
                restrict_to_home=False,
                restrict_to_root=_allowed_root(services, owner_id),
            )
            if not workspace_dir.is_dir():
                raise ValueError("workspace unavailable")
        except (ValueError, OSError) as exc:
            raise OctopError.localized(ErrorCode.TASK_RESOURCE_UNAVAILABLE) from exc
    else:
        workspace_dir = runtime_dir / "workspace"
        workspace_dir.mkdir(parents=True, exist_ok=True)
    revision = hashlib.sha256(
        json.dumps(
            [
                owner_id,
                task_id,
                str(workspace_dir),
                client,
                task.configuration_revision,
                raw.get("revision") if current else project.revision if project else None,
                read_only,
                user.role,
                sorted(user.permissions),
            ],
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    if expected_scope_revision is not None and expected_scope_revision != revision:
        raise OctopError.localized(ErrorCode.TASK_REVISION_CONFLICT)
    view = WorkbenchView(
        task_id=task_id,
        scope_revision=revision,
        server_directory=str(workspace_dir),
        client_directory=client,
        read_only=read_only,
        capabilities=WorkbenchCapabilities(
            browser=can("browser") and importlib.util.find_spec("octop_browser") is not None,
            terminal=can("terminal") and os.name == "posix",
            recording=can("browser") and importlib.util.find_spec("octop_browser") is not None,
        ),
    )
    workspace = BackendWorkspace(
        resolve_backend(task_backend(workspace_dir, runtime_dir), workspace_dir=workspace_dir),
        workspace_dir,
        system_files_path=".octop/workbench",
    )
    return WorkbenchScope(view, workspace, runtime_dir)


async def tree(scope: WorkbenchScope, path: str) -> list[WorkspaceEntry]:
    relative = await public_path(scope, path)
    result = await workspace_io(scope.workspace.als(relative))
    if result is None:
        raise OctopError.localized(ErrorCode.NOT_FOUND)
    entries = getattr(result, "entries", [])
    rows = []
    for entry in entries:
        name = str(entry.get("path", "")).replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        if not name or name.casefold() == ".octop":
            continue
        child = name if relative == "." else f"{relative}/{name}"
        rows.append(
            WorkspaceEntry(
                path=child,
                name=name,
                is_dir=bool(entry.get("is_dir")),
                size=entry.get("size") or 0,
                modified_at=entry.get("modified_at"),
                modified_time=str(entry.get("modified_at") or ""),
            )
        )
    return rows


async def download(scope: WorkbenchScope, path: str) -> tuple[bytes, str, str]:
    relative = await public_path(scope, path)
    data = await workspace_io(scope.workspace.adownload_bytes(relative))
    if data is None:
        raise OctopError.localized(ErrorCode.NOT_FOUND)
    return (
        data,
        mimetypes.guess_type(relative)[0] or "application/octet-stream",
        PurePosixPath(relative).name,
    )


async def read(scope: WorkbenchScope, path: str) -> WorkspaceFile:
    data, _, _ = await download(scope, path)
    if len(data) > 8 * 1024 * 1024:
        raise OctopError.localized(ErrorCode.PROJECT_RESOURCE_INVALID)
    try:
        text = data.decode("utf-8")
    except UnicodeError as exc:
        raise OctopError.localized(ErrorCode.PROJECT_RESOURCE_INVALID) from exc
    return WorkspaceFile(
        path=path_in_workspace(path),
        content=text,
        size=len(data),
        revision=hashlib.sha256(data).hexdigest(),
    )


async def write(scope: WorkbenchScope, body: FileWrite) -> WorkspaceFile:
    relative = await public_path(scope, body.path, mutable=True)
    data = body.content.encode()
    async with scope_lock("file", scope.view.server_directory, relative):
        if body.expected_revision is not None:
            existing = await workspace_io(scope.workspace.adownload_bytes(relative))
            revision = hashlib.sha256(existing).hexdigest() if existing is not None else "missing"
            if revision != body.expected_revision:
                raise OctopError.localized(ErrorCode.TASK_REVISION_CONFLICT)
        await workspace_io(scope.workspace.aupload_bytes(relative, data))
    return WorkspaceFile(
        path=relative,
        content=body.content,
        size=len(data),
        revision=hashlib.sha256(data).hexdigest(),
    )


async def tool_request(
    services: SharedServices,
    owner_id: int,
    task_id: str,
    turn_id: str,
    call_id: str,
    *,
    path: str,
    method: str,
    body: dict[str, Any],
    expected_scope_revision: str,
) -> Any:
    scope = await asyncio.to_thread(
        resolve,
        services,
        owner_id,
        task_id,
        expected_scope_revision=expected_scope_revision,
        mutable=method != "GET",
    )
    turn = await asyncio.to_thread(services.project_task_turn_repo.get, turn_id)
    blocks = (
        json.loads(turn.content_blocks_json) if turn is not None and turn.task_id == task_id else []
    )
    if not any(
        block.get("tool_call_id") == call_id and block.get("type") == "tool" for block in blocks
    ):
        raise OctopError.localized(ErrorCode.NOT_FOUND)
    assert turn is not None
    configuration = json.loads(turn.configuration_json)
    if configuration.get("branch_origin", {}).get("history_only"):
        raise OctopError.localized(ErrorCode.FORBIDDEN)
    if method != "GET":
        task = await asyncio.to_thread(services.project_task_repo.get, task_id, owner_id)
        directory = configuration.get("directory") or str(scope.runtime_dir / "workspace")
        if (
            task is None
            or task.action_mode != "craft"
            or configuration.get("action_mode", "craft") != "craft"
            or configuration.get("configuration_revision", 1) != task.configuration_revision
            or str(Path(directory)) != scope.view.server_directory
        ):
            raise OctopError.localized(ErrorCode.TASK_REVISION_CONFLICT)
    parsed = urlsplit(path)
    target = parsed.path.removeprefix(f"/project-tasks/{task_id}")
    if parsed.scheme or parsed.netloc or not target.startswith("/workspace/"):
        raise OctopError.localized(ErrorCode.FORBIDDEN)
    query = parse_qs(parsed.query)
    relative = str(body.get("path") or query.get("path", [""])[0])
    if target == "/workspace/file" and method == "GET":
        return await read(scope, relative)
    if target == "/workspace/tree-children" and method == "GET":
        return await tree(scope, relative or ".")
    if target == "/workspace/file" and method == "PUT":
        return await write(
            scope,
            FileWrite.model_validate(
                {**body, "path": relative, "expected_scope_revision": expected_scope_revision}
            ),
        )
    raise OctopError.localized(ErrorCode.FORBIDDEN)
