"""Translate this task's host workspace prefix for virtual filesystem tools."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from octop.i18n import tr

_PATH_FIELDS = {
    "read_file": ("file_path",),
    "write_file": ("file_path",),
    "edit_file": ("file_path",),
    "delete": ("file_path",),
    "ls": ("path",),
    "glob": ("path",),
    "grep": ("path",),
}


class TaskFilePathMiddleware(AgentMiddleware[Any, Any]):
    def __init__(self, workspace_dir: str | Path, locale: str = "en") -> None:
        root = str(workspace_dir)
        self._windows = bool(PureWindowsPath(root).drive)
        self._root = PureWindowsPath(root) if self._windows else PurePosixPath(root)
        self._locale = locale

    def _path(self, value: str) -> str:
        path = PureWindowsPath(value) if self._windows else PurePosixPath(value)
        # Never normalize traversal into an apparently safe workspace path.
        if ".." in path.parts or ".." in value.replace("\\", "/").split("/"):
            raise ValueError("parent traversal")
        if not path.is_absolute() or not path.is_relative_to(self._root):
            return value
        relative = path.relative_to(self._root)
        return "/" + "/".join(relative.parts)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        call = request.tool_call
        fields = _PATH_FIELDS.get(call["name"], ())
        if not fields:
            return await handler(request)
        arguments = dict(call["args"])
        try:
            for field in fields:
                value = arguments.get(field)
                if isinstance(value, str):
                    arguments[field] = self._path(value)
        except ValueError:
            return ToolMessage(
                content=tr("errors.FORBIDDEN", self._locale),
                name=call["name"],
                tool_call_id=str(call["id"]),
                status="error",
            )
        return await handler(request.override(tool_call={**call, "args": arguments}))
