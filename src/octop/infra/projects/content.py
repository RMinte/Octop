"""Ordered public task output, independent of provider token boundaries."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from octop.infra.projects.models import ProjectModel

ContentKind = Literal["text", "reasoning", "reasoning_summary", "tool"]
ContentStatus = Literal["streaming", "completed", "failed", "interrupted"]


class TaskContentBlock(ProjectModel):
    block_id: str = Field(description="轮次内稳定的内容块标识；追加文字和状态更新不会改变标识。")
    type: ContentKind = Field(description="回答文字、可展示思考、供应商思考摘要或工具活动。")
    text: str = Field(default="", description="该块的累计公开文字；不含私有推理、工具参数或输出。")
    status: ContentStatus = Field(description="块状态；completed 只表示该块结束，不表示整轮完成。")
    tool_call_id: str | None = None
    tool_name: str | None = None


class TaskContent:
    def __init__(self, turn_id: str) -> None:
        self.turn_id = turn_id
        self.blocks: list[TaskContentBlock] = []
        self.revision = 0
        self._text_key = ""
        self._text: TaskContentBlock | None = None
        self._tools: dict[str, TaskContentBlock] = {}

    def end_text(self) -> None:
        if self._text is not None:
            self._text.status = "completed"
            self._text = None
            self.revision += 1

    def append(self, kind: ContentKind, text: str, key: str = "") -> None:
        if not text:
            return
        if self._text is None or self._text.type != kind or self._text_key != key:
            self.end_text()
            self._text = self._new(kind)
            self._text_key = key
        self._text.text += text
        self.revision += 1

    def _new(self, kind: ContentKind, **fields: Any) -> TaskContentBlock:
        block = TaskContentBlock(
            block_id=f"{self.turn_id}:{len(self.blocks) + 1}",
            type=kind,
            status="streaming",
            **fields,
        )
        self.blocks.append(block)
        return block

    def start_tool(self, call_id: str, name: str) -> None:
        if call_id in self._tools:
            return
        self.end_text()
        self._tools[call_id] = self._new("tool", tool_call_id=call_id or None, tool_name=name)
        self.revision += 1

    def end_tool(self, call_id: str, *, failed: bool = False) -> None:
        block = self._tools.get(call_id)
        if block is not None and block.status == "streaming":
            block.status = "failed" if failed else "completed"
            self.revision += 1

    def finish(self, *, completed: bool) -> None:
        for block in self.blocks:
            if block.status == "streaming":
                block.status = "completed" if completed and block.type != "tool" else "interrupted"
                self.revision += 1
        self._text = None
        self._tools.clear()

    def dump(self) -> list[dict[str, Any]]:
        return [block.model_dump() for block in self.blocks]
