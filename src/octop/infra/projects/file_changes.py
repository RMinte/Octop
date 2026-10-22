"""File-change receipts from actual workspace state around authorized tools."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from difflib import unified_diff
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

from octop_harness.backends.workspace import is_system_logical_entry
from pydantic import Field, ValidationError

from octop.infra.projects.models import ProjectModel

if TYPE_CHECKING:
    from langchain_core.messages import ToolMessage
    from octop_harness.backends.workspace import BackendWorkspace

FILE_CHANGES_METADATA = "octop_task_file_changes"
MAX_SNAPSHOT_CHARACTERS = 256 * 1024
MAX_SNAPSHOT_LINES = 10_000
FILE_TOOLS = frozenset({"write_file", "edit_file", "delete"})


class TaskFileChange(ProjectModel):
    path: str = Field(description="工作区相对展示路径；仅记录实际成功的文件工具调用。")
    additions: int | None = Field(
        default=None, ge=0, description="真实文本差异的新增行数；未知为空。"
    )
    deletions: int | None = Field(
        default=None, ge=0, description="真实文本差异的删除行数；未知为空。"
    )
    diff: str | None = Field(
        default=None, description="执行前后文本生成的统一差异；无法读取或过大时为空。"
    )


@dataclass(frozen=True)
class FileSnapshot:
    path: str
    text: str | None
    exists: bool | None = None


def change_path(name: str, arguments: Mapping[str, Any]) -> str | None:
    if name not in FILE_TOOLS:
        return None
    raw = arguments.get("file_path")
    if (
        not isinstance(raw, str)
        or not raw
        or "\\" in raw
        or raw.startswith("~")
        or any(ord(character) < 32 for character in raw)
    ):
        return None
    # File tools use a virtual leading slash. Never pass that absolute spelling
    # to workspace reads, which also support host-absolute read fallbacks.
    path = PurePosixPath(raw.lstrip("/"))
    if not path.parts or ".." in path.parts or path.parts[0].casefold() in {".octop", "inbound"}:
        return None
    return path.as_posix()


async def snapshot(workspace: BackendWorkspace | None, path: str) -> FileSnapshot:
    # The facade remaps these logical names to the agent's system tree on reads,
    # while file tools address the workspace root. Do not read a different file.
    if workspace is None or path != path.strip() or is_system_logical_entry(path.split("/", 1)[0]):
        return FileSnapshot(path, None)
    try:
        if not await workspace.aexists(path):
            return FileSnapshot(path, "", False)
        text = await workspace.aread_text(path, limit=MAX_SNAPSHOT_CHARACTERS + 1)
        if (
            text is None
            or len(text) > MAX_SNAPSHOT_CHARACTERS
            or "\x00" in text
            or text.count("\n") > MAX_SNAPSHOT_LINES
        ):
            return FileSnapshot(path, None)
        return FileSnapshot(path, text, True)
    except Exception:
        # Display receipts must never prevent or fail an authorized file action.
        return FileSnapshot(path, None)


def _difference(before: FileSnapshot, after: FileSnapshot) -> list[TaskFileChange]:
    if before.text is None or after.text is None:
        return [TaskFileChange(path=before.path)]
    if before.text == after.text and before.exists == after.exists:
        return []
    lines = list(
        unified_diff(
            before.text.splitlines(keepends=True),
            after.text.splitlines(keepends=True),
            fromfile=f"a/{before.path}",
            tofile=f"b/{before.path}",
        )
    )
    additions = sum(line.startswith("+") for line in lines[2:])
    deletions = sum(line.startswith("-") for line in lines[2:])
    rendered = "".join(
        line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in lines
    )
    return [
        TaskFileChange(path=before.path, additions=additions, deletions=deletions, diff=rendered)
    ]


async def changes(workspace: BackendWorkspace | None, before: FileSnapshot) -> list[TaskFileChange]:
    try:
        after = await snapshot(workspace, before.path)
        return await asyncio.to_thread(_difference, before, after)
    except Exception:
        return [TaskFileChange(path=before.path)]


def recorded_file_changes(message: ToolMessage) -> list[TaskFileChange] | None:
    encoded = (
        message.artifact.get(FILE_CHANGES_METADATA) if isinstance(message.artifact, dict) else None
    )
    if not isinstance(encoded, list):
        return None
    try:
        return [TaskFileChange.model_validate(change) for change in encoded]
    except ValidationError:
        return None
