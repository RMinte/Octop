"""Public metadata for immutable image inputs owned by a task."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from octop.infra.projects.models import ProjectModel


class TaskInputAttachment(ProjectModel):
    attachment_id: str
    name: str
    media_type: str
    byte_count: int
    sha256: str
    preview_url: str = Field(description="需要当前账号鉴权的图片预览地址；不包含令牌。")
    created_at: datetime
