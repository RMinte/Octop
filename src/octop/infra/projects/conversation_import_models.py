"""Visible external history supplied by clients, without host file access or execution."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, Field, StrictBool, StringConstraints, model_validator

from octop.infra.projects.input_attachment_models import TaskInputAttachment
from octop.infra.projects.models import Name, ProjectModel, Reference

ImportSource = Literal["codex", "claude"]
HistoryText = Annotated[str, StringConstraints(max_length=1024 * 1024)]


class MissingImportAttachment(ProjectModel):
    name: str = Field(min_length=1, max_length=256)
    media_type: str | None = Field(default=None, max_length=128)


class ImportMessage(ProjectModel):
    text: HistoryText = ""
    timestamp: AwareDatetime | None = None
    attachment_ids: list[Reference] = Field(
        default_factory=list,
        max_length=9,
        description="先用任务附件上传接口上传字节，再传本人附件 ID；不接受本机文件 URL。",
    )
    missing_attachments: list[MissingImportAttachment] = Field(
        default_factory=list, max_length=100, description="无法上传的原附件仅保留名称，不读取路径。"
    )

    @model_validator(mode="after")
    def unique_attachments(self) -> Self:
        if len(self.attachment_ids) != len(set(self.attachment_ids)):
            raise ValueError("duplicate attachment IDs")
        return self


class ImportActivity(ProjectModel):
    kind: Literal["commentary", "tool"] = Field(
        description="仅可见进展和工具历史，不接收私有推理。"
    )
    name: str | None = Field(default=None, min_length=1, max_length=256)
    detail: HistoryText | None = None
    output: HistoryText | None = None
    succeeded: StrictBool = True
    timestamp: AwareDatetime | None = None
    duration_ms: float | None = Field(default=None, ge=0, le=31536000000, allow_inf_nan=False)
    exit_code: int | None = None

    @model_validator(mode="after")
    def tool_has_name(self) -> Self:
        if self.kind == "tool" and not self.name:
            raise ValueError("tool history requires a name")
        return self


class ImportFileChange(ProjectModel):
    path: str = Field(
        min_length=1, max_length=4096, description="来源展示路径；不读取或写入该路径。"
    )
    old_content: HistoryText | None = None
    new_content: HistoryText | None = None


class ImportTurn(ProjectModel):
    external_turn_id: Reference
    user_message: ImportMessage
    assistant_message: ImportMessage
    activities: list[ImportActivity] = Field(default_factory=list, max_length=1000)
    file_changes: list[ImportFileChange] = Field(default_factory=list, max_length=1000)
    completed_at: AwareDatetime | None = None


class ConversationImport(ProjectModel):
    source: ImportSource
    external_session_id: Reference = Field(description="来源内稳定会话 ID；按当前账号与来源去重。")
    project_id: Reference | None = Field(
        default=None, description="已导入或现有的本人 Octop 项目；null 为无项目任务。"
    )
    title: Name
    source_updated_at: AwareDatetime
    source_revision: str | None = Field(default=None, max_length=512)
    turns: list[ImportTurn] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def bounded_history(self) -> Self:
        if len({turn.external_turn_id for turn in self.turns}) != len(self.turns):
            raise ValueError("duplicate source turn IDs")
        if len(self.model_dump_json().encode()) > 16 * 1024 * 1024:
            raise ValueError("import exceeds 16 MiB; split large source conversations")
        return self


class ImportedTurnHistory(ProjectModel):
    source: ImportSource
    external_turn_id: str
    user_timestamp: AwareDatetime | None
    assistant_timestamp: AwareDatetime | None
    activities: list[ImportActivity]
    file_changes: list[ImportFileChange]
    assistant_attachments: list[TaskInputAttachment] = Field(default_factory=list)
    missing_user_attachments: list[MissingImportAttachment] = Field(default_factory=list)
    missing_assistant_attachments: list[MissingImportAttachment] = Field(default_factory=list)
