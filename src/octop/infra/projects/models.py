"""Project contracts shared by the domain and its HTTP adapter."""

from __future__ import annotations

from datetime import datetime
from pathlib import PurePosixPath, PureWindowsPath
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StringConstraints,
    field_validator,
    model_validator,
)
from pydantic.json_schema import SkipJsonSchema

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
Reference = Annotated[str, StringConstraints(min_length=1, max_length=256)]


class ProjectModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SkillBinding(ProjectModel):
    package_id: Reference
    slug: Reference


class SkillHubBinding(ProjectModel):
    source: Literal["skillhub"]
    slug: Reference
    name: str = Field(min_length=1, max_length=256, description="SkillHub 展示名。")
    description: str = Field(default="", max_length=4096, description="SkillHub 展示简介。")

    @field_validator("slug")
    @classmethod
    def valid_slug(cls, value: str) -> str:
        from octop.infra.skills.skill_packages import validate_skill_slug

        return validate_skill_slug(value)


class ProjectFields(ProjectModel):
    name: Name = Field(description="项目名称，同一用户下唯一。")
    description: str = Field(default="", max_length=1024, description="项目简介。")
    directory: str | None = Field(
        default=None,
        min_length=1,
        max_length=4096,
        description="服务端工作目录：运行 Octop 的盒子/主机上的已有绝对路径；可为 null。按当前用户权限校验，不创建或移动目录。",
    )
    client_directory: str | None = Field(
        default=None,
        min_length=1,
        max_length=4096,
        description="客户端工作目录：用户电脑上的绝对路径，支持 macOS/Linux、Windows 盘符及 UNC 路径。仅保存位置，不在服务端检查该目录，不上传、读取或同步文件；可为 null。",
    )

    instructions: str = Field(default="", max_length=32768, description="项目自己的行动指南。")
    skills: list[SkillBinding | SkillHubBinding] = Field(
        default_factory=list,
        max_length=64,
        description="关联的多个已安装 Skill 或 SkillHub 引用；SkillHub 使用 source=skillhub，不依赖专家，也不会在保存时下载或执行技能。",
    )
    mcp_servers: list[Reference] = Field(
        default_factory=list, max_length=64, description="当前用户可用的 MCP 连接引用，不保存凭据。"
    )
    plugins: list[Reference] = Field(
        default_factory=list,
        max_length=64,
        description="已安装插件 ID；仅加载所选插件的工具、中间件和技能。",
    )
    subagents: list[Reference] = Field(
        default_factory=list, max_length=16, description="内置子代理角色 slug；不创建或关联专家。"
    )
    memory_enabled: StrictBool = Field(
        default=True, description="启用任务独立的持久记忆，不与其他任务共享。"
    )
    team_enabled: StrictBool = Field(
        default=False, description="将所选子代理角色同时作为任务内团队成员，支持同步协作。"
    )

    @field_validator("client_directory")
    @classmethod
    def absolute_client_directory(cls, value: str | None) -> str | None:
        if value is not None and (
            any(ord(char) < 32 for char in value)
            or not (PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute())
        ):
            raise ValueError("client_directory must be an absolute path without control characters")
        return value

    @model_validator(mode="after")
    def unique_references(self) -> Self:
        keys = {
            ("package", item.package_id, item.slug)
            if isinstance(item, SkillBinding)
            else ("skillhub", item.slug)
            for item in self.skills
        }
        if len(keys) != len(self.skills):
            raise ValueError("duplicate Skill reference")
        if len(set(self.mcp_servers)) != len(self.mcp_servers):
            raise ValueError("duplicate MCP reference")
        if len(set(self.plugins)) != len(self.plugins) or len(set(self.subagents)) != len(
            self.subagents
        ):
            raise ValueError("duplicate capability reference")
        if any(ord(char) < 32 for char in self.name):
            raise ValueError("invalid project name")
        return self


class ProjectUpdate(ProjectFields):
    model_config = ConfigDict(
        json_schema_extra={"required": [*ProjectFields.model_fields, "expected_revision"]}
    )
    expected_revision: Annotated[StrictInt, Field(ge=1)] = Field(
        description="编辑时读取的版本；冲突返回 409，防止覆盖其他修改。"
    )

    @model_validator(mode="after")
    def require_complete_replacement(self) -> Self:
        if not set(ProjectFields.model_fields) <= self.model_fields_set:
            raise ValueError("PUT requires every project field; use PATCH for partial changes")
        return self


class ProjectPatch(ProjectModel):
    expected_revision: Annotated[StrictInt, Field(ge=1)] = Field(
        description="编辑时读取的版本；不匹配返回 409，不覆盖其他修改。"
    )
    name: Name | SkipJsonSchema[None] = Field(
        default=None, description="省略保持原值；不可为 null。"
    )
    description: Annotated[str, Field(max_length=1024)] | SkipJsonSchema[None] = None
    directory: str | None = Field(
        default=None,
        min_length=1,
        max_length=4096,
        description="服务端工作目录；省略保持原值，null 解除关联。",
    )
    client_directory: str | None = Field(
        default=None,
        min_length=1,
        max_length=4096,
        description="客户端目录路径；省略保持原值，null 解除关联，不访问客户端文件。",
    )
    instructions: Annotated[str, Field(max_length=32768)] | SkipJsonSchema[None] = None
    skills: (
        Annotated[list[SkillBinding | SkillHubBinding], Field(max_length=64)] | SkipJsonSchema[None]
    ) = None
    mcp_servers: Annotated[list[Reference], Field(max_length=64)] | SkipJsonSchema[None] = None
    plugins: Annotated[list[Reference], Field(max_length=64)] | SkipJsonSchema[None] = None
    subagents: Annotated[list[Reference], Field(max_length=16)] | SkipJsonSchema[None] = None
    memory_enabled: StrictBool | SkipJsonSchema[None] = None
    team_enabled: StrictBool | SkipJsonSchema[None] = None

    @model_validator(mode="after")
    def validate_changes(self) -> Self:
        changes = self.model_dump(exclude_unset=True, exclude={"expected_revision"})
        if not changes:
            raise ValueError("at least one project field is required")
        # Reuse the complete contract's null, path and duplicate-reference checks.
        ProjectFields.model_validate({"name": "Project", **changes})
        return self


class ProjectView(ProjectFields):
    project_id: str
    revision: int
    created_at: datetime
    updated_at: datetime


class SkillOption(SkillBinding):
    name: str
    description: str


class McpOption(ProjectModel):
    server_name: str
    name: str
    state: Literal["configured", "disabled", "authorization_required"]


class PluginOption(ProjectModel):
    plugin_id: str
    name: str
    description: str
    available: bool


class SubagentOption(ProjectModel):
    slug: str
    name: str
    description: str


class ProjectResources(ProjectModel):
    skills: list[SkillOption]
    mcp_servers: list[McpOption]
    plugins: list[PluginOption] = Field(default_factory=list)
    subagents: list[SubagentOption] = Field(default_factory=list)
    directory_roots: list[str] = Field(description="当前用户可浏览的服务端目录根路径。")
