"""Server-owned immutable task attachments, published only by explicit tools."""

from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import stat
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Annotated, Any

from langchain_core.tools import InjectedToolCallId, StructuredTool
from octop_harness.backends.workspace import BackendWorkspace

from octop.i18n import tr
from octop.infra.db.repos.project_task_artifacts import ProjectTaskArtifactRow
from octop.infra.db.repos.project_task_turns import ProjectTaskTurnRow
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects.artifact_models import (
    CreateArtifactArgs,
    PublishWorkspaceFileArgs,
    TaskArtifact,
)
from octop.infra.utils.ulid import new_ulid

MAX_ARTIFACT_BYTES = 25 * 1024 * 1024
MAX_TURN_BYTES = 50 * 1024 * 1024
MAX_ARTIFACTS_PER_TURN = 5
SERVER_ARTIFACT_TOOLS = frozenset({"create_artifact", "publish_workspace_file"})
_TEXT_EXTENSIONS = frozenset(
    {
        ".txt",
        ".md",
        ".markdown",
        ".csv",
        ".tsv",
        ".json",
        ".jsonl",
        ".html",
        ".htm",
        ".xml",
        ".yaml",
        ".yml",
        ".css",
        ".js",
        ".swift",
        ".py",
        ".rb",
        ".java",
        ".kt",
        ".c",
        ".h",
        ".cpp",
        ".hpp",
        ".rs",
        ".go",
        ".sql",
        ".toml",
        ".ini",
        ".log",
    }
)


class _CreateToolArgs(CreateArtifactArgs):
    call_id: Annotated[str, InjectedToolCallId]


class _PublishToolArgs(PublishWorkspaceFileArgs):
    call_id: Annotated[str, InjectedToolCallId]


def view(row: ProjectTaskArtifactRow) -> TaskArtifact:
    return TaskArtifact(
        artifact_id=row.artifact_id,
        task_id=row.task_id,
        turn_id=row.turn_id,
        name=row.name,
        media_type=row.media_type,
        byte_count=row.byte_count,
        sha256=row.sha256,
        source_path=row.source_path,
        download_url=f"/api/project-tasks/{row.task_id}/artifacts/{row.artifact_id}/download",
        created_at=datetime.fromtimestamp(row.created_at / 1000, UTC),
    )


def for_turns(
    services: SharedServices, conn: Any, task_id: str, turn_ids: list[str]
) -> dict[str, list[TaskArtifact]]:
    result: dict[str, list[TaskArtifact]] = {}
    for row in services.project_task_artifact_repo.for_turns(conn, task_id, turn_ids):
        result.setdefault(row.turn_id, []).append(view(row))
    return result


def download(
    services: SharedServices, owner_id: int, task_id: str, artifact_id: str
) -> tuple[TaskArtifact, bytes]:
    with services.db.read_snapshot() as conn:
        if services.project_task_repo.get_in_transaction(conn, task_id, owner_id) is None:
            raise OctopError.localized(ErrorCode.NOT_FOUND)
        stored = services.project_task_artifact_repo.download(conn, task_id, artifact_id)
    if stored is None:
        raise OctopError.localized(ErrorCode.NOT_FOUND)
    row, content = stored
    if len(content) != row.byte_count or hashlib.sha256(content).hexdigest() != row.sha256:
        raise OctopError.localized(ErrorCode.TASK_ARTIFACT_UNAVAILABLE)
    return view(row), content


class TaskArtifactPublisher:
    """Runtime-bound tools. Owner, task, turn and workspace never come from model arguments."""

    def __init__(
        self,
        services: SharedServices,
        owner_id: int,
        turn: ProjectTaskTurnRow,
        root: Path,
        locale: str,
    ) -> None:
        self.services, self.owner_id, self.turn = services, owner_id, turn
        self.root, self.locale = root, locale
        self.workspace: BackendWorkspace | None = None

    def _existing(self, conn: Any, call_id: str, request_hash: str) -> TaskArtifact | None:
        if not self.services.project_task_repo.lock_owner(conn, self.owner_id):
            raise OctopError.localized(ErrorCode.FORBIDDEN)
        task = self.services.project_task_repo.get_in_transaction(
            conn, self.turn.task_id, self.owner_id
        )
        if task is None:
            raise OctopError.localized(ErrorCode.NOT_FOUND)
        existing = self.services.project_task_artifact_repo.by_call(
            conn, self.turn.turn_id, call_id
        )
        if existing is not None:
            if existing.request_hash != request_hash:
                raise OctopError.localized(ErrorCode.TASK_ARTIFACT_CONFLICT)
            return view(existing)
        current = self.services.project_task_turn_repo.get_in_transaction(conn, self.turn.turn_id)
        if (
            current is None
            or current.task_id != task.task_id
            or current.status != "running"
            or current.cancel_requested
            or current.worker_id != self.turn.worker_id
            or current.updated_at < int(time.time() * 1000) - 90_000
        ):
            raise OctopError.localized(ErrorCode.TASK_ARTIFACT_CLOSED)
        return None

    def _lookup(self, call_id: str, request_hash: str) -> TaskArtifact | None:
        with self.services.db.transaction() as conn:
            return self._existing(conn, call_id, request_hash)

    def _save(
        self,
        call_id: str,
        request_hash: str,
        name: str,
        media_type: str,
        content: bytes,
        source_path: str | None,
    ) -> TaskArtifact:
        if len(content) > MAX_ARTIFACT_BYTES:
            raise OctopError.localized(ErrorCode.TASK_ARTIFACT_LIMIT)
        with self.services.db.transaction() as conn:
            existing = self._existing(conn, call_id, request_hash)
            if existing is not None:
                return existing
            count, size = self.services.project_task_artifact_repo.usage(conn, self.turn.turn_id)
            if count >= MAX_ARTIFACTS_PER_TURN or size + len(content) > MAX_TURN_BYTES:
                raise OctopError.localized(ErrorCode.TASK_ARTIFACT_LIMIT)
            row = ProjectTaskArtifactRow(
                "artifact_" + new_ulid(),
                self.turn.task_id,
                self.turn.turn_id,
                call_id,
                request_hash,
                name,
                media_type,
                len(content),
                hashlib.sha256(content).hexdigest(),
                source_path,
                int(time.time() * 1000),
            )
            self.services.project_task_artifact_repo.insert(conn, row, content)
        return view(row)

    @staticmethod
    def _request_hash(
        tool_name: str, arguments: CreateArtifactArgs | PublishWorkspaceFileArgs
    ) -> str:
        encoded = json.dumps(
            [tool_name, arguments.model_dump()], sort_keys=True, ensure_ascii=False
        )
        return hashlib.sha256(encoded.encode()).hexdigest()

    async def create(self, call_id: str, arguments: CreateArtifactArgs) -> TaskArtifact:
        request_hash = self._request_hash("create_artifact", arguments)
        existing = await asyncio.to_thread(self._lookup, call_id, request_hash)
        if existing is not None:
            return existing
        if PurePosixPath(arguments.name).suffix.lower() not in _TEXT_EXTENSIONS:
            raise OctopError.localized(ErrorCode.TASK_ARTIFACT_INVALID)
        return await asyncio.to_thread(
            self._save,
            call_id,
            request_hash,
            arguments.name,
            arguments.media_type or mimetypes.guess_type(arguments.name)[0] or "text/plain",
            arguments.content.encode("utf-8"),
            None,
        )

    def _source_bytes(self, relative_path: str) -> bytes:
        assert self.workspace is not None
        # Tasks currently have a host-mounted server workspace. Check metadata locally,
        # while all content reads still go through BackendWorkspace.
        root = self.root.resolve()
        source = root.joinpath(*relative_path.split("/"))

        def checked_stat() -> Any:
            current = root
            for component in relative_path.split("/"):
                current = current / component
                if current.is_symlink():
                    raise OctopError.localized(ErrorCode.TASK_ARTIFACT_INVALID)
            info = source.stat()
            if not stat.S_ISREG(info.st_mode):
                raise OctopError.localized(ErrorCode.TASK_ARTIFACT_INVALID)
            if info.st_size > MAX_ARTIFACT_BYTES:
                raise OctopError.localized(ErrorCode.TASK_ARTIFACT_LIMIT)
            return info

        try:
            before = checked_stat()
            materialized = self.workspace.materialize_local(relative_path, map_system_paths=False)
            if materialized != source:
                raise OctopError.localized(ErrorCode.TASK_ARTIFACT_INVALID)
            content = self.workspace.download_bytes(relative_path)
            after = checked_stat()
        except OSError as exc:
            raise OctopError.localized(ErrorCode.TASK_ARTIFACT_UNAVAILABLE) from exc
        if content is None:
            raise OctopError.localized(ErrorCode.TASK_ARTIFACT_UNAVAILABLE)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ) or len(content) != after.st_size:
            raise OctopError.localized(ErrorCode.TASK_ARTIFACT_UNAVAILABLE)
        return content

    async def publish(self, call_id: str, arguments: PublishWorkspaceFileArgs) -> TaskArtifact:
        request_hash = self._request_hash("publish_workspace_file", arguments)
        existing = await asyncio.to_thread(self._lookup, call_id, request_hash)
        if existing is not None:
            return existing
        if self.workspace is None:
            raise OctopError.localized(ErrorCode.TASK_ARTIFACT_UNAVAILABLE)
        content = await asyncio.to_thread(self._source_bytes, arguments.path)
        name = arguments.name or PurePosixPath(arguments.path).name
        return await asyncio.to_thread(
            self._save,
            call_id,
            request_hash,
            name,
            mimetypes.guess_type(name)[0] or "application/octet-stream",
            content,
            arguments.path,
        )

    async def _result(self, operation: Callable[[], Awaitable[TaskArtifact]]) -> str:
        try:
            artifact = await operation()
        except OctopError as exc:
            return json.dumps(
                {
                    "is_error": True,
                    "error_code": exc.code.value,
                    "message": exc.to_envelope(locale=self.locale)["error"]["message"],
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {"status": "attached", **artifact.model_dump(mode="json")}, ensure_ascii=False
        )

    def tools(self) -> list[StructuredTool]:
        async def create_artifact(
            name: str,
            content: str,
            call_id: Annotated[str, InjectedToolCallId],
            media_type: str | None = None,
        ) -> str:
            return await self._result(
                lambda: self.create(
                    call_id,
                    CreateArtifactArgs(name=name, content=content, media_type=media_type),
                )
            )

        async def publish_workspace_file(
            path: str,
            call_id: Annotated[str, InjectedToolCallId],
            name: str | None = None,
        ) -> str:
            return await self._result(
                lambda: self.publish(call_id, PublishWorkspaceFileArgs(path=path, name=name))
            )

        return [
            StructuredTool.from_function(
                coroutine=create_artifact,
                name="create_artifact",
                description=tr("projects.create_artifact_description", self.locale),
                args_schema=_CreateToolArgs,
            ),
            StructuredTool.from_function(
                coroutine=publish_workspace_file,
                name="publish_workspace_file",
                description=tr("projects.publish_workspace_file_description", self.locale),
                args_schema=_PublishToolArgs,
            ),
        ]
