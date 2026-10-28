"""Unified multi-turn work records with an optional expert and independent project."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, Self, cast

from langchain_core.messages import messages_from_dict
from pydantic import Field, StrictBool, StrictInt, StringConstraints, model_validator
from pydantic.json_schema import SkipJsonSchema

from octop.i18n import tr
from octop.infra.agents.providers.reasoning import reasoning_request_parameters
from octop.infra.agents.providers.store import ProviderStore
from octop.infra.db.repos.project_task_turns import ProjectTaskTurnRow
from octop.infra.db.repos.project_tasks import ProjectTaskRow
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.gateway.media.attachment_hints import content_blocks_need_vision
from octop.infra.knowledge.tools import SEARCH_KNOWLEDGE_TOOL
from octop.infra.projects import (
    approval_policy,
    artifacts,
    engine_catalog,
    input_attachments,
    interactions,
)
from octop.infra.projects.approval_models import (
    ApprovalCapabilities,
    ApprovalMode,
    TaskApprovalPolicy,
)
from octop.infra.projects.artifact_models import TaskArtifact
from octop.infra.projects.client_tool_models import ClientID, ClientToolDefinition
from octop.infra.projects.content import TaskContentBlock
from octop.infra.projects.context import is_compaction
from octop.infra.projects.conversation_import_models import ImportedTurnHistory, ImportSource
from octop.infra.projects.cursors import cursor_codec, cursor_reset
from octop.infra.projects.deletion_state import assert_project_writable
from octop.infra.projects.engine_catalog import (
    EngineCatalogEntry,
    ExecutionEngineSnapshot,
)
from octop.infra.projects.engine_catalog import EngineSelection as EngineSelection
from octop.infra.projects.file_changes import TaskFileChange
from octop.infra.projects.image_tools import READ_TASK_IMAGE
from octop.infra.projects.input_attachment_models import TaskInputAttachment
from octop.infra.projects.interaction_models import TaskInteraction, public_status
from octop.infra.projects.knowledge import selection_snapshot
from octop.infra.projects.legacy_conversation_models import (
    LegacySourceAttachment,
    LegacyTrajectoryEvent,
)
from octop.infra.projects.model_routing import (
    ModelRoutingDecision,
    RoutingCapabilities,
    RoutingRequest,
    decide_model,
    routing_capabilities,
)
from octop.infra.projects.model_routing import (
    load_settings as load_routing_settings,
)
from octop.infra.projects.models import (
    ProjectFields,
    ProjectModel,
    ProjectResources,
    Reference,
    SkillBinding,
    SkillHubBinding,
)
from octop.infra.projects.progress import TaskPlan
from octop.infra.projects.recovery import TaskRecovery
from octop.infra.projects.service import _validate, get_project
from octop.infra.projects.service import resources as project_resources
from octop.infra.projects.settings import (
    TaskPresentation,
    presentation_for_turn,
    snapshot_presentation,
)
from octop.infra.projects.title_policy import provisional_title
from octop.infra.users.resource_policy import assert_token_quota_available
from octop.infra.utils.locale import normalize_locale
from octop.infra.utils.ulid import new_ulid

RequestID = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{8,128}$")]
Prompt = Annotated[str, StringConstraints(strip_whitespace=True, max_length=32768)]
ActionMode = Literal["ask", "plan", "craft"]
TurnStatus = Literal[
    "queued",
    "running",
    "waiting_approval",
    "waiting_answer",
    "waiting_client_tool",
    "paused",
    "interrupted",
    "completed",
    "failed",
    "cancelled",
]


class TaskExecutionOptions(ProjectModel):
    expert_memory_enabled: StrictBool | None = Field(
        default=None,
        description="本轮是否使用所选专家的长期记忆；省略遵循专家策略，false 禁用召回和采集。",
    )
    action_timeline_enabled: StrictBool | None = Field(
        default=None,
        description="本轮结构化动作链路开关；省略或 null 使用当前全局默认值，入队后固定，不修改计划执行或步骤面板显示。",
    )
    reasoning_mode: Literal["auto", "enabled", "disabled"] | None = Field(
        default=None,
        description="本轮思考模式；省略或 null 使用所选模型默认值。不支持的设置返回 422。",
    )
    reasoning_effort: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)] | None
    ) = Field(
        default=None,
        description="本轮思考强度，必须来自所选模型 reasoning_config.efforts；省略或 null 使用模型默认值。",
    )
    model_ref: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=512)]
        | None
    ) = Field(
        default=None,
        description="本轮优先模型引用，使用 GET /api/project-tasks/resources 的 models[].model_ref。创建、追问和重试均可传，优先于 routing；省略或 null 且未启用 routing 时使用发送时的服务端默认模型，不继承上一轮选择。未启用 routing 时，当前或历史图片需要视觉能力可临时选择兼容模型；原选项保留，实际模型以轮次 model_ref 为准。显式不可用模型返回 409 TASK_MODEL_UNAVAILABLE。",
        examples=["Box/RM-01 VLM"],
    )
    routing: RoutingRequest | None = Field(
        default=None,
        description="本轮盒子或混合模式；省略保持现有默认模型行为。显式 model_ref 优先。混合档位是难度门槛，100 也保留基础任务在盒子。",
    )
    skills: list[SkillBinding | SkillHubBinding] | None = Field(
        default=None,
        max_length=64,
        description="本轮完整 Skill 选择；省略或 null 继承项目，[] 本轮禁用。",
    )
    mcp_servers: list[Reference] | None = Field(
        default=None,
        max_length=64,
        description="本轮完整 MCP 选择；省略或 null 继承项目，[] 本轮禁用。",
    )
    knowledge_base_ids: list[Reference] | None = Field(
        default=None,
        max_length=64,
        description="本轮知识库；省略或 null 选择本人默认开启的知识库，[] 禁用。显式选择须对当前用户可见，入队后固定。",
    )
    plugins: list[Reference] | None = Field(
        default=None, max_length=64, description="本轮插件；省略或 null 继承项目，[] 禁用。"
    )
    subagents: list[Reference] | None = Field(
        default=None,
        max_length=16,
        description="本轮子代理角色；省略或 null 继承项目，[] 清空角色。",
    )
    memory_enabled: StrictBool | None = Field(
        default=None, description="本轮是否启用任务持久记忆；省略或 null 继承项目。"
    )
    team_enabled: StrictBool | None = Field(
        default=None, description="本轮是否启用任务内团队同步协作；省略或 null 继承项目。"
    )

    client_id: ClientID | None = Field(
        default=None, description="本轮客户端工具执行端标识；与 client.attach 一致。"
    )
    client_tools: list[ClientToolDefinition] | None = Field(
        default=None, max_length=64, description="本轮可调用的客户端工具；不继承前轮或项目。"
    )

    @model_validator(mode="after")
    def validate_references(self) -> Self:
        ProjectFields.model_validate(
            {
                "name": "Task",
                **self.model_dump(
                    exclude_none=True,
                    exclude={
                        "model_ref",
                        "routing",
                        "client_id",
                        "client_tools",
                        "reasoning_mode",
                        "reasoning_effort",
                        "action_timeline_enabled",
                        "knowledge_base_ids",
                        "expert_memory_enabled",
                    },
                ),
            }
        )
        if self.client_tools and not self.client_id:
            raise ValueError("client_tools require client_id")
        if self.knowledge_base_ids is not None and len(self.knowledge_base_ids) != len(
            set(self.knowledge_base_ids)
        ):
            raise ValueError("duplicate knowledge base reference")
        names = [tool.name for tool in self.client_tools or []]
        if len(names) != len(set(names)):
            raise ValueError("duplicate client tool names")
        if set(names) & (
            artifacts.SERVER_ARTIFACT_TOOLS | {READ_TASK_IMAGE, SEARCH_KNOWLEDGE_TOOL}
        ):
            raise ValueError(
                "task built-in tools are server-owned and cannot be registered by clients"
            )
        return self


class TaskReasoningCapability(ProjectModel):
    supported: bool
    toggle: bool
    default_mode: Literal["auto", "enabled", "disabled"]
    efforts: list[str]
    default_effort: str | None
    effort_type: Literal["enum", "token_budget"]
    adapter: str


class TaskModelOption(ProjectModel):
    model_ref: str = Field(description="客户端选中后原样提交到 options.model_ref 的完整模型引用。")
    name: str = Field(description="供客户端展示的模型名称；调用时使用 model_ref。")
    provider_kind: str = Field(default="openai", description="模型连接类型；box 表示盒子内部模型。")
    supports_images: bool = Field(
        description="模型是否支持图片输入；包含图片的任务须选择此类模型。"
    )
    reasoning_config: TaskReasoningCapability | None = Field(
        default=None, description="模型的思考开关、强度及默认值；null 表示未声明思考能力。"
    )


class TaskExpertOption(ProjectModel):
    expert_id: str = Field(description="持久专家的 agent_id，不是模板名或发布资源 ID。")
    name: str
    description: str | None = None
    config_revision: int = 1
    kind: Literal["expert", "team"] = "expert"
    is_shared: bool = False
    is_owner: bool = True
    member_ids: list[str] = Field(default_factory=list)
    default_skills: list[SkillBinding] = Field(
        default_factory=list, description="专家默认挂载的已安装 Skill 引用；不含文件内容。"
    )
    has_workspace_skills: bool = Field(
        default=False,
        description="专家另有不能用本轮 Skill 引用表达的工作区技能；显式自定义 Skill 会替换这些默认技能。",
    )


class TaskResources(ProjectResources):
    execution_engines: list[EngineCatalogEntry] = Field(default_factory=list)
    experts: list[TaskExpertOption] = Field(
        default_factory=list,
        description="本人可用的本地专家、本人团队及已授权共享专家；不含私人管家。",
    )
    models: list[TaskModelOption] = Field(
        description="当前可用于任务的已启用模型，包含盒子和远端；不含 ASR/TTS 模型、密钥或连接地址。"
    )
    default_model_ref: str | None = Field(
        description="当前服务端默认任务模型；null 表示无可用模型。客户端未传 options.model_ref 且未启用 routing 时，由服务端在每次发送时解析。"
    )
    approval: ApprovalCapabilities
    routing: RoutingCapabilities = Field(
        description="服务端盒子/混合路由能力及两端模型配置，不含连接凭据。"
    )


class TaskPatch(ProjectModel):
    engine: EngineSelection | SkipJsonSchema[None] = Field(
        default=None, description="空闲时选择下一轮执行引擎；必须提供配置版本，历史快照不变。"
    )
    expected_revision: Annotated[StrictInt, Field(ge=1)] = Field(
        description="详情或摘要的 list_revision；冲突返回 409。"
    )
    expected_configuration_revision: Annotated[StrictInt, Field(ge=1)] | None = None
    selected_expert_id: Reference | None = Field(
        default=None, description="省略不变；显式 null 切换为通用助手。"
    )
    action_mode: ActionMode | SkipJsonSchema[None] = None
    pinned: StrictBool | SkipJsonSchema[None] = None
    title: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
        | SkipJsonSchema[None]
    ) = None
    archived: StrictBool | SkipJsonSchema[None] = Field(
        default=None, description="省略保持原值；不可为 null。"
    )
    web_search_enabled: StrictBool | SkipJsonSchema[None] = Field(
        default=None,
        description="任务网络搜索开关；省略保持原值，不可为 null。修改仅影响之后入队的新轮次。",
    )

    @model_validator(mode="after")
    def validate_changes(self) -> Self:
        changes = self.model_dump(
            exclude_unset=True, exclude={"expected_revision", "expected_configuration_revision"}
        )
        if not changes or any(
            value is None for key, value in changes.items() if key != "selected_expert_id"
        ):
            raise ValueError("a non-null task setting is required")
        if {
            "selected_expert_id",
            "action_mode",
            "engine",
        } & changes.keys() and self.expected_configuration_revision is None:
            raise ValueError(
                "expected_configuration_revision is required for configuration changes"
            )
        return self


class TaskMessage(ProjectModel):
    expected_configuration_revision: Annotated[StrictInt, Field(ge=1)] | None = Field(
        default=None,
        description="发送时看到的任务配置版本；省略按初始版本 1 校验，防止旧队列沿用已变更的专家。",
    )
    expected_project_revision: Annotated[StrictInt, Field(ge=1)] | None = Field(
        default=None, description="发送时确认的项目版本；有值时须与当前项目一致。"
    )
    request_id: RequestID = Field(
        description="本次发送的唯一标识；网络重试必须复用，相同标识不会重复执行。"
    )
    prompt: Prompt = Field(
        default="",
        description="本轮用户文字；可为空但须提供附件。无附件的 /compact 为上下文压缩命令，不执行任务工具。",
    )
    attachment_ids: list[Reference] = Field(
        default_factory=list,
        max_length=input_attachments.MAX_ATTACHMENTS,
        description="已上传的本人附件 ID，按显示顺序排列，图片和其他文件合计最多九个；首次发送后归属此任务。",
    )
    options: TaskExecutionOptions = Field(
        default_factory=TaskExecutionOptions,
        description="本轮执行选项；省略时使用当前项目及默认模型，不修改项目。",
    )

    @model_validator(mode="after")
    def validate_message(self) -> Self:
        if not self.prompt and not self.attachment_ids:
            raise ValueError("text or attachments are required")
        if len(self.attachment_ids) != len(set(self.attachment_ids)):
            raise ValueError("duplicate attachment IDs")
        return self


class TaskRetry(ProjectModel):
    expected_configuration_revision: Annotated[StrictInt, Field(ge=1)] | None = None
    expected_project_revision: Annotated[StrictInt, Field(ge=1)] | None = None
    request_id: RequestID = Field(description="本次重试的唯一标识；网络重传必须复用。")
    options: TaskExecutionOptions = Field(
        default_factory=TaskExecutionOptions,
        description="本轮执行选项；省略时使用当前项目及默认模型，不修改项目。",
    )


class TaskCancel(ProjectModel):
    turn_id: str = Field(
        min_length=1, max_length=256, description="用户要停止的轮次；不会取消其他轮次。"
    )


class ProjectTaskCreate(TaskMessage):
    engine: EngineSelection = Field(
        default_factory=EngineSelection,
        description="任务执行引擎，默认Octop；ACP连接须属于本人且已启用并检查就绪。",
    )
    selected_expert_id: Reference | None = Field(
        default=None,
        description="可选本人专家、本人团队或已授权共享专家的 agent_id；null 使用通用助手。",
    )
    action_mode: ActionMode = Field(
        default="craft", description="ask 问答、plan 规划、craft 执行；服务端约束工具能力。"
    )
    web_search_enabled: StrictBool = Field(
        default=True, description="任务网络搜索开关；默认开启，持久保存，追问和重试沿用任务当前值。"
    )
    approval_mode: ApprovalMode = Field(
        default="ask_for_approval",
        description="旧客户端输入字段，已忽略；新任务继承当前账号全局审批模式。",
        deprecated=True,
    )


class TaskCreate(ProjectTaskCreate):
    project_id: str | None = Field(default=None, description="所属独立项目；null 创建无项目任务。")


class TaskTurn(ProjectModel):
    presentation: TaskPresentation = Field(
        description="本轮入队时冻结的展示配置，供网页和 APP 按轮展示使用；旧记录采用固定兼容值，不读取当前全局设置。"
    )
    execution_engine: ExecutionEngineSnapshot = Field(
        default_factory=ExecutionEngineSnapshot,
        description="本轮受理时冻结的引擎、连接、原生模型及适配能力；不含凭据和私有会话标识。",
    )
    history_only: bool = False
    source_task_id: str | None = None
    source_turn_id: str | None = None
    source_thread_id: str | None = None
    source_agent_id: str | None = None
    legacy_attachments: list[LegacySourceAttachment] = Field(default_factory=list)
    source_trajectory: list[LegacyTrajectoryEvent] = Field(default_factory=list)
    source_status: str | None = None
    server_directory: str | None = None
    client_directory: str | None = None
    selected_expert_id: str | None = None
    expert_name: str | None = None
    expert_config_revision: int | None = None
    action_mode: ActionMode = "craft"
    imported_history: ImportedTurnHistory | None = Field(
        default=None,
        description="外部来源的可见历史副本；不构成 Octop 工具执行、授权或文件修改回执。",
    )
    sequence: int = Field(description="轮次稳定排序序号，可能不连续；资源标识仍使用 turn_id。")
    turn_id: str
    request_id: str
    prompt: str
    attachments: list[TaskInputAttachment] = Field(
        default_factory=list,
        description="本轮用户上传的附件，仅含元数据；图片预览和文件下载通过鉴权 HTTP 获取。",
    )
    reply: str
    reasoning: str = Field(
        description="模型实际返回的可展示思考文本；与正式回复分开保存。无内容时为空字符串。"
    )
    content_blocks: list[TaskContentBlock] = Field(
        description="按发生顺序排列的完整内容块快照；历史未记录分段时为空，不推测原始顺序。"
    )
    file_changes: list[TaskFileChange] = Field(
        default_factory=list,
        description="从本轮已保存的成功工具内容块派生的文件差异；同路径多次修改分别保留，旧记录为空。",
    )
    content_revision: int = Field(description="本轮内容块版本；只有内容块或块状态变化才递增。")
    plan: TaskPlan | None = Field(
        description="本轮最新的执行计划；未创建时为 null，独立于项目长期目标。"
    )
    plan_revision: int = Field(description="本轮计划版本；未创建时为零，只在计划条目变化时递增。")
    interactions: list[TaskInteraction] = Field(
        description="本轮审批和提问完整快照，包含已处理项。"
    )
    interaction_revision: int = Field(
        description="交互、暂停意图及续接能力版本；独立于内容版本，单调递增。"
    )
    pause_requested: bool = Field(
        description="用户暂停意图；只有 status=paused 表示已到达安全边界。"
    )
    status: TurnStatus
    error_code: str
    recovery: TaskRecovery | None = Field(
        default=None,
        description="本轮中断或失败的脱敏诊断与原轮续接能力；不能据此重放结果未知的操作。",
    )
    activity: str = Field(description="当前执行的工具名称；不包含参数、凭据或工具输出。")
    artifacts: list[TaskArtifact] = Field(
        default_factory=list,
        description="该轮已成功发布的不可变附件；仅含元数据，内容使用鉴权 HTTP 下载。",
    )
    model_ref: str
    routing: ModelRoutingDecision | None = Field(
        default=None, description="本轮已冻结的模型分配结果；旧默认或直接选择模型的轮次可为 null。"
    )
    web_search_enabled: bool = Field(
        description="本轮入队时采用的任务网络搜索开关快照；之后修改任务不会改变此值。"
    )
    approval_policy_at_start: TaskApprovalPolicy = Field(
        description="本轮入队时的任务审批模式快照；之后切换模式不会改写该快照。"
    )
    options: TaskExecutionOptions = Field(
        description="本轮实际采用的模型、Skill、MCP、插件、子代理、记忆/团队开关和客户端工具。"
    )
    cancel_requested: bool = Field(description="该轮已请求停止；运行中需等待 cancelled 终态。")
    retry_of_turn_id: str | None = Field(description="手动重试的原轮次；普通消息为 null。")
    input_tokens: int = Field(description="本轮模型输入 Token 数，独立于专家用量记录。")
    output_tokens: int = Field(description="本轮模型输出 Token 数。")
    project_revision: int | None = Field(description="本轮开始时采用的项目配置版本。")
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = Field(
        default=None,
        description="本轮首次实际开始执行的服务端时间；首次排队不计入，历史未知时为 null。",
    )
    finished_at: datetime | None = Field(
        default=None,
        description="本轮到达完成、失败或取消终态的服务端时间；尚未终止或历史未知时为 null。",
    )
    processing_duration_ms: int | None = Field(
        default=None,
        ge=0,
        description="首次实际开始到终态的墙钟毫秒数，包含审批、暂停及恢复等待；任一边界未知时为 null。",
    )


class TaskSummary(ProjectModel):
    engine: EngineSelection = Field(
        default_factory=EngineSelection, description="任务下一轮采用的持久引擎选择。"
    )
    selected_expert_id: str | None = None
    configuration_revision: int = 1
    action_mode: ActionMode = "craft"
    pinned: bool = False
    import_source: ImportSource | None = Field(
        default=None, description="任务导入来源；原生任务为 null。"
    )
    external_session_id: str | None = Field(default=None, description="来源会话 ID，仅本人可见。")
    task_id: str
    project_id: str | None
    title: str
    archived: bool
    web_search_enabled: bool = Field(description="任务持久保存的网络搜索开关。")
    status: TurnStatus = Field(
        description="任务当前或最近一轮的执行状态；追问重新进入 queued，开始执行变为 running。列表直接返回，无需读取正文。"
    )
    active_turn_id: str | None
    list_revision: int = Field(description="任务摘要版本；仅列表字段变化时递增。")
    approval_policy: TaskApprovalPolicy
    created_at: datetime
    updated_at: datetime


class TaskConfigurationEvent(ProjectModel):
    event_id: str
    previous_expert_id: str | None
    selected_expert_id: str | None
    selected_expert_name: str | None = None
    previous_action_mode: ActionMode
    action_mode: ActionMode
    configuration_revision: int
    created_at: datetime


class TaskDetail(TaskSummary):
    configuration_events: list[TaskConfigurationEvent] = Field(default_factory=list)
    turns: list[TaskTurn]
    next_page_cursor: str | None = Field(
        default=None, description="读取更早轮次的游标；每页按时间正序。"
    )
    event_cursor: str | None = Field(
        default=None, description="首页对应的实时状态续接位置；历史页不推进。"
    )


class TaskUpdate(ProjectModel):
    task: TaskSummary
    turn: TaskTurn
    cursor: str


def now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


def _datetime(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1000, UTC)


def summary(row: ProjectTaskRow, policy: TaskApprovalPolicy) -> TaskSummary:
    return TaskSummary(
        engine=engine_catalog.read_engine(row.execution_json),
        selected_expert_id=row.selected_expert_id,
        configuration_revision=row.configuration_revision,
        action_mode=cast(ActionMode, row.action_mode),
        pinned=row.pinned,
        import_source=cast(ImportSource, row.import_source) if row.import_source else None,
        external_session_id=row.external_session_id or None,
        task_id=row.task_id,
        project_id=row.project_id,
        title=row.title,
        archived=row.archived,
        web_search_enabled=row.web_search_enabled,
        status=cast(TurnStatus, public_status(row.status, row.wait_kind)),
        active_turn_id=row.active_turn_id,
        list_revision=row.list_revision,
        approval_policy=policy,
        created_at=_datetime(row.created_at),
        updated_at=_datetime(row.updated_at),
    )


def turn_view(t: ProjectTaskTurnRow, attachments: list[TaskArtifact] | None = None) -> TaskTurn:
    configuration = json.loads(t.configuration_json)
    expert = configuration.get("expert_profile") or {}
    origin = configuration.get("branch_origin") or {}
    blocks = [TaskContentBlock.model_validate(block) for block in json.loads(t.content_blocks_json)]
    plan = next((block.plan for block in reversed(blocks) if block.type == "plan"), None)
    if t.status not in {"queued", "running"} or t.wait_kind == "interrupted":
        for block in blocks:
            if block.status == "streaming":
                block.status = "interrupted"
    return TaskTurn(
        presentation=presentation_for_turn(configuration),
        execution_engine=ExecutionEngineSnapshot.model_validate(
            configuration.get("execution_engine") or {}
        ),
        history_only=origin.get("history_only", False),
        source_task_id=origin.get("source_task_id"),
        source_turn_id=origin.get("source_turn_id"),
        source_thread_id=origin.get("source_thread_id"),
        source_agent_id=origin.get("source_agent_id"),
        legacy_attachments=configuration.get("legacy_attachments", []),
        source_trajectory=(configuration.get("migrated_history") or {}).get("trajectory", []),
        source_status=origin.get("source_status"),
        server_directory=configuration.get("directory"),
        client_directory=configuration.get("client_directory"),
        selected_expert_id=expert.get("expert_id"),
        expert_name=expert.get("name"),
        expert_config_revision=expert.get("config_revision"),
        action_mode=configuration.get("action_mode", "craft"),
        imported_history=configuration.get("imported_history"),
        sequence=t.pk,
        turn_id=t.turn_id,
        request_id=t.request_id,
        prompt=t.prompt,
        attachments=configuration.get("attachments", []),
        reply=t.reply,
        reasoning=t.reasoning,
        content_blocks=blocks,
        file_changes=[
            change
            for block in blocks
            if block.type == "tool" and block.status == "completed"
            for change in block.file_changes
        ],
        content_revision=t.content_revision,
        plan=plan,
        plan_revision=plan.revision if plan else 0,
        interactions=interactions.load(t),
        interaction_revision=t.interaction_revision,
        pause_requested=t.pause_requested,
        status=cast(
            TurnStatus,
            public_status(
                t.status,
                ("user" if t.pause_requested and t.wait_kind != "interrupted" else t.wait_kind)
                if t.is_waiting
                else "",
            ),
        ),
        error_code=t.error_code,
        recovery=TaskRecovery.model_validate(
            {key: value for key, value in json.loads(t.recovery_json).items() if key != "mode"}
        )
        if t.recovery_json and (t.wait_kind == "interrupted" or t.status == "failed")
        else None,
        activity=t.activity,
        artifacts=attachments or [],
        model_ref=t.model_ref,
        routing=ModelRoutingDecision.model_validate(configuration["routing"])
        if configuration.get("routing")
        else None,
        web_search_enabled=configuration.get("web_search_enabled", True),
        approval_policy_at_start=TaskApprovalPolicy.model_validate(
            configuration.get("approval_policy_at_start")
            or {"mode": "ask_for_approval", "revision": 1, "updated_at": _datetime(t.created_at)}
        ),
        options=TaskExecutionOptions(
            expert_memory_enabled=configuration.get("expert_memory_enabled"),
            action_timeline_enabled=configuration.get("action_timeline_enabled", True),
            model_ref=t.model_ref or None,
            routing=configuration.get("execution_options", {}).get("routing"),
            reasoning_mode=configuration.get("reasoning_mode"),
            reasoning_effort=configuration.get("reasoning_effort"),
            skills=configuration.get("skills", []),
            mcp_servers=configuration.get("mcp_servers", []),
            knowledge_base_ids=configuration.get("knowledge_base_ids", []),
            plugins=configuration.get("plugins", []),
            subagents=configuration.get("subagents", []),
            memory_enabled=configuration.get("memory_enabled", True),
            team_enabled=configuration.get("team_enabled", False),
            client_id=configuration.get("execution_options", {}).get("client_id"),
            client_tools=configuration.get("execution_options", {}).get("client_tools"),
        ),
        cancel_requested=t.cancel_requested,
        retry_of_turn_id=configuration.get("retry_of_turn_id"),
        input_tokens=t.input_tokens,
        output_tokens=t.output_tokens,
        project_revision=configuration.get("revision"),
        created_at=_datetime(t.created_at),
        updated_at=_datetime(t.updated_at),
        started_at=_datetime(t.started_at) if t.started_at is not None else None,
        finished_at=_datetime(t.finished_at) if t.finished_at is not None else None,
        processing_duration_ms=t.finished_at - t.started_at
        if t.started_at is not None and t.finished_at is not None and t.finished_at >= t.started_at
        else None,
    )


def _fingerprint(turn: TaskTurn, list_revision: int) -> str:
    # Lease heartbeats alone do not change the public conversation state.
    return hashlib.sha256(
        (str(list_revision) + ":" + turn.model_dump_json(exclude={"updated_at"})).encode()
    ).hexdigest()


def detail(
    services: SharedServices,
    owner_id: int,
    task_id: str,
    *,
    limit: int = 50,
    page_cursor: str | None = None,
) -> TaskDetail:
    codec = cursor_codec(services, owner_id, f"conversation:{task_id}:history")
    before = codec.decode(page_cursor, "conversation").position if page_cursor else None
    # Summary, page and stream baseline must describe the same database snapshot.
    with services.db.read_snapshot() as conn:
        row = services.project_task_repo.get_in_transaction(conn, task_id, owner_id)
        if row is None:
            raise OctopError.localized(ErrorCode.NOT_FOUND)
        policy = approval_policy.in_transaction(conn, services, owner_id)
        turns = services.project_task_turn_repo.page(conn, task_id, limit + 1, before)
        attachments = artifacts.for_turns(services, conn, task_id, [turn.turn_id for turn in turns])
        configuration_events = services.project_task_repo.configuration_events(conn, task_id)
    more = len(turns) > limit
    turns = turns[-limit:]
    views = [turn_view(t, attachments.get(t.turn_id)) for t in turns]
    stream = cursor_codec(services, owner_id, f"conversation:{task_id}:events")
    return TaskDetail(
        **summary(row, policy).model_dump(),
        configuration_events=[
            TaskConfigurationEvent(
                **{
                    **asdict(event),
                    "event_id": f"{task_id}:configuration:{event.configuration_revision}",
                    "created_at": _datetime(event.created_at),
                }
            )
            for event in configuration_events
        ],
        turns=views,
        next_page_cursor=(codec.encode("conversation", position=turns[0].pk) if more else None),
        event_cursor=(
            stream.encode(
                "conversation",
                position=turns[-1].pk if turns else 0,
                after=_fingerprint(views[-1], row.list_revision) if views else "",
            )
            if page_cursor is None
            else None
        ),
    )


def updates(
    services: SharedServices, owner_id: int, task_id: str, event_cursor: str
) -> list[TaskUpdate]:
    codec = cursor_codec(services, owner_id, f"conversation:{task_id}:events")
    cursor = codec.decode(event_cursor, "conversation")
    with services.db.read_snapshot() as conn:
        task = services.project_task_repo.get_in_transaction(conn, task_id, owner_id)
        if task is None:
            raise OctopError.localized(ErrorCode.NOT_FOUND)
        policy = approval_policy.in_transaction(conn, services, owner_id)
        rows = services.project_task_turn_repo.since(conn, task_id, cursor.position, 50)
        attachments = artifacts.for_turns(services, conn, task_id, [turn.turn_id for turn in rows])
    if cursor.position and (not rows or rows[0].pk != cursor.position):
        raise cursor_reset()
    result = []
    for row in rows:
        view = turn_view(row, attachments.get(row.turn_id))
        fingerprint = _fingerprint(view, task.list_revision)
        if row.pk == cursor.position and fingerprint == cursor.after:
            continue
        result.append(
            TaskUpdate(
                task=summary(task, policy),
                turn=view,
                cursor=codec.encode("conversation", position=row.pk, after=fingerprint),
            )
        )
    return result


def _reasoning_configuration(
    providers: ProviderStore, ref: str, options: TaskExecutionOptions
) -> dict[str, object]:
    capability = providers.get_model_reasoning_capability(ref)
    if capability is None:
        if options.reasoning_mode not in {None, "auto"} or options.reasoning_effort is not None:
            raise OctopError.localized(ErrorCode.TASK_REASONING_INVALID, status=422)
        return {"reasoning_mode": None, "reasoning_effort": None, "reasoning_overrides": {}}
    mode = options.reasoning_mode or capability["default_mode"]
    effort = options.reasoning_effort or capability["default_effort"]
    if (
        mode == "disabled" and (not capability["toggle"] or options.reasoning_effort is not None)
    ) or (
        options.reasoning_effort is not None
        and options.reasoning_effort not in capability["efforts"]
    ):
        raise OctopError.localized(ErrorCode.TASK_REASONING_INVALID, status=422)
    if mode == "disabled":
        effort = None
    if capability["adapter"] == "status_only":
        mode = "enabled"
    return {
        "reasoning_mode": mode,
        "reasoning_effort": effort,
        "reasoning_overrides": reasoning_request_parameters(capability, mode=mode, effort=effort),
    }


def _base_configuration(
    services: SharedServices,
    owner_id: int,
    project_id: str | None,
    options: TaskExecutionOptions,
    selected_expert_id: str | None = None,
    action_mode: ActionMode = "craft",
    configuration_revision: int = 1,
) -> dict[str, Any]:
    from octop.infra.projects.expert_profiles import (
        resolve_expert_profile,
        validate_expert_workspace_skills,
    )

    project = get_project(services, owner_id, project_id) if project_id else None
    if project and project.archived:
        raise OctopError.localized(ErrorCode.PROJECT_ARCHIVED)
    fields = project or ProjectFields(name="Task")
    snapshot = fields.model_dump(mode="json")
    expert = resolve_expert_profile(services, owner_id, selected_expert_id)
    capabilities = expert.get("capabilities", {}) if expert else {}
    defaults = fields.model_dump(include=set(ProjectFields.model_fields), mode="json")
    project_defaults = dict(defaults)
    for key in ("skills", "mcp_servers", "plugins", "subagents"):
        merged = [*capabilities.get(key, []), *defaults[key]]
        defaults[key] = list({json.dumps(item, sort_keys=True): item for item in merged}.values())
    overrides = options.model_dump(
        exclude_none=True,
        exclude={
            "model_ref",
            "routing",
            "client_id",
            "client_tools",
            "reasoning_mode",
            "reasoning_effort",
            "action_timeline_enabled",
            "knowledge_base_ids",
            "expert_memory_enabled",
        },
    )
    is_expert_team = bool(expert and expert.get("kind") == "team" and expert.get("team"))
    fields = ProjectFields.model_validate(
        {**defaults, **overrides, **({"team_enabled": True} if is_expert_team else {})}
    )
    if len({skill.slug for skill in fields.skills}) != len(fields.skills):
        raise OctopError.localized(ErrorCode.PROJECT_RESOURCE_INVALID, status=422)
    validate_expert_workspace_skills(
        expert, fields.skills, inherit_workspace=options.skills is None
    )
    _validate(services, owner_id, fields, allow_expert_team=is_expert_team)
    assert_token_quota_available(services.user_policy_repo, services.usage_repo, owner_id)
    presentation = snapshot_presentation(services, options.action_timeline_enabled)
    return {
        **snapshot,
        **fields.model_dump(),
        **selection_snapshot(
            services,
            owner_id,
            options.knowledge_base_ids
            if options.knowledge_base_ids is not None
            else capabilities.get("knowledge_base_ids") or None,
        ),
        "expert_profile": expert,
        "project_defaults": project_defaults,
        "expert_memory_enabled": options.expert_memory_enabled,
        "action_mode": action_mode,
        "configuration_revision": configuration_revision,
        "action_timeline_enabled": presentation.action_timeline_enabled,
        "presentation": presentation.model_dump(),
        "execution_options": options.model_dump(exclude_none=True),
    }


def _configuration(
    services: SharedServices,
    owner_id: int,
    project_id: str | None,
    options: TaskExecutionOptions,
    routing_decision: ModelRoutingDecision | None = None,
    selected_expert_id: str | None = None,
    action_mode: ActionMode = "craft",
    configuration_revision: int = 1,
    engine: EngineSelection | None = None,
    requires_images: bool = False,
    base_configuration: dict[str, Any] | None = None,
) -> tuple[str, str]:
    engine = engine or EngineSelection()
    entry = engine_catalog.validate_engine(services, owner_id, engine, action_mode=action_mode)
    snapshot = (
        dict(base_configuration)
        if base_configuration is not None
        else _base_configuration(
            services,
            owner_id,
            project_id,
            options,
            selected_expert_id,
            action_mode,
            configuration_revision,
        )
    )
    snapshot["execution_engine"] = engine_catalog.snapshot_engine(entry)
    if engine.engine_id == "acp":
        engine_catalog.validate_configuration(snapshot)
        if options.routing is not None or options.model_ref:
            raise OctopError.localized(
                ErrorCode.TASK_SETTINGS_INVALID,
                status=409,
                setting="model_ref",
                details={"setting": "model_ref", "reason": "native_engine_model"},
            )
        return json.dumps(snapshot), ""
    if options.routing is not None and options.model_ref and routing_decision is None:
        routing_decision = decide_model(
            ProviderStore(services.provider_repo),
            load_routing_settings(services),
            mode=options.routing.mode,
            level=options.routing.level,
            explicit_model_ref=options.model_ref,
            requires_images=requires_images,
        )
    if options.routing is not None and routing_decision is None and not options.model_ref:
        # Hybrid admission must classify outside the database transaction.
        raise OctopError.localized(
            ErrorCode.TASK_SETTINGS_INVALID, setting="routing", details={"setting": "routing"}
        )
    expert = snapshot.get("expert_profile") or {}
    ref = options.model_ref or (
        routing_decision.model_ref
        if routing_decision is not None
        else expert.get("default_model") or resolve_model_ref(services)
    )
    providers = ProviderStore(services.provider_repo)
    if not ref or not providers.is_model_ref_usable(ref):
        raise OctopError.localized(ErrorCode.TASK_MODEL_UNAVAILABLE, status=409)
    if requires_images and options.routing is None:
        selected_ref = ref
        ref = providers.resolve_model_for_multimodal_turn(ref, needs_multimodal=True)
        if not ref or not providers.is_model_ref_multimodal(ref):
            raise OctopError.localized(ErrorCode.TASK_IMAGE_MODEL_REQUIRED)
        if ref != selected_ref:
            snapshot["capability_model_route"] = True
    return json.dumps(
        {
            **snapshot,
            **_reasoning_configuration(providers, ref, options),
            **({"routing": routing_decision.model_dump()} if routing_decision else {}),
        }
    ), ref


def resolve_model_ref(services: SharedServices) -> str | None:
    """Resolve the current model for task admission."""
    providers = ProviderStore(services.provider_repo)
    provider, model = services.settings_repo.get_active_model()
    ref = f"{provider}/{model}" if provider and model else None
    if not ref or not providers.is_model_ref_usable(ref):
        configured = services.config.agentcomputer_text_model_ref
        ref = (
            configured
            if configured and providers.is_model_ref_usable(configured)
            else providers.resolve_first_model_ref()
        )
    return ref


def _requires_images(
    services: SharedServices,
    conn: Any,
    owner_id: int,
    task_id: str | None,
    body: TaskMessage,
) -> bool:
    if is_compaction(body.prompt, body.attachment_ids):
        return False
    attached = [
        input_attachments._available(
            services.project_task_attachment_repo.get(conn, owner_id, attachment_id)
        )
        for attachment_id in body.attachment_ids
    ]
    if any(row.task_id not in {None, task_id} for row in attached):
        raise OctopError.localized(ErrorCode.TASK_ATTACHMENT_UNAVAILABLE)
    existing = (
        services.project_task_attachment_repo.for_task(conn, owner_id, task_id) if task_id else []
    )
    if any(input_attachments.is_image(row.media_type) for row in [*attached, *existing]):
        return True
    context = services.project_task_turn_repo.context(task_id) if task_id else None
    return bool(
        context
        and any(
            content_blocks_need_vision(message.content)
            for message in messages_from_dict(json.loads(context[1]))
        )
    )


def _same_prompt(old: ProjectTaskTurnRow, body: TaskMessage, retry_of_turn_id: str | None) -> None:
    saved = json.loads(old.configuration_json)
    if (
        old.prompt != body.prompt
        or json.loads(old.configuration_json).get("attachment_ids", []) != body.attachment_ids
        or json.loads(old.configuration_json).get("retry_of_turn_id") != retry_of_turn_id
        or json.loads(old.configuration_json).get("execution_options", {})
        != body.options.model_dump(exclude_none=True)
        or saved.get("request_configuration_revision") != body.expected_configuration_revision
        or saved.get("request_project_revision") != body.expected_project_revision
        or (
            isinstance(body, ProjectTaskCreate)
            and (
                saved.get("web_search_enabled", True) != body.web_search_enabled
                or (saved.get("expert_profile") or {}).get("expert_id") != body.selected_expert_id
                or saved.get("action_mode", "craft") != body.action_mode
                or EngineSelection.model_validate(
                    {
                        key: (saved.get("execution_engine") or {}).get(key)
                        for key in ("engine_id", "connection_id", "model_id")
                        if key in (saved.get("execution_engine") or {})
                    }
                )
                != body.engine
            )
        )
    ):
        raise OctopError.localized(ErrorCode.TASK_REQUEST_CONFLICT, status=409)


def _input_configuration(
    services: SharedServices,
    conn: Any,
    owner_id: int,
    task_id: str,
    body: TaskMessage,
    configuration: str,
    model_ref: str,
) -> str:
    selected_engine = json.loads(configuration).get("execution_engine") or {}
    if selected_engine.get("engine_id") == "acp" and is_compaction(
        body.prompt, body.attachment_ids
    ):
        raise OctopError.localized(
            ErrorCode.TASK_SETTINGS_INVALID,
            status=409,
            setting="engine",
            details={
                "setting": "engine",
                "reason": "unsupported_capability",
                "capability": "compact",
            },
        )
    attached = input_attachments.bind(services, conn, owner_id, task_id, body.attachment_ids)
    has_images = any(
        input_attachments.is_image(row.media_type)
        for row in services.project_task_attachment_repo.for_task(conn, owner_id, task_id)
    )
    if (
        has_images
        and selected_engine.get("engine_id") == "acp"
        and not selected_engine.get("capabilities", {}).get("images", False)
    ):
        raise OctopError.localized(
            ErrorCode.TASK_SETTINGS_INVALID,
            status=409,
            setting="engine",
            details={
                "setting": "engine",
                "reason": "unsupported_capability",
                "capability": "images",
            },
        )
    if (
        has_images
        and not is_compaction(body.prompt, body.attachment_ids)
        and json.loads(configuration).get("execution_engine", {}).get("engine_id", "octop")
        == "octop"
        and not ProviderStore(services.provider_repo).is_model_ref_multimodal(model_ref)
    ):
        raise OctopError.localized(ErrorCode.TASK_IMAGE_MODEL_REQUIRED)
    return json.dumps(
        {
            **json.loads(configuration),
            "request_configuration_revision": body.expected_configuration_revision,
            "request_project_revision": body.expected_project_revision,
            "attachment_ids": body.attachment_ids,
            "attachments": [item.model_dump(mode="json") for item in attached],
        }
    )


def _submission_state(
    services: SharedServices,
    conn: Any,
    owner_id: int,
    body: TaskMessage | TaskRetry,
    *,
    task_id: str | None = None,
    project_id: str | None = None,
    retry_of_turn_id: str | None = None,
) -> tuple[ProjectTaskRow | None, TaskMessage, ProjectTaskTurnRow | None]:
    """Validate ownership, replay identity and admission state before any model call."""
    tasks, turns = services.project_task_repo, services.project_task_turn_repo
    if not tasks.lock_owner(conn, owner_id):
        raise OctopError.localized(ErrorCode.FORBIDDEN)
    task = (
        tasks.get_in_transaction(conn, task_id, owner_id)
        if task_id
        else tasks.by_request(conn, owner_id, body.request_id)
    )
    if task_id and task is None:
        raise OctopError.localized(ErrorCode.NOT_FOUND)
    if retry_of_turn_id is not None:
        original = turns.get_in_transaction(conn, retry_of_turn_id)
        if original is None or original.task_id != task_id:
            raise OctopError.localized(ErrorCode.NOT_FOUND)
        if json.loads(original.configuration_json).get("branch_origin", {}).get("history_only"):
            raise OctopError.localized(ErrorCode.TASK_RETRY_UNAVAILABLE)
        body = TaskMessage(
            request_id=body.request_id,
            expected_configuration_revision=body.expected_configuration_revision,
            expected_project_revision=body.expected_project_revision,
            prompt=original.prompt,
            attachment_ids=json.loads(original.configuration_json).get("attachment_ids", []),
            options=body.options,
        )
    assert isinstance(body, TaskMessage)
    if task is None:
        assert_project_writable(services, conn, owner_id, project_id)
        return None, body, None
    if not task_id and task.project_id != project_id:
        raise OctopError.localized(ErrorCode.TASK_REQUEST_CONFLICT, status=409)
    previous = turns.by_request(conn, task.task_id, body.request_id)
    if previous:
        _same_prompt(previous, body, retry_of_turn_id)
        return task, body, previous
    assert_project_writable(services, conn, owner_id, task.project_id)
    if (body.expected_configuration_revision or 1) != task.configuration_revision:
        raise OctopError.localized(ErrorCode.TASK_REVISION_CONFLICT)
    if body.expected_project_revision is not None:
        project = get_project(services, owner_id, task.project_id) if task.project_id else None
        if project is None or project.revision != body.expected_project_revision:
            raise OctopError.localized(ErrorCode.PROJECT_REVISION_CONFLICT)
    if task.archived:
        raise OctopError.localized(ErrorCode.TASK_ARCHIVED)
    if task.active_turn_id is not None:
        raise OctopError.localized(ErrorCode.TASK_BUSY, status=409)
    if retry_of_turn_id is not None:
        latest = turns.page(conn, task.task_id, 1)
        if (
            not latest
            or latest[0].turn_id != retry_of_turn_id
            or latest[0].status not in {"failed", "cancelled"}
        ):
            raise OctopError.localized(ErrorCode.TASK_RETRY_UNAVAILABLE)
    return task, body, None


@dataclass(frozen=True)
class PreparedSubmission:
    task: ProjectTaskRow | None
    message: TaskMessage
    configuration: dict[str, Any]
    requires_images: bool
    previous_turn_id: str | None = None


def prepare_submission(
    services: SharedServices,
    owner_id: int,
    body: TaskMessage | TaskRetry,
    *,
    task_id: str | None = None,
    project_id: str | None = None,
    retry_of_turn_id: str | None = None,
    before: Callable[[Any], None] | None = None,
) -> PreparedSubmission:
    """Read admission state briefly, then freeze content without a SQL transaction."""
    with services.db.transaction() as conn:
        if before is not None:
            before(conn)
        task, message, previous = _submission_state(
            services,
            conn,
            owner_id,
            body,
            task_id=task_id,
            project_id=project_id,
            retry_of_turn_id=retry_of_turn_id,
        )
        if previous is not None:
            return PreparedSubmission(task, message, {}, False, previous.turn_id)
        requires_images = _requires_images(
            services, conn, owner_id, task.task_id if task else None, message
        )
    configuration = _base_configuration(
        services,
        owner_id,
        task.project_id if task else project_id,
        message.options,
        selected_expert_id=task.selected_expert_id
        if task
        else (message.selected_expert_id if isinstance(message, ProjectTaskCreate) else None),
        action_mode=cast(ActionMode, task.action_mode)
        if task
        else (message.action_mode if isinstance(message, ProjectTaskCreate) else "craft"),
        configuration_revision=task.configuration_revision if task else 1,
    )
    return PreparedSubmission(task, message, configuration, requires_images)


def submission_configuration(
    services: SharedServices,
    owner_id: int,
    prepared: PreparedSubmission,
    routing_decision: ModelRoutingDecision | None = None,
) -> tuple[str, str]:
    task, message = prepared.task, prepared.message
    engine = (
        engine_catalog.read_engine(task.execution_json)
        if task
        else (message.engine if isinstance(message, ProjectTaskCreate) else EngineSelection())
    )
    return _configuration(
        services,
        owner_id,
        task.project_id if task else None,
        message.options,
        routing_decision,
        action_mode=cast(ActionMode, prepared.configuration["action_mode"]),
        engine=engine,
        requires_images=prepared.requires_images,
        base_configuration=prepared.configuration,
    )


def validate_preparation(
    services: SharedServices,
    conn: Any,
    owner_id: int,
    prepared: PreparedSubmission,
    task: ProjectTaskRow | None,
) -> None:
    """Recheck SQL versions and access without reopening any content backend."""
    if (task is None) != (prepared.task is None) or (
        task is not None
        and prepared.task is not None
        and (task.task_id, task.list_revision)
        != (prepared.task.task_id, prepared.task.list_revision)
    ):
        raise OctopError.localized(ErrorCode.TASK_REVISION_CONFLICT)
    project_id = prepared.configuration.get("project_id")
    if project_id:
        project = services.project_repo.get_in_transaction(conn, project_id, owner_id)
        if (
            project is None
            or project.archived
            or project.revision != prepared.configuration["revision"]
        ):
            raise OctopError.localized(ErrorCode.PROJECT_REVISION_CONFLICT)
    profile = prepared.configuration.get("expert_profile")
    if profile:
        from octop.infra.projects.expert_profiles import assert_profile_access, expert_configuration

        assert_profile_access(services, owner_id, profile)
        for selected in [profile, *((profile.get("team") or {}).get("members") or [])]:
            row, _ = expert_configuration(services, owner_id, selected["expert_id"])
            if row.config_revision != selected["config_revision"]:
                raise OctopError.localized(ErrorCode.TASK_REVISION_CONFLICT)


def send(
    services: SharedServices,
    owner_id: int,
    body: TaskMessage | TaskRetry,
    *,
    task_id: str | None = None,
    project_id: str | None = None,
    retry_of_turn_id: str | None = None,
) -> TaskDetail:
    prepared = prepare_submission(
        services,
        owner_id,
        body,
        task_id=task_id,
        project_id=project_id,
        retry_of_turn_id=retry_of_turn_id,
    )
    configuration = (
        submission_configuration(services, owner_id, prepared)
        if prepared.previous_turn_id is None
        else None
    )
    with services.db.transaction() as conn:
        task_id, _ = send_in_transaction(
            services,
            conn,
            owner_id,
            body,
            task_id=task_id,
            project_id=project_id,
            retry_of_turn_id=retry_of_turn_id,
            prepared=prepared,
            prepared_configuration=configuration,
        )
    return detail(services, owner_id, task_id)


def send_in_transaction(
    services: SharedServices,
    conn: Any,
    owner_id: int,
    body: TaskMessage | TaskRetry,
    *,
    task_id: str | None = None,
    project_id: str | None = None,
    retry_of_turn_id: str | None = None,
    prepared: PreparedSubmission,
    prepared_configuration: tuple[str, str] | None,
) -> tuple[str, str]:
    """Admit one task turn inside the caller's transaction; never start execution."""
    task, body, previous = _submission_state(
        services,
        conn,
        owner_id,
        body,
        task_id=task_id,
        project_id=project_id,
        retry_of_turn_id=retry_of_turn_id,
    )
    tasks, turns = services.project_task_repo, services.project_task_turn_repo
    if previous is not None:
        assert task is not None
        return task.task_id, previous.turn_id
    validate_preparation(services, conn, owner_id, prepared, task)
    assert prepared_configuration is not None
    configuration, model_ref = prepared_configuration
    if task:
        configuration = json.dumps(
            {
                **json.loads(configuration),
                "web_search_enabled": task.web_search_enabled,
                "approval_policy_at_start": approval_policy.in_transaction(
                    conn, services, owner_id
                ).model_dump(mode="json"),
            }
        )
        if retry_of_turn_id is not None:
            configuration = json.dumps(
                {**json.loads(configuration), "retry_of_turn_id": retry_of_turn_id}
            )
        turn_id, now = "turn_" + new_ulid(), now_ms()
        configuration = _input_configuration(
            services, conn, owner_id, task.task_id, body, configuration, model_ref
        )
        if not tasks.activate(conn, task.task_id, turn_id, now):
            raise OctopError.localized(ErrorCode.TASK_BUSY, status=409)
        turns.insert(
            conn,
            ProjectTaskTurnRow(
                turn_id,
                task.task_id,
                body.request_id,
                body.prompt,
                "",
                "queued",
                "",
                "",
                configuration,
                model_ref,
                "",
                False,
                now,
                now,
            ),
        )
        task_id = task.task_id
    else:
        if body.expected_configuration_revision not in {None, 1}:
            raise OctopError.localized(ErrorCode.TASK_REVISION_CONFLICT)
        if body.expected_project_revision is not None:
            project = get_project(services, owner_id, project_id) if project_id else None
            if project is None or project.revision != body.expected_project_revision:
                raise OctopError.localized(ErrorCode.PROJECT_REVISION_CONFLICT)
        selected_expert_id = (
            body.selected_expert_id if isinstance(body, ProjectTaskCreate) else None
        )
        action_mode = body.action_mode if isinstance(body, ProjectTaskCreate) else "craft"
        selected_engine = body.engine if isinstance(body, ProjectTaskCreate) else EngineSelection()
        task_id, turn_id, now = "task_" + new_ulid(), "turn_" + new_ulid(), now_ms()
        web_search_enabled = (
            body.web_search_enabled if isinstance(body, ProjectTaskCreate) else True
        )
        start_policy = approval_policy.in_transaction(conn, services, owner_id)
        configuration = json.dumps(
            {
                **json.loads(configuration),
                "web_search_enabled": web_search_enabled,
                "approval_policy_at_start": start_policy.model_dump(mode="json"),
            }
        )
        initial_title = body.prompt
        if not initial_title:
            user = services.user_repo.get(owner_id)
            initial_title = tr(
                "projects.attachment_task_title",
                normalize_locale(user.locale if user else "zh"),
            )
        tasks.insert(
            conn,
            ProjectTaskRow(
                task_id,
                owner_id,
                project_id,
                provisional_title(initial_title),
                body.request_id,
                turn_id,
                now,
                now,
                approval_mode=start_policy.mode,
                approval_revision=start_policy.revision,
                approval_updated_at=int(start_policy.updated_at.timestamp() * 1000),
                web_search_enabled=web_search_enabled,
                selected_expert_id=selected_expert_id,
                action_mode=action_mode,
                execution_json=engine_catalog.execution_json(selected_engine),
            ),
        )
        configuration = _input_configuration(
            services, conn, owner_id, task_id, body, configuration, model_ref
        )
        turns.insert(
            conn,
            ProjectTaskTurnRow(
                turn_id,
                task_id,
                body.request_id,
                body.prompt,
                "",
                "queued",
                "",
                "",
                configuration,
                model_ref,
                "",
                False,
                now,
                now,
            ),
        )
        if body.prompt:
            services.project_task_title_repo.enqueue(conn, task_id)
    assert task_id is not None
    return task_id, turn_id


def cancel(services: SharedServices, owner_id: int, task_id: str, turn_id: str) -> TaskDetail:
    with services.db.transaction() as conn:
        tasks, turns = services.project_task_repo, services.project_task_turn_repo
        if not tasks.lock_owner(conn, owner_id):
            raise OctopError.localized(ErrorCode.FORBIDDEN)
        task = tasks.get_in_transaction(conn, task_id, owner_id)
        if task is None:
            raise OctopError.localized(ErrorCode.NOT_FOUND)
        turn = turns.get_in_transaction(conn, turn_id)
        if turn is None or turn.task_id != task_id:
            raise OctopError.localized(ErrorCode.NOT_FOUND)
        if task.active_turn_id == turn_id:
            turns.cancel(conn, turn_id, now_ms())
            turn = turns.get_in_transaction(conn, turn_id)
            if turn is not None and turn.status == "cancelled":
                interactions.close(services, conn, turn, now_ms())
                services.project_task_tool_call_repo.close_turn(
                    conn, turn_id, "cancelled", "", now_ms()
                )
                tasks.finish(conn, task_id, turn.turn_id, now_ms())
    return detail(services, owner_id, task_id)


def resources(services: SharedServices, owner_id: int) -> TaskResources:
    from octop.infra.projects.expert_profiles import (
        _workspace,
        composer_skill_defaults,
        expert_profile_supported,
    )
    from octop.infra.projects.expert_teams import member_ids as team_member_ids

    available = project_resources(services, owner_id)
    providers = ProviderStore(services.provider_repo)
    models = [
        TaskModelOption(
            model_ref=f"{provider.name}/{model['id']}",
            name=str(model.get("name") or model["id"]),
            provider_kind=provider.kind,
            supports_images=providers.is_model_ref_multimodal(f"{provider.name}/{model['id']}"),
            reasoning_config=TaskReasoningCapability.model_validate(capability)
            if (
                capability := providers.get_model_reasoning_capability(
                    f"{provider.name}/{model['id']}"
                )
            )
            else None,
        )
        for provider in providers.iter_usable_rows()
        for model in provider.get_models()
        if providers.is_model_ref_usable(f"{provider.name}/{model['id']}")
    ]
    experts = []
    rows = [
        *services.agent_repo.list_by_user(owner_id, include_disabled=False, exclude_resident=True),
        *services.agent_repo.list_shared(exclude_user_id=owner_id),
    ]
    for row in rows:
        if row.kind not in {"expert", "team"} or (
            row.kind == "team" and (row.is_shared or row.user_id != owner_id)
        ):
            continue
        if services.resident_conversation_repo.get_by_agent(row.agent_id) is not None:
            continue
        try:
            config = json.loads(row.config_json or "{}")
            if not isinstance(config, dict) or not expert_profile_supported(config):
                continue
            workspace = _workspace(services, row, config)
            default_skills, has_workspace_skills = composer_skill_defaults(
                row, config, workspace, available.skills
            )
            roster = team_member_ids(services, owner_id, workspace) if row.kind == "team" else []
        except (OctopError, ValueError, OSError):
            continue
        experts.append(
            TaskExpertOption(
                expert_id=row.agent_id,
                name=row.name,
                description=row.description,
                config_revision=row.config_revision,
                kind=cast(Literal["expert", "team"], row.kind),
                is_shared=bool(row.is_shared),
                is_owner=row.user_id == owner_id,
                member_ids=roster,
                default_skills=[SkillBinding.model_validate(skill) for skill in default_skills],
                has_workspace_skills=has_workspace_skills,
            )
        )
    return TaskResources(
        **available.model_dump(),
        experts=experts,
        models=models,
        default_model_ref=resolve_model_ref(services),
        routing=routing_capabilities(services),
        approval=approval_policy.capabilities(services),
        execution_engines=engine_catalog.catalog(services, owner_id),
    )


def patch(services: SharedServices, owner_id: int, task_id: str, body: TaskPatch) -> TaskDetail:
    with services.db.transaction() as conn:
        repo = services.project_task_repo
        if not repo.lock_owner(conn, owner_id):
            raise OctopError.localized(ErrorCode.FORBIDDEN)
        current = repo.get_in_transaction(conn, task_id, owner_id)
        if current is None:
            raise OctopError.localized(ErrorCode.NOT_FOUND)
        assert_project_writable(services, conn, owner_id, current.project_id)
        if current.list_revision != body.expected_revision:
            raise OctopError.localized(ErrorCode.TASK_REVISION_CONFLICT)
        if {"selected_expert_id", "action_mode", "engine"} & body.model_fields_set:
            from octop.infra.projects.expert_profiles import resolve_expert_profile

            selected = (
                body.selected_expert_id
                if "selected_expert_id" in body.model_fields_set
                else current.selected_expert_id
            )
            if current.active_turn_id:
                raise OctopError.localized(ErrorCode.TASK_BUSY)
            resolve_expert_profile(services, owner_id, selected)
            selection = body.engine or engine_catalog.read_engine(current.execution_json)
            engine_catalog.validate_engine(
                services, owner_id, selection, action_mode=body.action_mode or current.action_mode
            )
            updated = repo.update_configuration(
                conn,
                replace(
                    current,
                    selected_expert_id=selected,
                    action_mode=body.action_mode or current.action_mode,
                    execution_json=engine_catalog.execution_json(selection, current.execution_json)
                    if selection != engine_catalog.read_engine(current.execution_json)
                    else current.execution_json,
                    updated_at=now_ms(),
                ),
                body.expected_configuration_revision or 1,
            )
            if updated is None:
                raise OctopError.localized(ErrorCode.TASK_REVISION_CONFLICT)
            current = updated
        title = body.title if body.title is not None else current.title
        archived = body.archived if body.archived is not None else current.archived
        web_search_enabled = (
            body.web_search_enabled
            if body.web_search_enabled is not None
            else current.web_search_enabled
        )
        pinned = body.pinned if body.pinned is not None else current.pinned
        if archived and not current.archived and current.active_turn_id:
            raise OctopError.localized(ErrorCode.TASK_BUSY)
        if (
            body.title is not None
            or archived != current.archived
            or web_search_enabled != current.web_search_enabled
            or pinned != current.pinned
        ):
            updated = repo.update_metadata(
                conn,
                replace(
                    current,
                    title=title,
                    archived=archived,
                    web_search_enabled=web_search_enabled,
                    pinned=pinned,
                    updated_at=now_ms(),
                ),
                current.list_revision,
                manual_title=body.title is not None,
            )
            if updated is None:
                raise OctopError.localized(ErrorCode.TASK_REVISION_CONFLICT)
            if body.title is not None:
                services.project_task_title_repo.skip_pending(conn, task_id)
    return detail(services, owner_id, task_id)


def delete(services: SharedServices, owner_id: int, task_id: str) -> None:
    with services.db.transaction() as conn:
        repo = services.project_task_repo
        if not repo.lock_owner(conn, owner_id):
            raise OctopError.localized(ErrorCode.FORBIDDEN)
        row = repo.get_in_transaction(conn, task_id, owner_id)
        if row is None:
            raise OctopError.localized(ErrorCode.NOT_FOUND)
        if row.active_turn_id:
            raise OctopError.localized(ErrorCode.TASK_BUSY)
        repo.delete(conn, row)
