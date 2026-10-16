"""Client-executed tool contracts; no client filesystem access on the server."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Any, Literal, Self

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import Field, JsonValue, StrictBool, StringConstraints, model_validator

from octop.infra.projects.approval_models import ApprovalMode, RequestID
from octop.infra.projects.models import ProjectModel
from octop.infra.projects.review_models import ApprovalReview

ClientID = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{8,128}$")]
ToolName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,47}$")]
CallStatus = Literal["pending", "claimed", "completed", "cancelled", "expired", "interrupted"]


class ClientToolDefinition(ProjectModel):
    name: ToolName
    description: str = Field(min_length=1, max_length=4096)
    input_schema: dict[str, Any] = Field(
        description="Draft 2020-12 JSON Schema，根 type 必须为 object；仅支持文档内引用。"
    )

    @model_validator(mode="after")
    def valid_schema(self) -> Self:
        try:
            encoded = json.dumps(self.input_schema, allow_nan=False)
            if len(encoded.encode()) > 32768 or self.input_schema.get("type") != "object":
                raise ValueError("object schema required, maximum 32 KiB")
            stack: list[Any] = [self.input_schema]
            while stack:
                value = stack.pop()
                if isinstance(value, dict):
                    for key, item in value.items():
                        if key in {"$ref", "$dynamicRef"} and (
                            not isinstance(item, str) or not item.startswith("#")
                        ):
                            raise ValueError("only local schema references are supported")
                        # Nested identifiers can redirect an otherwise local reference.
                        if key == "$id":
                            raise ValueError("schema identifiers are not supported")
                        stack.append(item)
                elif isinstance(value, list):
                    stack.extend(value)
            Draft202012Validator.check_schema(self.input_schema)
        except (SchemaError, TypeError, RecursionError) as exc:
            raise ValueError("invalid client tool schema") from exc
        return self


class ClientApprovalContext(ProjectModel):
    mode: ApprovalMode
    revision: int = Field(ge=1)
    action_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    requires_user: bool = False


class ClientReviewContext(ProjectModel):
    working_directory: str = Field(
        min_length=1,
        max_length=4096,
        description="执行电脑固定的工作目录；服务端只审查文本，不探测该路径。",
    )
    workspace_authorization_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    command_authorization_id: str = Field(min_length=1, max_length=128)
    execution_profile: Literal["workspace_write", "full_access"]
    context_digest: str = Field(
        pattern=r"^[a-f0-9]{64}$",
        description="客户端原执行计划摘要，须覆盖参数、目录、授权身份、实际执行配置；审查不代替本机核对。",
    )


class ClientToolReviewRequest(ProjectModel):
    request_id: RequestID
    claim_token: str = Field(pattern=r"^[A-Za-z0-9_-]{16,256}$")
    action_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    context: ClientReviewContext


class ClientToolCall(ProjectModel):
    call_id: str
    task_id: str
    turn_id: str
    client_id: str
    name: str = Field(description="客户端声明的原始工具名；模型侧使用 client__ 前缀。")
    arguments: dict[str, JsonValue]
    approval_context: ClientApprovalContext
    dispatch_allowed: bool = Field(
        description="当前原轮次未暂停/取消且调用有效；执行电脑应在派发前用 tool.get 重新核对。"
    )
    manual_review_required: bool = Field(
        description="本机操作仍须由用户明确批准；任务模式及 Reviewer 结果均不能覆盖。"
    )
    review: ApprovalReview | None = None
    status: CallStatus
    error_code: str
    created_at: datetime
    updated_at: datetime
    expires_at: datetime = Field(description="服务端固定十分钟截止时间；重连、领取不延长。")


class ClientToolClaim(ProjectModel):
    call: ClientToolCall
    claim_token: str = Field(description="仅返回给成功领取者；执行及回传前须持久保存。")


class ClientToolResult(ProjectModel):
    claim_token: str = Field(pattern=r"^[A-Za-z0-9_-]{16,256}$")
    output: JsonValue = Field(description="客户端执行结果或可供模型理解的错误信息，最多 256 KiB。")
    is_error: StrictBool = False

    @model_validator(mode="after")
    def bounded_output(self) -> Self:
        try:
            if len(json.dumps(self.output, allow_nan=False).encode()) > 256 * 1024:
                raise ValueError("client tool output exceeds 256 KiB")
        except (TypeError, RecursionError) as exc:
            raise ValueError("invalid JSON output") from exc
        return self
