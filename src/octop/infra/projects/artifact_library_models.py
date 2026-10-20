"""Metadata contracts for browsing published task files across conversations."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from octop.infra.projects.artifact_models import TaskArtifact
from octop.infra.projects.models import ProjectModel
from octop.infra.projects.tasks import TurnStatus
from octop.infra.utils.artifact_kinds import ArtifactKind, ascii_lower

LibraryScope = Literal["all", "project", "standalone"]
LibraryArchive = Literal["all", "active", "archived"]
LibraryKind = ArtifactKind | Literal["all"]
LibraryBusinessCategory = Literal["all", "report", "data", "workflow", "uncategorized"]


class ArtifactLibraryQuery(ProjectModel):
    scope: LibraryScope = "all"
    project_id: str | None = Field(default=None, min_length=1, max_length=256)
    task_id: str | None = Field(default=None, min_length=1, max_length=256)
    kind: LibraryKind = "all"
    business_category: LibraryBusinessCategory = "all"
    archived: LibraryArchive = "all"
    q: str = Field(default="", max_length=200)
    limit: int = Field(default=50, ge=1, le=100)
    page_cursor: str | None = Field(default=None, min_length=1, max_length=4096)

    @field_validator("q")
    @classmethod
    def normalize_query(cls, value: str) -> str:
        if "\0" in value:
            raise ValueError("file name search must not contain NUL")
        return ascii_lower(value.strip())

    @model_validator(mode="after")
    def project_scope(self) -> Self:
        if (self.scope == "project") != (self.project_id is not None):
            raise ValueError("project_id is required only for scope=project")
        return self


class TaskArtifactLibraryItem(TaskArtifact):
    file_kind: ArtifactKind = Field(description="固定规则计算的文件浏览分类，不代表真实格式校验。")
    task_title: str = Field(description="来源任务的当前标题。")
    task_archived: bool = Field(description="来源任务当前是否归档。")
    turn_status: TurnStatus = Field(
        description="来源轮次当前的公开状态；发布后失败或取消仍保留文件。"
    )
    project_id: str | None = Field(description="来源项目 ID；无项目任务为 null。")
    project_name: str | None = Field(description="来源项目的当前名称；无项目任务为 null。")
    project_archived: bool | None = Field(description="来源项目当前是否归档；无项目任务为 null。")


class TaskArtifactLibraryPage(ProjectModel):
    items: list[TaskArtifactLibraryItem]
    next_page_cursor: str | None = Field(description="按发布时间倒序的下一页位置；末页为 null。")
