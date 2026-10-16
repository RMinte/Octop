"""Public metadata for immutable file inputs owned by a task."""

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
    preview_url: str = Field(description="需要当前账号鉴权的附件原始内容地址；不包含令牌。")
    workspace_path: str | None = Field(
        default=None, description="普通文件在本轮服务端工作目录中的相对路径；视觉图片为 null。"
    )
    created_at: datetime
