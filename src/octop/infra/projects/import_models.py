"""Explicit external project and inert configuration import contracts."""

from __future__ import annotations

import base64
import binascii
from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, field_validator, model_validator

from octop.infra.projects.models import Name, ProjectFields, ProjectModel, ProjectView, Reference

ImportSource = Literal["codex", "claude"]
ConfigurationCategory = Literal["settings", "instructions", "plugins", "skills", "mcpServers"]
ExternalID = Annotated[str, StringConstraints(min_length=1, max_length=4096)]
MAX_CONFIGURATION_FILE_BYTES = 2 * 1024 * 1024
MAX_CONFIGURATION_BYTES = 20 * 1024 * 1024


class ProjectImport(ProjectModel):
    source: ImportSource
    external_project_id: ExternalID = Field(description="来源项目的稳定标识；只用于本人导入去重。")
    name: Name
    client_directory: str | None = Field(
        default=None,
        min_length=1,
        max_length=4096,
        description="来源电脑的绝对目录路径；只保存，不在 Octop 主机上解析或读取。",
    )
    project_id: Reference | None = Field(
        default=None,
        description="首次导入可明确选择本人已有项目；后续不能把同一来源重新关联到其他项目。",
    )

    @model_validator(mode="after")
    def validate_project(self) -> Self:
        ProjectFields(name=self.name, client_directory=self.client_directory)
        if any(ord(char) < 32 for char in self.external_project_id):
            raise ValueError("invalid external_project_id")
        return self


class ProjectImportResult(ProjectModel):
    project: ProjectView
    action: Literal["imported", "linked", "unchanged"]


class ConfigurationFile(ProjectModel):
    relative_path: str = Field(
        min_length=1,
        max_length=4096,
        description="条目内的相对文件名，使用 / 分隔；不接受绝对路径、点目录或反斜杠。",
    )
    content_base64: str = Field(
        max_length=4 * ((MAX_CONFIGURATION_FILE_BYTES + 2) // 3),
        description="文件原始字节的标准 Base64；单文件最多 2 MiB，不由服务端读取来源路径。",
    )

    @field_validator("relative_path")
    @classmethod
    def safe_relative_path(cls, value: str) -> str:
        if (
            any(part in {"", ".", ".."} for part in value.split("/"))
            or any(ord(char) < 32 for char in value)
            or "\\" in value
            or ":" in value
        ):
            raise ValueError("invalid relative_path")
        return value

    @field_validator("content_base64")
    @classmethod
    def valid_content(cls, value: str) -> str:
        try:
            data = base64.b64decode(value, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("invalid Base64 content") from exc
        if len(data) > MAX_CONFIGURATION_FILE_BYTES:
            raise ValueError("configuration file exceeds 2 MiB")
        return base64.b64encode(data).decode("ascii")

    def byte_count(self) -> int:
        return len(base64.b64decode(self.content_base64))


class ConfigurationImport(ProjectModel):
    source: ImportSource
    category: ConfigurationCategory = Field(description="每次只导入一个可独立选择的配置条目。")
    name: Name
    source_path: str | None = Field(
        default=None,
        max_length=4096,
        description="来源位置，仅作为不透明的来源说明保存；不读取或解析该路径。",
    )
    files: list[ConfigurationFile] = Field(
        min_length=1,
        max_length=1000,
        description="该设置、行动指南、插件、Skill 或 MCP 条目自己的文件副本；总计最多 20 MiB。",
    )

    @model_validator(mode="after")
    def bounded_files(self) -> Self:
        if len({file.relative_path for file in self.files}) != len(self.files):
            raise ValueError("duplicate configuration file path")
        if sum(file.byte_count() for file in self.files) > MAX_CONFIGURATION_BYTES:
            raise ValueError("configuration exceeds 20 MiB")
        return self


class ConfigurationView(ProjectModel):
    configuration_id: str
    source: ImportSource
    category: ConfigurationCategory
    name: str
    source_path: str | None
    status: Literal["pendingActivation"] = "pendingActivation"
    digest: str
    byte_count: int
    file_count: int
    created_at: datetime


class ConfigurationDetail(ConfigurationView):
    files: list[ConfigurationFile]


class ConfigurationImportResult(ProjectModel):
    configuration: ConfigurationView
    action: Literal["imported", "unchanged"]
