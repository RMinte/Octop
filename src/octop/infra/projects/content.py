"""Ordered public task output, independent of provider token boundaries."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from octop.infra.projects.models import ProjectModel
from octop.infra.projects.progress import TaskPlan

ContentKind = Literal["text", "reasoning", "reasoning_summary", "tool", "plan", "progress"]
ContentStatus = Literal["streaming", "completed", "failed", "interrupted"]


class TaskContentBlock(ProjectModel):
    block_id: str = Field(description="轮次内稳定的内容块标识；追加文字和状态更新不会改变标识。")
    type: ContentKind = Field(
        description="回答文字、可展示思考、供应商摘要、工具活动、计划或公开进展。"
    )
    text: str = Field(default="", description="该块的累计公开文字；不含私有推理、工具参数或输出。")
    status: ContentStatus = Field(description="块状态；completed 只表示该块结束，不表示整轮完成。")
    tool_call_id: str | None = None
    tool_name: str | None = None
    tool_label: str | None = Field(default=None, description="服务端本地化的工具动作名称。")
    plan: TaskPlan | None = Field(
        default=None, description="该次计划更新的完整快照，仅 plan 块提供。"
    )
    plan_id: str | None = Field(
        default=None, description="执行时明确关联的计划；历史未关联记录为空。"
    )
    step_id: str | None = Field(
        default=None, description="执行时固定的步骤标识，不随步骤排序或当前步骤变化。"
    )
    arguments: dict[str, Any] | None = Field(
        default=None, description="工具的实际参数，不含宿主步骤元数据。"
    )
    output: Any | None = Field(
        default=None, description="工具返回的公开 content，保留其原始 JSON 结构。"
    )
    started_at: datetime | None = Field(
        default=None, description="服务端实际开始执行工具的 UTC 时间；未知为空。"
    )
    finished_at: datetime | None = Field(
        default=None, description="服务端观察到工具执行结束的 UTC 时间；未知为空。"
    )
    duration_ms: float | None = Field(
        default=None, ge=0, description="实际工具执行耗时，单位毫秒；未知为空。"
    )


class TaskContent:
    def __init__(self, turn_id: str) -> None:
        self.turn_id = turn_id
        self.blocks: list[TaskContentBlock] = []
        self.revision = 0
        self._text_key = ""
        self._text: TaskContentBlock | None = None
        self._tools: dict[str, TaskContentBlock] = {}

    @property
    def plan(self) -> TaskPlan | None:
        return next((block.plan for block in reversed(self.blocks) if block.type == "plan"), None)

    @classmethod
    def restore(cls, turn_id: str, blocks: list[Any], revision: int) -> TaskContent:
        content = cls(turn_id)
        content.blocks = [TaskContentBlock.model_validate(block) for block in blocks]
        content.revision = revision
        content._tools = {
            block.tool_call_id or "": block for block in content.blocks if block.type == "tool"
        }
        return content

    def end_text(self) -> None:
        if self._text is not None:
            self._text.status = "completed"
            self._text = None
            self.revision += 1

    def append(
        self,
        kind: ContentKind,
        text: str,
        key: str = "",
        *,
        plan_id: str | None = None,
        step_id: str | None = None,
    ) -> None:
        if not text:
            return
        if (
            self._text is None
            or self._text.type != kind
            or self._text_key != key
            or (self._text.plan_id, self._text.step_id) != (plan_id, step_id)
        ):
            self.end_text()
            self._text = self._new(kind, plan_id=plan_id, step_id=step_id)
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

    def update_plan(
        self, plan: TaskPlan, *, call_id: str | None = None, arguments: dict[str, Any] | None = None
    ) -> None:
        previous = self.plan
        if previous is not None and plan.revision <= previous.revision:
            return
        self.end_text()
        block = self._new(
            "plan",
            plan=plan.model_copy(deep=True),
            plan_id=plan.plan_id,
            tool_call_id=call_id,
            tool_name="planning" if call_id else None,
            arguments=arguments,
        )
        block.status = "completed"
        self.revision += 1

    def report_progress(
        self, call_id: str, text: str, *, plan_id: str | None = None, step_id: str | None = None
    ) -> None:
        if any(block.type == "progress" and block.tool_call_id == call_id for block in self.blocks):
            return
        self.end_text()
        block = self._new(
            "progress", text=text, tool_call_id=call_id, plan_id=plan_id, step_id=step_id
        )
        block.status = "completed"
        self.revision += 1

    def start_tool(
        self,
        call_id: str,
        name: str,
        label: str | None = None,
        *,
        plan_id: str | None = None,
        step_id: str | None = None,
        arguments: dict[str, Any] | None = None,
        started_at: datetime | str | None = None,
    ) -> None:
        existing = self._tools.get(call_id)
        if existing is not None:
            # Model intent arrives before dispatch. Only actual dispatch adds execution time;
            # once dispatched, completion must retain that original step binding.
            before = existing.model_dump()
            if existing.started_at is None and started_at is not None:
                parsed = TaskContentBlock.model_validate({**before, "started_at": started_at})
                existing.started_at = parsed.started_at
                existing.plan_id, existing.step_id = plan_id, step_id
            elif existing.started_at is None and arguments is not None:
                existing.plan_id, existing.step_id = plan_id, step_id
            if arguments is not None:
                existing.arguments = arguments
            if existing.model_dump() != before:
                self.revision += 1
            return
        self.end_text()
        self._tools[call_id] = self._new(
            "tool",
            tool_call_id=call_id or None,
            tool_name=name,
            tool_label=label,
            plan_id=plan_id,
            step_id=step_id,
            arguments=arguments,
            started_at=started_at,
        )
        self.revision += 1

    def end_tool(
        self,
        call_id: str,
        *,
        failed: bool = False,
        output: Any = None,
        finished_at: datetime | str | None = None,
        duration_ms: float | None = None,
    ) -> None:
        block = self._tools.get(call_id)
        if block is not None:
            before = block.model_dump()
            if failed or block.status == "streaming":
                block.status = "failed" if failed else "completed"
            # A later native tool_result may follow the execution-end event; it must
            # not erase the duration or output already observed by the executor.
            if output is not None:
                block.output = output
            if finished_at is not None:
                block.finished_at = TaskContentBlock.model_validate(
                    {**block.model_dump(), "finished_at": finished_at}
                ).finished_at
            if duration_ms is not None:
                block.duration_ms = duration_ms
            if block.model_dump() != before:
                self.revision += 1

    def finish(self, *, completed: bool) -> None:
        for block in self.blocks:
            if block.status == "streaming":
                block.status = "completed" if completed and block.type != "tool" else "interrupted"
                self.revision += 1
        self._text = None
        self._tools.clear()

    def dump(self) -> list[dict[str, Any]]:
        return [block.model_dump(mode="json") for block in self.blocks]
