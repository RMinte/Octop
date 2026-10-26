"""Durable task runner. Each turn gets a fresh, optionally expert-backed harness.

Canonical messages and tool results are restored as context. Interrupted tool calls are
never automatically replayed. SQL leases keep multiple HTTP workers from claiming
the same turn and release abandoned runs after a process crash.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import time
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Iterable
from contextvars import ContextVar
from pathlib import Path
from typing import Any, cast

import aiosqlite
from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, ToolMessage, message_to_dict, messages_from_dict
from langchain_core.messages.tool import ToolCall
from langchain_core.tools import ToolException
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command
from octop_harness import HarnessAgent as HarnessAgentBase
from octop_harness import HarnessAgentConfig
from octop_harness.mcp import aload_mcp_tools
from octop_harness.messages import extract_call_response
from octop_harness.plugins import collect_plugin_tool_configs
from octop_harness.request import ChatRequest

from octop.i18n import tool_display_name, tr
from octop.infra.agents.middleware.reasoning import CONFIG_KEY, ReasoningRequestMiddleware
from octop.infra.agents.plugins.plugin_tool_names import extract_original_plugin_label
from octop.infra.agents.providers.box import OctopHarnessAgent as HarnessAgent
from octop.infra.agents.providers.store import ProviderStore
from octop.infra.agents.security.policy_store import SecuritySettingsStore
from octop.infra.connectors.builder import gateway_mcp_server_names, inject_missing_gateway_tools
from octop.infra.connectors.service import ConnectorService
from octop.infra.conversations.updates import CommittedUpdates
from octop.infra.db.repos.project_task_turns import ProjectTaskTurnRow
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.gateway.media.attachment_hints import content_blocks_need_vision
from octop.infra.knowledge.hint import KnowledgeSearchHintMiddleware
from octop.infra.knowledge.tools import SEARCH_KNOWLEDGE_TOOL, build_knowledge_tools
from octop.infra.projects import (
    approval_policy,
    approval_runtime,
    input_attachments,
    interactions,
    workbench,
)
from octop.infra.projects.approval_reviewer import TaskApprovalReviewWorker
from octop.infra.projects.artifacts import SERVER_ARTIFACT_TOOLS, TaskArtifactPublisher
from octop.infra.projects.capabilities import plugin_capabilities, subagent_specs
from octop.infra.projects.checkpoint_saver import TaskCheckpointSaver, checkpoint_fence
from octop.infra.projects.checkpoints import (
    checkpoint,
    prepare_wait,
    resume,
    resume_decisions,
)
from octop.infra.projects.client_tool_models import ClientToolDefinition
from octop.infra.projects.client_tools import ClientToolBroker
from octop.infra.projects.collaboration import TaskTeam, guard_subagents
from octop.infra.projects.compaction import run_compaction
from octop.infra.projects.content import TaskContent, TaskContentBlock
from octop.infra.projects.context import (
    TaskContextMiddleware,
    capture_memory,
    close_pending_tools,
    is_compaction,
    model_context,
    task_backend,
)
from octop.infra.projects.control import ASK_USER, TaskControlMiddleware, question_tool
from octop.infra.projects.execution import TaskExecution, strip_step_metadata
from octop.infra.projects.expert_memory import EXPERT_MEMORY_TOOLS, ExpertMemory
from octop.infra.projects.expert_memory_extraction import ExpertMemoryWorker
from octop.infra.projects.expert_profiles import (
    ExpertAccessMiddleware,
    runtime_plugin_configuration,
)
from octop.infra.projects.expert_team_runtime import ExpertTeamRuntime
from octop.infra.projects.file_paths import TaskFilePathMiddleware
from octop.infra.projects.hitl import TaskHumanInTheLoopMiddleware
from octop.infra.projects.image_middleware import TaskImageMaterializeMiddleware
from octop.infra.projects.image_tools import READ_TASK_IMAGE, read_image_tool
from octop.infra.projects.input_attachment_models import TaskInputAttachment
from octop.infra.projects.interaction_models import TaskInteraction
from octop.infra.projects.models import ProjectFields, SkillBinding
from octop.infra.projects.narration import (
    ProgressSnapshot,
    TaskProgressAgent,
    TaskProgressNarrator,
    progress_context,
    tool_progress_facts,
)
from octop.infra.projects.planning import PLANNING, TaskPlanning, plan_report
from octop.infra.projects.progress import (
    NOTIFY_USER,
    TaskPlan,
    progress_introduction,
    progress_prompt,
    progress_report,
    progress_tool,
)
from octop.infra.projects.recovery import (
    RecoveryMode,
    TaskInterruption,
    TaskRecovery,
    diagnostic,
    transient,
)
from octop.infra.projects.resource_snapshot import SnapshotError, load_snapshot, save_snapshot
from octop.infra.projects.service import _validate
from octop.infra.projects.stream import TASK_PROTOCOL
from octop.infra.projects.tasks import now_ms
from octop.infra.projects.title import TaskTitleWorker
from octop.infra.projects.usage import message_usage, save_usage
from octop.infra.projects.workbench_browser import TaskBrowserMiddleware, task_skill_files
from octop.infra.skills.install import prepare_skillhub_package
from octop.infra.skills.skill_package_store import SkillPackageStore
from octop.infra.skills.skillhub_market import download_skillhub_package_files
from octop.infra.usage.model_calls import model_call_scope
from octop.infra.users.resource_policy import assert_token_quota_available
from octop.infra.utils.locale import normalize_locale
from octop.infra.utils.ulid import new_ulid

logger = logging.getLogger(__name__)
_OUTPUT_SAVE_SECONDS = 0.1
_HEARTBEAT_SECONDS = 0.5
_MAX_CONCURRENT_TURNS = 4
_plan_persistence: ContextVar[Callable[[TaskPlan, str, dict[str, Any]], Awaitable[None]] | None] = (
    ContextVar("task_plan_persistence", default=None)
)
_progress_persistence: ContextVar[
    Callable[[str, str, str | None, str | None], Awaitable[bool]] | None
] = ContextVar("task_progress_persistence", default=None)


class SkillPreparationError(Exception):
    def __init__(self, resource_id: str):
        super().__init__("skill preparation failed")
        self.resource_id = resource_id


class McpToolsUnavailable(ConnectionError):
    def __init__(self, resource_id: str):
        super().__init__("MCP tool discovery unavailable")
        self.resource_id = resource_id


class TaskActionModeMiddleware(AgentMiddleware[Any, Any]):
    """Enforce read-only modes at dispatch, including delegated graphs."""

    def __init__(self, mode: str, locale: str, readonly_tools: set[str]) -> None:
        self.mode, self.locale = mode, locale
        self.readonly_tools = readonly_tools | {
            "read_file",
            "ls",
            "glob",
            "grep",
            "web_search",
            "web_fetch",
            "task",
            "agent_list",
            "ask_agent",
            ASK_USER,
            PLANNING,
            NOTIFY_USER,
            READ_TASK_IMAGE,
            SEARCH_KNOWLEDGE_TOOL,
            "memory_search",
            "memory_get",
            *EXPERT_MEMORY_TOOLS,
        }

    def allows(self, name: str) -> bool:
        return self.mode == "craft" or name in self.readonly_tools

    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]
    ) -> ModelResponse:
        if self.mode == "craft":
            return await handler(request)
        tools = [
            tool
            for tool in request.tools
            if self.allows(tool.get("name", "") if isinstance(tool, dict) else tool.name)
        ]
        return await handler(request.override(tools=tools))

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        if not self.allows(request.tool_call["name"]):
            return ToolMessage(
                content=tr("projects.task_action_mode_restricted", self.locale, mode=self.mode),
                name=request.tool_call["name"],
                tool_call_id=str(request.tool_call["id"]),
                status="error",
            )
        return await handler(request)


def _readonly_mcp_tools(tools: list[Any]) -> set[str]:
    result = set()
    for tool in tools:
        metadata = getattr(tool, "metadata", None) or {}
        annotations = metadata.get("annotations") or metadata
        if isinstance(annotations, dict) and annotations.get("readOnlyHint") is True:
            result.add(tool.name)
    return result


def _skill_files(
    services: SharedServices, fields: ProjectFields
) -> list[tuple[str, list[tuple[str, bytes]]]]:
    result = []
    seen: set[str] = set()
    store = SkillPackageStore(
        repo=services.skill_package_repo, root=services.paths.skill_packages_dir
    )
    for skill in fields.skills:
        if skill.slug in seen:
            raise ValueError("duplicate runtime skill name")
        seen.add(skill.slug)
        try:
            if isinstance(skill, SkillBinding):
                root = store.package_skills_dir(skill.package_id) / skill.slug
                files = [
                    (p.relative_to(root).as_posix(), p.read_bytes())
                    for p in root.rglob("*")
                    if p.is_file()
                ]
                if not any(name == "SKILL.md" for name, _ in files):
                    raise ValueError("skill manifest unavailable")
            else:
                package = prepare_skillhub_package(
                    skill.slug, download_skillhub_package_files(skill.slug), display_name=skill.name
                )
                files = list(package.files)
        except Exception as exc:
            raise SkillPreparationError(skill.slug) from exc
        result.append((skill.slug, files))
    return result


def _save_progress(
    content: TaskContent,
    text: str,
    call_id: str,
    plan_id: str | None,
    step_id: str | None,
    locale: str,
) -> bool:
    previous = next(
        (b for b in content.blocks if b.type == "progress" and b.tool_call_id == call_id), None
    )
    if previous is not None:
        if previous.text != text:
            raise ToolException(tr("projects.progress_call_reused", locale))
        return previous.introduction is True
    introduction = not any(b.type == "progress" and b.introduction is True for b in content.blocks)
    content.report_progress(
        call_id, text, plan_id=plan_id, step_id=step_id, introduction=introduction
    )
    return introduction


def _source_access_middleware(
    services: SharedServices, owner_id: int, task_id: str, origin: dict[str, Any]
) -> Any:
    from octop.infra.projects.source_access import SourceAccessMiddleware

    return SourceAccessMiddleware(services, owner_id, task_id, origin)


async def run_turn(
    services: SharedServices, owner_id: int, turn: ProjectTaskTurnRow, history: list[Any]
) -> AsyncGenerator[dict[str, Any], None]:
    """Yield only public text/tool-progress events; credentials stay in-process."""
    task = await asyncio.to_thread(services.project_task_repo.get_internal, turn.task_id, owner_id)
    with model_call_scope(
        services,
        user_id=owner_id,
        purpose="main",
        agent_id=turn.task_id,
        project_id=task.project_id if task else None,
        task_id=turn.task_id,
        turn_id=turn.turn_id,
        submission_id=turn.request_id,
    ):
        async with contextlib.aclosing(_run_turn(services, owner_id, turn, history)) as events:
            async for event in events:
                yield event


async def _run_turn(
    services: SharedServices, owner_id: int, turn: ProjectTaskTurnRow, history: list[Any]
) -> AsyncGenerator[dict[str, Any], None]:
    raw = json.loads(turn.configuration_json)
    from octop.infra.projects.source_access import restrict_external_configuration

    raw = restrict_external_configuration(raw)
    if raw.get("execution_kind") == "resident":
        from octop.infra.projects.resident_tasks import run

        async with contextlib.aclosing(run(services, owner_id, turn, history)) as native_events:
            async for event in native_events:
                yield event
        return
    if raw.get("source_origin"):
        from octop.infra.projects.source_access import assert_source_access

        await asyncio.to_thread(
            assert_source_access, services, owner_id, turn.task_id, raw["source_origin"]
        )
    if is_compaction(turn.prompt, raw.get("attachment_ids", [])):
        async for event in run_compaction(services, owner_id, turn, history, factory=HarnessAgent):
            yield event
        return
    action_timeline_enabled = bool(raw.get("action_timeline_enabled", True))
    fields = ProjectFields.model_validate(
        {key: raw[key] for key in ProjectFields.model_fields if key in raw}
    )
    recovery = json.loads(turn.recovery_json) if turn.recovery_json else {}
    recovery_mode = cast(
        RecoveryMode, recovery.get("mode") or ("wait" if turn.checkpoint_id else "prepare")
    )
    if turn.pause_json and json.loads(turn.pause_json).get("checkpoint_id") == turn.checkpoint_id:
        recovery_mode = "wait"
    runtime_dir = services.paths.root / "project-tasks" / turn.task_id
    snapshot_dir = runtime_dir / turn.turn_id / "resources"
    resource_phase = "skill_snapshot"
    try:
        cached_files = await asyncio.to_thread(load_snapshot, snapshot_dir, turn.configuration_json)
        if cached_files is None and recovery_mode in {"graph", "wait"}:
            raise SnapshotError("missing recovery skill snapshot")
        resource_phase = "resource_validation"
        await asyncio.to_thread(
            _validate,
            services,
            owner_id,
            fields,
            cached_skills=cached_files is not None,
            allow_expert_team=isinstance((raw.get("expert_profile") or {}).get("team"), dict),
        )
        resource_phase = "skill_prepare"
        files = (
            cached_files
            if cached_files is not None
            else await asyncio.to_thread(_skill_files, services, fields)
        )
        resource_phase = "mcp_connect"
        connectors = ConnectorService(
            repo=services.connector_repo,
            secret_repo=services.secret_repo,
            settings_repo=services.settings_repo,
            config=services.config,
        )
        # Only selected servers are loaded; connector credentials stay in-process.
        configs = await connectors.mcp_configs_for_user(owner_id) if fields.mcp_servers else {}
        if not set(fields.mcp_servers) <= configs.keys():
            raise ValueError("selected MCP unavailable")
        selected = {name: configs[name] for name in fields.mcp_servers}
        gateway_names = await asyncio.to_thread(
            gateway_mcp_server_names, connector_repo=services.connector_repo, user_id=owner_id
        )
        remote = {name: spec for name, spec in selected.items() if name not in gateway_names}
        tools = await aload_mcp_tools(remote) if remote else []
        for name in remote:
            if not any(t.name.startswith(name + "_") for t in tools):
                # Harness logs and suppresses individual transport exceptions.
                # Preserve the exact missing server; do not invent its cause.
                raise McpToolsUnavailable(name)
    except Exception as exc:
        raise TaskInterruption(
            diagnostic(
                resource_phase,
                exc,
                can_resume=transient(exc),
                reason_code="TASK_RESOURCE_UNAVAILABLE",
                resource_type="skill"
                if resource_phase.startswith("skill")
                else "mcp"
                if resource_phase == "mcp_connect"
                else "project",
                resource_id=getattr(exc, "resource_id", None),
            ),
            recovery_mode,
            turn.checkpoint_id,
        ) from exc
    providers = ProviderStore(services.provider_repo)
    if not await asyncio.to_thread(providers.is_model_ref_usable, turn.model_ref):
        raise OctopError.localized(ErrorCode.TASK_MODEL_UNAVAILABLE)
    user = await asyncio.to_thread(services.user_repo.get, owner_id)
    locale = normalize_locale(user.locale if user else "zh")
    action_gate = TaskActionModeMiddleware(
        str(raw.get("action_mode") or "craft"), locale, _readonly_mcp_tools(tools)
    )
    profile = raw.get("expert_profile")
    expert_memory = None
    if isinstance(profile, dict):
        task_row = await asyncio.to_thread(services.project_task_repo.get, turn.task_id, owner_id)
        expert_memory = ExpertMemory(
            services,
            owner_id,
            turn.task_id,
            turn.turn_id,
            task_row.project_id if task_row else None,
            profile,
            enabled=raw.get("expert_memory_enabled"),
            worker_id=turn.worker_id,
        )
    knowledge_config = {
        "user": str(owner_id),
        "user_is_admin": bool(user and user.role == "admin"),
        "locale": locale,
        "knowledge_base_ids": raw.get("knowledge_base_ids", []),
        "knowledge_base_catalog": raw.get("knowledge_base_catalog", []),
    }
    user_content = input_attachments.content(
        turn.prompt,
        raw.get("attachment_ids", []),
        [TaskInputAttachment.model_validate(item) for item in raw.get("attachments", [])],
        locale=locale,
    )
    needs_images = content_blocks_need_vision(user_content) or any(
        content_blocks_need_vision(
            message.get("content")
            if isinstance(message, dict)
            else getattr(message, "content", None)
        )
        for message in history
    )
    if needs_images and not await asyncio.to_thread(
        providers.is_model_ref_multimodal, turn.model_ref
    ):
        raise OctopError.localized(ErrorCode.TASK_IMAGE_MODEL_REQUIRED)
    configs_provider = await asyncio.to_thread(providers.build_harness_configs)
    from octop.infra.projects.model_failover import automatic_route, task_failover_middleware

    failover = await task_failover_middleware(services, owner_id, turn, raw, configs_provider)
    workspace = Path(fields.directory) if fields.directory else runtime_dir / "workspace"
    if fields.directory is None:
        await asyncio.to_thread(workspace.mkdir, parents=True, exist_ok=True)
    workbench_scope = await asyncio.to_thread(workbench.resolve, services, owner_id, turn.task_id)
    system_dir = runtime_dir / turn.turn_id
    try:
        system_prefix = system_dir.relative_to(workspace).as_posix()
    except ValueError:
        system_prefix = f".octop/tasks/{turn.task_id}/{turn.turn_id}"
    await asyncio.to_thread(
        assert_token_quota_available, services.user_policy_repo, services.usage_repo, owner_id
    )
    prompt = tr("projects.task_prompt", locale)
    prompt += "\n\n" + tr("projects.task_file_paths", locale)
    if isinstance(profile, dict):
        content = (
            str(profile.get("system_prompt") or "")
            + "\n\n"
            + "\n\n".join(
                f"[{item['path']}]\n{item['content']}" for item in profile.get("profile_files", [])
            )
        )
        prompt += "\n\n" + tr(
            "projects.expert_profile_prompt", locale, name=profile.get("name", ""), content=content
        )
        if expert_memory is not None:
            if expert_memory.manual_context():
                prompt += "\n\n" + tr("projects.expert_manual_priority", locale)
            recalled = await expert_memory.prompt(turn.prompt)
            if recalled:
                prompt += "\n\n" + tr("projects.expert_memory_prompt", locale, content=recalled)
    if fields.instructions:
        prompt += "\n\n" + fields.instructions
    if isinstance(profile, dict) and isinstance(profile.get("team"), dict):
        prompt += "\n\n" + tr("projects.task_team_prompt", locale)
    options = raw.get("execution_options", {})
    definitions = (
        [ClientToolDefinition.model_validate(t) for t in options.get("client_tools") or []]
        if action_gate.mode == "craft"
        else []
    )
    broker = ClientToolBroker(services, owner_id, turn, options.get("client_id") or "", definitions)
    client_tools = broker.tools()
    prompt += "\n\n" + tr(
        "projects.task_environment_prompt",
        locale,
        environment=json.dumps(
            {
                "server_workspace": str(workspace),
                "client_directory": fields.client_directory,
                "client_tools": [tool.name for tool in client_tools],
            },
            ensure_ascii=False,
        ),
    )
    if (
        {tool.name for tool in client_tools}
        | SERVER_ARTIFACT_TOOLS
        | {READ_TASK_IMAGE, NOTIFY_USER, PLANNING, SEARCH_KNOWLEDGE_TOOL}
    ) & {tool.name for tool in tools}:
        raise OctopError.localized(ErrorCode.TASK_RESOURCE_UNAVAILABLE)
    active_mcp = [*fields.mcp_servers, *(["client"] if definitions else [])]
    if definitions:
        prompt += "\n\n" + tr("projects.client_tools_prompt", locale)
    publisher = TaskArtifactPublisher(services, owner_id, turn, workspace, locale)
    prompt += "\n\n" + tr("projects.artifacts_prompt", locale)
    prompt += "\n\n" + tr("projects.task_images_prompt", locale)
    prompt += "\n\n" + tr("projects.planning_prompt", locale)
    if action_timeline_enabled:
        prompt += "\n\n" + progress_prompt(locale)
    task_files = [
        item
        for item in await asyncio.to_thread(
            input_attachments.for_task, services, owner_id, turn.task_id
        )
        if item.workspace_path is not None
    ]
    if task_files:
        prompt += "\n\n" + tr("projects.task_files_prompt", locale)
        prompt += "\n\n" + "\n\n".join(
            tr(
                "projects.task_file_hint",
                locale,
                name=item.name,
                attachment_id=item.attachment_id,
                path=item.workspace_path,
            )
            for item in task_files
        )
    try:
        expert_plugins = (
            await asyncio.to_thread(runtime_plugin_configuration, services, owner_id, profile)
            if isinstance(profile, dict)
            else None
        )
        plugin_tool_configs = collect_plugin_tool_configs(expert_plugins)
        plugin_tools, plugin_middleware, plugin_skills = await asyncio.to_thread(
            plugin_capabilities,
            fields,
            include_skills=cached_files is None,
            **({"agent_plugins": expert_plugins} if expert_plugins is not None else {}),
            reserved={t.name for t in [*tools, *client_tools]}
            | SERVER_ARTIFACT_TOOLS
            | {
                ASK_USER,
                "task",
                "agent_list",
                "ask_agent",
                "read_file",
                "write_file",
                "edit_file",
                "execute",
                "ls",
                "glob",
                "grep",
                READ_TASK_IMAGE,
                SEARCH_KNOWLEDGE_TOOL,
                NOTIFY_USER,
                PLANNING,
            },
        )
        roles = await asyncio.to_thread(subagent_specs, fields, locale)
        if isinstance(profile, dict):
            denied = set(profile.get("capabilities", {}).get("tools_disabled", []))
            plugin_tools = [
                tool
                for tool in plugin_tools
                if tool.name not in denied
                and extract_original_plugin_label(tool.description or "") not in denied
            ]
        files.extend(plugin_skills)
        if (
            isinstance(profile, dict)
            and cached_files is None
            and raw.get("execution_options", {}).get("skills") is None
        ):
            for skill in profile.get("workspace_skills", []):
                entries = []
                for item in skill["files"]:
                    skill_data = base64.b64decode(item["data"], validate=True)
                    if hashlib.sha256(skill_data).hexdigest() != item["sha256"]:
                        raise ValueError("expert skill snapshot changed")
                    entries.append((item["path"], skill_data))
                files.append((skill["slug"], entries))
        if len({slug for slug, _ in files}) != len(files):
            raise ValueError("duplicate runtime skill name")
        if cached_files is None:
            applied = await task_skill_files(workbench_scope)
            applied_names = {slug for slug, _entries in applied}
            files = [(slug, entries) for slug, entries in files if slug not in applied_names]
            files.extend(applied)
            await asyncio.to_thread(save_snapshot, snapshot_dir, turn.configuration_json, files)
    except Exception as exc:
        raise TaskInterruption(
            diagnostic(
                "plugin_prepare",
                exc,
                can_resume=transient(exc),
                reason_code="TASK_RESOURCE_UNAVAILABLE",
                resource_type="plugin",
            ),
            recovery_mode,
            turn.checkpoint_id,
        ) from exc
    await asyncio.to_thread(runtime_dir.mkdir, parents=True, exist_ok=True)
    restored_content = TaskContent.restore(
        turn.turn_id, json.loads(turn.content_blocks_json), turn.content_revision
    )

    async def persist_plan(plan: TaskPlan, call_id: str, arguments: dict[str, Any]) -> None:
        if plan.steps and all(step.status in {"completed", "skipped"} for step in plan.steps):
            await execution.require_delivery()
        sink = _plan_persistence.get()
        if sink is not None:
            await sink(plan, call_id, arguments)
            return
        # Direct runtime callers have no worker heartbeat. Persist before the
        # tool returns so no next-step action can outrun its durable identity.
        restored_content.update_plan(plan, call_id=call_id, arguments=arguments)
        stopped = await asyncio.to_thread(
            services.project_task_turn_repo.progress,
            turn.turn_id,
            turn.worker_id,
            turn.reply,
            turn.activity,
            now_ms(),
            content_blocks_json=json.dumps(restored_content.dump(), ensure_ascii=False),
            content_revision=restored_content.revision,
        )
        if stopped:
            raise OctopError.localized(ErrorCode.TASK_INTERRUPTED)

    planning = TaskPlanning(restored_content.plan, locale, persist=persist_plan)
    for block in restored_content.blocks:
        if (
            block.type == "plan"
            and block.plan is not None
            and block.tool_call_id
            and block.arguments is not None
        ):
            planning.restore_call(block.tool_call_id, block.arguments, block.plan)
    execution = TaskExecution(
        planning,
        locale,
        turn.turn_id,
        action_timeline_enabled=action_timeline_enabled,
        artifact_publisher=publisher if fields.directory is None else None,
        client_directory=fields.client_directory,
    )
    execution.restore(restored_content.dump())
    await execution.delivery_pending()

    async def save_checkpoint() -> None:
        saver = agent.checkpointer
        saved = (
            await saver.aget_tuple({"configurable": {"thread_id": turn.turn_id}}) if saver else None
        )
        identifier = str(saved.config["configurable"].get("checkpoint_id") or "") if saved else ""
        if identifier:

            def persist() -> None:
                with services.db.transaction() as conn:
                    if not services.project_task_turn_repo.save_checkpoint(
                        conn, turn.turn_id, turn.worker_id, identifier, now_ms()
                    ):
                        raise OctopError.localized(ErrorCode.TASK_INTERRUPTED)

            await asyncio.to_thread(persist)

    async def persist_progress(text: str, call_id: str) -> bool:
        if not execution.is_root():
            return False
        plan_id, step_id = call_bindings.get(call_id, response_binding)
        sink = _progress_persistence.get()
        if sink is not None:
            introduction = await sink(text, call_id, plan_id, step_id)
        else:
            introduction = _save_progress(restored_content, text, call_id, plan_id, step_id, locale)
            stopped = await asyncio.to_thread(
                services.project_task_turn_repo.progress,
                turn.turn_id,
                turn.worker_id,
                turn.reply,
                turn.activity,
                now_ms(),
                content_blocks_json=json.dumps(restored_content.dump(), ensure_ascii=False),
                content_revision=restored_content.revision,
            )
            if stopped:
                raise OctopError.localized(ErrorCode.TASK_INTERRUPTED)
        return introduction

    cfg = HarnessAgentConfig(
        name="project-task",
        language=locale,
        workspace_dir=str(workspace),
        system_files_path=system_prefix,
        backend=task_backend(workspace, runtime_dir),
        default_model=turn.model_ref,
        providers=configs_provider,
        system_prompt=prompt,
        default_timezone=services.config.default_timezone,
        mcp_server_configs={name: {} for name in active_mcp},
        memory=(),
        memory_enabled=fields.memory_enabled,
        memory_capture_enabled=False,
        memory_namespace=turn.task_id,
        memory_backend_config={"db_path": str(runtime_dir / "memory.sqlite")},
        model_retry_enabled=False,
        memory_jsonl_enabled=False,
        session_log_enabled=False,
        bootstrap_enabled=False,
        subagents=roles,
        subagents_auto_load=False,
        todos_enabled=False,
        team_enabled=fields.team_enabled,
        peer_invoke_mode="sync",
        middleware=[
            *(
                [_source_access_middleware(services, owner_id, turn.task_id, raw["source_origin"])]
                if raw.get("source_origin")
                else []
            ),
            action_gate,
            TaskFilePathMiddleware(workspace, locale),
            *(
                [ExpertAccessMiddleware(services, owner_id, profile)]
                if isinstance(profile, dict)
                else []
            ),
            *(
                [
                    TaskBrowserMiddleware(
                        services, owner_id, turn.task_id, workbench_scope.view.scope_revision
                    )
                ]
                if workbench_scope.view.capabilities.browser
                else []
            ),
            *plugin_middleware,
            TaskContextMiddleware(turn.task_id, turn.turn_id),
            TaskControlMiddleware(
                services,
                owner_id,
                turn,
                locale,
                execution,
                save_checkpoint,
                _readonly_mcp_tools([*tools, *plugin_tools]),
            ),
            execution,
            failover,
            ReasoningRequestMiddleware(summary=raw.get("reasoning_mode") not in {None, "disabled"}),
            KnowledgeSearchHintMiddleware(),
            TaskImageMaterializeMiddleware(services, owner_id, turn.task_id),
        ],
        ask_user_enabled=False,
        protocol=TASK_PROTOCOL,
        media_offload_enabled=False,
        tools_disabled=frozenset(
            {
                *({"browser_use"} if not workbench_scope.view.capabilities.browser else set()),
                "desktop_screenshot",
                "send_file_to_user",
                "read_env_file",
                "write_env_file",
                *(
                    profile.get("capabilities", {}).get("tools_disabled", [])
                    if isinstance(profile, dict)
                    else []
                ),
                *({"write_file", "edit_file", "execute"} if action_gate.mode != "craft" else set()),
            }
        ),
        web_search_tools=raw.get("web_search_enabled", True),
        tools=[
            *publisher.tools(),
            *build_knowledge_tools(services),
            *plugin_tools,
            question_tool(locale),
            planning.tool(),
            *([progress_tool(locale, persist_progress)] if action_timeline_enabled else []),
            read_image_tool(services, owner_id, turn.task_id, locale),
            *(expert_memory.tools(locale) if expert_memory is not None else []),
        ],
    )
    policy = await asyncio.to_thread(
        SecuritySettingsStore(settings_repo=services.settings_repo).load
    )
    cfg = policy.apply_to_config(cfg)
    task_approvals = await asyncio.to_thread(approval_policy.settings, services)
    cfg.interrupt_on = {
        "execute": {"allowed_decisions": ["approve", "reject"]},
        **{
            name: {"allowed_decisions": ["approve", "reject"]}
            for name in task_approvals.require_human_tools
            if not name.startswith("client__") and name != ASK_USER
        },
        **(cfg.interrupt_on or {}),
        ASK_USER: {"allowed_decisions": ["respond"]},
        **{tool.name: {"allowed_decisions": ["respond"]} for tool in client_tools},
    }
    if raw.get("external_owner_approval_required"):
        # Unverified senders can request work, but cannot inherit the account's
        # automatic approval policy for writes or external tools.
        cfg.interrupt_on.update(
            {
                name: {"allowed_decisions": ["approve", "reject"]}
                for name in {
                    "write_file",
                    "edit_file",
                    "execute",
                    *[
                        tool.name
                        for tool in [*tools, *plugin_tools, *client_tools]
                        if not action_gate.allows(tool.name)
                        or tool.name not in _readonly_mcp_tools([tool])
                    ],
                }
            }
        )
    if action_gate.mode != "craft":
        cfg.interrupt_on = {
            name: spec for name, spec in cfg.interrupt_on.items() if action_gate.allows(name)
        }
        cfg.tools = [tool for tool in cfg.tools or [] if action_gate.allows(tool.name)]
    guard_subagents(cfg)
    cfg.middleware = [
        *(cfg.middleware or []),
        TaskHumanInTheLoopMiddleware(lambda: cfg.interrupt_on or {}),
    ]
    team = TaskTeam(owner_id)
    checkpoint_connection = None
    if "action_mode" in raw or isinstance(profile, dict):
        # An explicit saver prevents Harness from using expert/Task memory as
        # its graph store. Old snapshots retain their original recovery path.
        await asyncio.to_thread(system_dir.mkdir, parents=True, exist_ok=True)
        checkpoint_connection = await aiosqlite.connect(str(system_dir / "checkpoints.sqlite"))
        cfg.checkpointer = AsyncSqliteSaver(checkpoint_connection)
    try:
        agent = await asyncio.to_thread(
            HarnessAgent,
            cfg,
            agent_id=turn.task_id,
            team_manager=team.manager if fields.team_enabled else None,
        )
    except BaseException:
        await failover.aclose()
        if checkpoint_connection is not None:
            await checkpoint_connection.close()
        raise
    team.register(turn.task_id, agent)
    publisher.workspace = agent.workspace
    execution.workspace = agent.workspace
    usage: dict[str, dict[str, Any]] = {}
    member_events: list[dict[str, Any]] = []
    expert_members = (
        ExpertTeamRuntime(
            services,
            owner_id,
            turn,
            raw,
            cfg,
            common_tools=[
                *publisher.tools(),
                read_image_tool(services, owner_id, turn.task_id, locale),
            ],
            skill_files_factory=_skill_files,
            action_gate_factory=TaskActionModeMiddleware,
            usage_dict=usage,
            event_sink=member_events.append,
        )
        if isinstance(profile, dict) and isinstance(profile.get("team"), dict)
        else None
    )
    reply_text = turn.reply
    completed = False
    paused = False
    graph_started = False
    events: AsyncIterator[dict[str, Any]] | None = None

    async def prepare(agent: HarnessAgentBase) -> None:
        await input_attachments.materialize_files(services, owner_id, turn.task_id, agent.workspace)
        for slug, entries in files:
            for name, data in entries:
                await agent.workspace.aupload_bytes(
                    agent.workspace.system_rel(f"skills/{slug}/{name}"), data
                )
        await asyncio.to_thread(
            inject_missing_gateway_tools,
            agent,
            svc=connectors,
            connector_repo=services.connector_repo,
            user_id=owner_id,
            agent_id=turn.task_id,
            mcp_server_configs=selected,
        )

    async def enrich_peer(agent_id: str, request: ChatRequest) -> ChatRequest:
        if expert_members is not None:
            return await expert_members.enrich(agent_id, request)
        request.agent_id = agent_id
        request.user = str(owner_id)
        request.skills = [slug for slug, _ in files]
        request.mcp_servers = active_mcp
        request.configurable = {
            **(request.configurable or {}),
            **knowledge_config,
            CONFIG_KEY: raw.get("reasoning_overrides", {}),
            "plugin_tool_configs": plugin_tool_configs,
        }
        return request

    async def peer_finished(call: Any, thread_id: str, payload: dict[str, Any]) -> None:
        if expert_members is not None:
            await expert_members.finished(call, thread_id, payload)
            source_id, source_name = team.speaker(call.to_agent_id)
            member_events.append(
                {
                    "type": "team_message",
                    "content": extract_call_response(payload),
                    "block_key": f"member:{source_id}:{thread_id}",
                    "speaker_agent_id": source_id,
                    "speaker_name": source_name,
                }
            )
            return
        await capture_memory(
            team.registry.get(call.to_agent_id).agent,
            owner_id,
            thread_id,
            call.message,
            extract_call_response(payload),
        )

    try:
        await prepare(agent)
        if tools or client_tools:
            agent.inject_mcp_tools([*tools, *client_tools])
        if fields.team_enabled:
            team.manager.bind_invocation_hooks(enrich_request=enrich_peer)
            team.manager.bind_peer_session(after=peer_finished)
            if expert_members is not None:
                assert isinstance(profile, dict)
                await team.load_experts(
                    HarnessAgent,
                    cfg,
                    profile["team"]["members"],
                    turn.task_id,
                    runtime_dir,
                    configure=expert_members.configure,
                    prepare=expert_members.prepare,
                )
            else:
                await team.load(
                    HarnessAgent,
                    cfg,
                    roles,
                    turn.task_id,
                    runtime_dir,
                    [*tools, *client_tools],
                    prepare,
                )
        if agent.checkpointer:
            saver = TaskCheckpointSaver(services, owner_id, turn, agent.checkpointer)
            agent._checkpointer_instance = saver
            agent._base_graph.checkpointer = saver
            agent.graph.checkpointer = saver
            cfg.checkpointer = saver
        request = ChatRequest(
            messages=[*history, {"role": "user", "content": user_content}],
            thread_id=turn.turn_id,
            agent_id=turn.task_id,
            user=str(owner_id),
            source="project-task",
            skills=[slug for slug, _ in files],
            mcp_servers=active_mcp,
            callbacks=failover.callbacks,
            configurable={
                "model": turn.model_ref,
                "octop_auto_route": automatic_route(raw),
                **knowledge_config,
                CONFIG_KEY: raw.get("reasoning_overrides", {}),
                "plugin_tool_configs": plugin_tool_configs,
            },
        )
        control_calls: dict[str, str] = {}
        visible_controls: set[str] = set()
        registered = {tool.name: tool for tool in [*tools, *client_tools, *(cfg.tools or [])]}
        call_bindings = {
            block.tool_call_id: (block.plan_id, block.step_id)
            for block in restored_content.blocks
            if block.tool_call_id
        }
        restored_call_ids = set(call_bindings)
        call_details: dict[str, dict[str, Any]] = {}
        response_binding = planning.binding()

        def intent(call: ToolCall) -> dict[str, Any]:
            call_id = call["id"] or ""
            if call_id not in restored_call_ids and not call_details.get(call_id, {}).get(
                "started_at"
            ):
                try:
                    actual = strip_step_metadata(call, registered.get(call["name"]))
                    call_bindings[call_id] = (
                        execution.binding_for_call(call, registered.get(call["name"]))
                        if actual["args"] != call["args"]
                        else response_binding
                    )
                except ValueError:
                    # Invalid metadata is rejected before dispatch, never guessed.
                    call_bindings[call_id] = (None, None)
            call_bindings.setdefault(call_id, (None, None))
            plan_id, step_id = call_bindings[call_id]
            detail = {
                "call_id": call_id,
                "name": call["name"],
                "label": tr("projects.progress_action", locale)
                if call["name"] == NOTIFY_USER
                else tool_display_name(call["name"], locale),
                "plan_id": plan_id,
                "step_id": step_id,
                "arguments": strip_step_metadata(call, registered.get(call["name"]))["args"],
            }
            detail["brief"] = execution.brief_for_call(call, registered.get(call["name"]))
            call_details[call_id] = {**call_details.get(call_id, {}), **detail}
            if call["name"] in {NOTIFY_USER, PLANNING}:
                control_calls[call_id] = call["name"]
            return {"type": "tool_start", **detail}

        if turn.checkpoint_id or recovery_mode == "graph":
            await checkpoint(
                agent, turn.turn_id, "" if recovery_mode == "graph" else turn.checkpoint_id
            )
            saved_messages = await agent.aget_history(turn.turn_id, limit=1_000_000)
            # Checkpoints include earlier turns as model context. Planning state
            # and replay receipts come only from this turn's durable content above;
            # every plan mutation is persisted there before its tool returns.
            if planning.plan is not None:
                yield {"type": "plan", "plan": planning.plan}
            response_binding = planning.binding()
            last_model = next(
                (message for message in reversed(saved_messages) if isinstance(message, AIMessage)),
                None,
            )
            if last_model is not None:
                for call in last_model.tool_calls:
                    intent(call)
            if recovery_mode == "graph":
                state = await agent.protocol._graph.aget_state(
                    {"configurable": {"thread_id": turn.turn_id}}
                )
                if not state.next or any(task.interrupts for task in state.tasks):
                    raise TaskInterruption(
                        diagnostic("checkpoint", RuntimeError(), can_resume=False),
                        "graph",
                        turn.checkpoint_id,
                    )
                decisions = None
            else:
                decisions = await asyncio.to_thread(resume_decisions, services, owner_id, turn)
                if decisions is None:
                    raise OctopError.localized(ErrorCode.TASK_INTERRUPTED)
            events = resume(agent, request, decisions)
        else:
            events = agent.stream(request, protocol=TASK_PROTOCOL)
        delegating = False
        pending: dict[str, dict[str, Any]] = {}
        graph_started = True
        async for event in events:
            while member_events:
                yield member_events.pop(0)
            if broker.failure:
                raise OctopError.localized(broker.failure)
            kind = event.get("type")
            if kind == "token":
                if not delegating:
                    projected = {
                        "type": "token",
                        "content": str(event.get("content") or ""),
                        "block_key": event.get("block_key", ""),
                        "plan_id": response_binding[0],
                        "step_id": response_binding[1],
                    }
                    reply_text += projected["content"]
                    yield projected
            elif kind in {"reasoning", "reasoning_summary"}:
                if not delegating:
                    projected = {
                        "type": kind,
                        "content": str(event.get("content") or ""),
                        "block_key": event.get("block_key", ""),
                        "plan_id": response_binding[0],
                        "step_id": response_binding[1],
                    }
                    yield projected
            elif kind == "message_end":
                if not delegating:
                    yield event
            elif kind == "custom":
                data = event.get("data")
                if not isinstance(data, dict):
                    continue
                if data.get("type") == "task_step_context":
                    yield {"type": "message_end"}
                    response_binding = data.get("plan_id"), data.get("step_id")
                elif data.get("type") == "task_tool_start":
                    call_id = data["call_id"]
                    call_bindings[call_id] = data.get("plan_id"), data.get("step_id")
                    call_details[call_id] = {**call_details.get(call_id, {}), **data}
                    if data["name"] in {NOTIFY_USER, PLANNING}:
                        control_calls[call_id] = data["name"]
                    if data["name"] not in {NOTIFY_USER, PLANNING}:
                        yield {
                            **data,
                            "type": "tool_start",
                            "label": tool_display_name(data["name"], locale),
                        }
                elif data.get("type") == "task_tool_end":
                    call_id = data["call_id"]
                    call_details[call_id] = {**call_details.get(call_id, {}), **data}
                    if call_id not in control_calls or call_id in visible_controls:
                        yield {**data, "type": "tool_end"}
                elif data.get("type") == "task_tool_receipt":
                    yield {**data, "type": "tool_receipt"}
            elif kind == "tool_call_chunk":
                if not event.get("name") or not event.get("id"):
                    continue
                call_bindings.setdefault(str(event["id"]), response_binding)
                if event["name"] in {NOTIFY_USER, PLANNING}:
                    control_calls[str(event["id"])] = str(event["name"])
                    continue
                if event["name"] in {"task", "ask_agent"}:
                    delegating = True
                yield {
                    "type": "tool_start",
                    "name": str(event["name"]),
                    "call_id": str(event.get("id") or ""),
                    "label": tool_display_name(str(event["name"]), locale),
                    "plan_id": response_binding[0],
                    "step_id": response_binding[1],
                }
            elif kind == "tool_result":
                delegating = False
                for message in event.get("messages", []):
                    if isinstance(message, ToolMessage):
                        plan = plan_report(message)
                        if plan is not None:
                            yield {
                                "type": "plan",
                                "plan": plan,
                                "call_id": message.tool_call_id,
                                "arguments": call_details.get(message.tool_call_id, {}).get(
                                    "arguments"
                                ),
                            }
                            continue
                        report = progress_report(message)
                        if report is not None:
                            yield {
                                "type": "progress",
                                "call_id": message.tool_call_id,
                                "content": report,
                                "plan_id": call_bindings.get(message.tool_call_id, (None, None))[0],
                                "step_id": call_bindings.get(message.tool_call_id, (None, None))[1],
                                "introduction": progress_introduction(message),
                            }
                            continue
                        control_name = control_calls.get(message.tool_call_id)
                        if message.name in {NOTIFY_USER, PLANNING}:
                            control_name = message.name
                        failed = message.status == "error"
                        result_name = message.name or call_details.get(
                            message.tool_call_id, {}
                        ).get("name", "")
                        if (
                            result_name.startswith("client__") or result_name == ASK_USER
                        ) and isinstance(message.content, str):
                            with contextlib.suppress(ValueError, TypeError):
                                result = json.loads(message.content)
                                failed = failed or (
                                    isinstance(result, dict) and result.get("is_error") is True
                                )
                        if control_name:
                            if not failed:
                                continue
                            visible_controls.add(message.tool_call_id)
                            yield {
                                **call_details.get(message.tool_call_id, {}),
                                "type": "tool_start",
                                "name": control_name,
                                "call_id": message.tool_call_id,
                                "label": tr("projects.progress_action", locale)
                                if control_name == NOTIFY_USER
                                else tool_display_name(control_name, locale),
                            }
                        yield {
                            **call_details.get(message.tool_call_id, {}),
                            "type": "tool_end",
                            "call_id": message.tool_call_id,
                            "failed": failed,
                            "output": message.content,
                        }
            elif kind == "state_update":
                for message in (event.get("data") or {}).get("messages", []):
                    if isinstance(message, AIMessage):
                        snapshot = message_usage(message)
                        if snapshot is not None:
                            await asyncio.to_thread(save_usage, services, owner_id, turn, snapshot)
                        for call in message.tool_calls:
                            detail = intent(call)
                            if call["name"] in {NOTIFY_USER, PLANNING}:
                                continue
                            if call["name"] in {"task", "ask_agent"}:
                                delegating = True
                            yield detail
            elif kind == "usage":
                usage[str(event.get("call_id") or "model")] = dict(event.get("usage") or {})
            elif kind == "hitl_required" and event.get("interrupt_id"):
                pending[str(event.get("interrupt_id") or "")] = event
        while member_events:
            yield member_events.pop(0)
        if broker.failure:
            raise OctopError.localized(broker.failure)
        if pending:
            # Drain before reading the durable interrupt checkpoint.
            wait = await prepare_wait(agent, broker, list(pending.values()), locale, execution)
            paused = True
            yield {"type": "pause", **wait}
        else:
            if planning.phase == "executing":
                raise OctopError.localized(ErrorCode.TASK_PLAN_UNFINISHED)
            if planning.phase == "blocked":
                raise OctopError.localized(ErrorCode.TASK_PLAN_BLOCKED)
            completed = True
    except BaseException as exc:
        current = await asyncio.to_thread(services.project_task_turn_repo.get, turn.turn_id)
        if (
            isinstance(exc, asyncio.CancelledError)
            and current is not None
            and current.cancel_requested
        ):
            raise

        def unfinished_receipts() -> list[Any]:
            with services.db.connect() as conn:
                return services.project_task_execution_receipt_repo.unfinished(conn, turn.turn_id)

        unfinished = await asyncio.to_thread(unfinished_receipts)
        if (
            isinstance(exc, TaskInterruption)
            or transient(exc)
            or isinstance(exc, asyncio.CancelledError)
            or unfinished
        ):
            if events is not None:
                close = getattr(events, "aclose", None)
                if close is not None:
                    await close()
            identifier = turn.checkpoint_id
            can_resume = not unfinished
            mode: RecoveryMode = recovery_mode
            if graph_started:
                mode = "graph"
                try:
                    identifier = await checkpoint(agent, turn.turn_id)
                    state = await agent.protocol._graph.aget_state(
                        {"configurable": {"thread_id": turn.turn_id}}
                    )
                    can_resume = can_resume and bool(state.next)
                    if any(task.interrupts for task in state.tasks):
                        if turn.pause_json and identifier == turn.checkpoint_id:
                            mode = "wait"
                        else:
                            can_resume = False
                except Exception:
                    can_resume = False
            info = (
                exc.recovery
                if isinstance(exc, TaskInterruption)
                else diagnostic(
                    "tool" if unfinished else "model" if graph_started else "runtime_prepare",
                    exc,
                    can_resume=can_resume,
                    needs_reconciliation=bool(unfinished),
                    resource_type="tool" if unfinished else "model",
                    resource_id=unfinished[0].tool_name if unfinished else turn.model_ref,
                )
            )
            info.can_resume = info.can_resume and can_resume
            info.needs_reconciliation = info.needs_reconciliation or bool(unfinished)
            paused = True
            raise TaskInterruption(info, mode, identifier) from exc
        raise
    finally:
        try:
            if events is not None:
                close = getattr(events, "aclose", None)
                if close is not None:
                    await close()
            messages = [] if paused else await model_context(agent, turn.turn_id)
            if messages:
                saved = await asyncio.to_thread(
                    services.project_task_turn_repo.save_context,
                    turn.turn_id,
                    turn.worker_id,
                    json.dumps([message_to_dict(m) for m in close_pending_tools(messages, locale)]),
                )
                if saved:
                    await capture_memory(
                        agent,
                        owner_id,
                        turn.turn_id,
                        turn.prompt,
                        reply_text
                        if completed
                        else reply_text + "\n\n" + tr("projects.task_incomplete_context", locale),
                    )
                    if completed and expert_memory is not None:
                        await expert_memory.capture(turn.prompt, reply_text)
                    if completed and expert_members is not None:
                        await expert_members.flush()
        finally:
            try:
                await asyncio.to_thread(
                    services.project_task_turn_repo.record_usage,
                    turn.turn_id,
                    sum(int(values.get("input_tokens") or 0) for values in usage.values()),
                    sum(int(values.get("output_tokens") or 0) for values in usage.values()),
                )
            finally:
                try:
                    try:
                        await team.close()
                    finally:
                        await failover.aclose()
                finally:
                    if checkpoint_connection is not None:
                        await checkpoint_connection.close()


class ProjectTaskWorker:
    def __init__(self, services: SharedServices) -> None:
        self.services = services
        self.worker_id = new_ulid()
        self.closing = False
        self._wake = asyncio.Event()
        self._loop_task: asyncio.Task[None] | None = None
        self._running: dict[str, asyncio.Task[None]] = {}
        self._committed_updates = CommittedUpdates[str]()
        self._update_listeners = self._committed_updates.listeners
        self.title_worker = TaskTitleWorker(services)
        self.review_worker = TaskApprovalReviewWorker(services)
        self.expert_memory_worker = ExpertMemoryWorker(services)

    async def start(self) -> None:
        self._loop_task = asyncio.create_task(self._loop())
        await self.title_worker.start()
        await self.review_worker.start()
        self.expert_memory_worker.start()

    def wake(self) -> None:
        self._wake.set()
        self.title_worker.wake()
        self.review_worker.wake()
        self.expert_memory_worker.wake()

    def watch_updates(self, task_id: str, wake: Callable[[], None]) -> Callable[[], None]:
        """Observe committed output on this loop; database polling remains the fallback."""
        return self._committed_updates.watch(task_id, wake)

    def _notify_update(self, task_id: str) -> None:
        self._committed_updates.notify(task_id)

    async def close(self) -> None:
        self.closing = True
        self._wake.set()
        await self.title_worker.close()
        await self.review_worker.close()
        await self.expert_memory_worker.close()
        for current in tuple(self._running.values()):
            current.cancel()
        if self._loop_task:
            await self._loop_task

    def _deletion_state(
        self, owner_id: int, task_ids: set[str], local_turn_ids: tuple[str, ...]
    ) -> tuple[list[ProjectTaskTurnRow], dict[str, str], set[str]]:
        active, local, protected = [], {}, set()
        with self.services.db.read_snapshot() as conn:
            for task_id in task_ids:
                task = self.services.project_task_repo.get_in_transaction(conn, task_id, owner_id)
                if task is None:
                    raise OctopError.localized(ErrorCode.NOT_FOUND)
                if conn.execute(
                    "SELECT 1 FROM project_task_tool_calls WHERE task_id=? AND status='claimed' LIMIT 1",
                    (task_id,),
                ).fetchone():
                    protected.add(task_id)
                if task.active_turn_id:
                    turn = self.services.project_task_turn_repo.get_in_transaction(
                        conn, task.active_turn_id
                    )
                    if turn is not None:
                        active.append(turn)
            for turn_id in local_turn_ids:
                turn = self.services.project_task_turn_repo.get_in_transaction(conn, turn_id)
                if turn is not None and turn.task_id in task_ids:
                    local[turn_id] = turn.task_id
                    if turn not in active:
                        active.append(turn)
            for turn in active:
                if turn.status == "running" and (
                    self.services.project_task_execution_receipt_repo.unfinished(conn, turn.turn_id)
                ):
                    protected.add(turn.task_id)
        return active, local, protected

    async def stop_for_deletion(
        self, owner_id: int, task_ids: Iterable[str], *, timeout: float = 10.0
    ) -> bool:
        """Drain fenced tasks without cancelling dispatched external operations.

        The caller must persist its deletion fence and pause intent first. A
        timeout leaves those records intact and never cancels a cleanup twice.
        Other processes are observed through their actual durable finalization.
        """
        from octop.infra.projects import tasks

        selected = set(task_ids)
        if not selected:
            return True
        deadline = time.monotonic() + max(0.0, timeout)
        cancelled: set[asyncio.Task[None]] = set()
        while True:
            active, local, protected = await asyncio.to_thread(
                self._deletion_state, owner_id, selected, tuple(self._running)
            )
            for turn in active:
                if (
                    turn.task_id not in protected
                    and turn.status in {"queued", "running"}
                    and not turn.cancel_requested
                ):
                    await asyncio.to_thread(
                        tasks.cancel, self.services, owner_id, turn.task_id, turn.turn_id
                    )
            pending = set()
            for turn_id, task_id in local.items():
                current = self._running.get(turn_id)
                if current is None or current.done():
                    continue
                pending.add(current)
                if task_id not in protected and current not in cancelled:
                    cancelled.add(current)
                    if not current.cancelling():
                        current.cancel()
            self.wake()
            if (
                not protected
                and not pending
                and not any(turn.status in {"queued", "running"} for turn in active)
            ):
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if pending:
                # asyncio.wait neither propagates cancellation nor cancels the
                # executions when this bounded observation times out.
                await asyncio.wait(pending, timeout=min(0.05, remaining))
            else:
                await asyncio.sleep(min(0.05, remaining))

    def _recover(self) -> None:
        before, now = now_ms() - 90_000, now_ms()
        self._ready_waits(now)
        for turn in self.services.project_task_turn_repo.stale(before):
            owner = self.services.project_task_repo.runtime_owner(turn.task_id)
            with checkpoint_fence(self.services, turn), self.services.db.transaction() as conn:
                if owner is not None:
                    self.services.project_task_repo.lock_owner(conn, owner)
                current = self.services.project_task_turn_repo.get_in_transaction(
                    conn, turn.turn_id
                )
                if (
                    current is None
                    or current.updated_at >= before
                    or current.worker_id != turn.worker_id
                ):
                    continue
                uncertain = bool(
                    self.services.project_task_execution_receipt_repo.unfinished(conn, turn.turn_id)
                )
                maintenance = is_compaction(
                    current.prompt,
                    json.loads(current.configuration_json).get("attachment_ids", []),
                )
                interruption = TaskInterruption(
                    diagnostic(
                        "worker",
                        TimeoutError(),
                        can_resume=(maintenance or bool(turn.checkpoint_id)) and not uncertain,
                        needs_reconciliation=uncertain,
                    ),
                    "prepare" if maintenance else "graph",
                    turn.checkpoint_id,
                )
                if self.services.project_task_turn_repo.park_recovery(
                    conn,
                    turn.turn_id,
                    turn.worker_id,
                    turn.checkpoint_id,
                    interruption.encode(),
                    now,
                    before=before,
                ):
                    self.services.project_task_repo.wait(
                        conn, turn.task_id, turn.turn_id, "interrupted", now
                    )

    def _ready_waits(self, now: int) -> None:
        for turn in self.services.project_task_turn_repo.waiting():
            owner = self.services.project_task_repo.runtime_owner(turn.task_id)
            if owner is None:
                continue
            error = ""
            approval_timeout = False
            try:
                decisions = resume_decisions(self.services, owner, turn)
                refreshed = self.services.project_task_turn_repo.get(turn.turn_id)
                wait = json.loads(turn.pause_json)
                approval_ids = {
                    action["id"]
                    for group in wait["groups"]
                    for action in group["actions"]
                    if action["type"] == "approval"
                }
                approval_timeout = refreshed is not None and any(
                    item.interaction_id in approval_ids
                    and item.status == "expired"
                    and item.resolution is not None
                    and item.resolution.source == "timeout"
                    and not item.dispatch_started
                    for item in interactions.load(refreshed)
                )
                if approval_timeout:
                    error = "TASK_INTERACTION_CLOSED"
                elif decisions is None:
                    continue
            except OctopError as exc:
                error = str(exc.code)
            except (ValueError, KeyError, TypeError):
                error = "TASK_INTERRUPTED"
            with self.services.db.transaction() as conn:
                self.services.project_task_repo.lock_owner(conn, owner)
                if error == "TASK_CLIENT_TOOL_TIMEOUT" or approval_timeout:
                    interruption = TaskInterruption(
                        diagnostic(
                            "approval" if approval_timeout else "client_tool",
                            RuntimeError(),
                            can_resume=approval_timeout,
                            needs_reconciliation=not approval_timeout,
                            reason_code=error,
                        ),
                        "wait",
                        turn.checkpoint_id,
                    )
                    if self.services.project_task_turn_repo.interrupt_wait(
                        conn, turn.turn_id, turn.checkpoint_id, interruption.encode(), now
                    ):
                        self.services.project_task_repo.wait(
                            conn, turn.task_id, turn.turn_id, "interrupted", now
                        )
                else:
                    if (
                        self.services.project_task_turn_repo.resume_wait(
                            conn,
                            turn.turn_id,
                            now,
                            error,
                            recovery_json=json.dumps(
                                diagnostic(
                                    "checkpoint",
                                    RuntimeError(),
                                    can_resume=False,
                                    reason_code=error,
                                ).model_dump()
                            )
                            if error
                            else None,
                        )
                        and error
                    ):
                        interactions.close(self.services, conn, turn, now)
                        self.services.project_task_tool_call_repo.close_turn(
                            conn, turn.turn_id, "interrupted", error, now
                        )
                        self.services.project_task_repo.finish(
                            conn, turn.task_id, turn.turn_id, now
                        )

    async def _loop(self) -> None:
        try:
            while not self.closing:
                self._wake.clear()
                try:
                    await asyncio.to_thread(self._recover)
                    while not self.closing and len(self._running) < _MAX_CONCURRENT_TURNS:
                        turn = await asyncio.to_thread(
                            self.services.project_task_turn_repo.claim, new_ulid(), now_ms()
                        )
                        if turn is None:
                            break
                        previous_execution = self._running.get(turn.turn_id)
                        if previous_execution is not None:
                            # A fast client may resolve a durable wait before
                            # its previous graph has returned from finalization.
                            # Drain that execution before reusing its turn key.
                            await asyncio.gather(previous_execution, return_exceptions=True)
                        if self.closing:
                            # A lease claimed while close drained the previous
                            # execution must stay recoverable. Do not create a
                            # coroutine that can be cancelled before its cleanup
                            # starts, leaving the new lease abandoned.
                            mode = cast(
                                RecoveryMode,
                                json.loads(turn.recovery_json or "{}").get(
                                    "mode", "wait" if turn.checkpoint_id else "prepare"
                                ),
                            )
                            interruption = TaskInterruption(
                                diagnostic("worker", asyncio.CancelledError(), can_resume=True),
                                mode,
                                turn.checkpoint_id,
                            )
                            await asyncio.to_thread(self._park_interruption, turn, interruption)
                            self._notify_update(turn.task_id)
                            break
                        current_turn = await asyncio.to_thread(
                            self.services.project_task_turn_repo.get, turn.turn_id
                        )
                        if (
                            current_turn is None
                            or current_turn.status != "running"
                            or current_turn.worker_id != turn.worker_id
                        ):
                            continue
                        if current_turn.cancel_requested:
                            await asyncio.to_thread(
                                self._finish,
                                current_turn,
                                "cancelled",
                                current_turn.reply,
                                "",
                            )
                            self._notify_update(turn.task_id)
                            continue
                        current = asyncio.create_task(self._execute(turn))
                        self._running[turn.turn_id] = current

                        def finished(
                            future: asyncio.Task[None], turn_id: str = turn.turn_id
                        ) -> None:
                            if self._running.get(turn_id) is future:
                                self._running.pop(turn_id, None)
                            if not future.cancelled() and future.exception() is not None:
                                logger.error(
                                    "Project task execution failed outside finalization: %s",
                                    turn_id,
                                )
                            self._wake.set()

                        current.add_done_callback(finished)
                except Exception:
                    logger.exception("Project task worker iteration failed")
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), 1.0)
        finally:
            await asyncio.gather(*self._running.values(), return_exceptions=True)

    def _finish(
        self,
        turn: ProjectTaskTurnRow,
        status: str,
        reply: str,
        error: str,
        reasoning: str | None = None,
        content: TaskContent | None = None,
        recovery_json: str | None = None,
    ) -> None:
        now = now_ms()
        owner = self.services.project_task_repo.runtime_owner(turn.task_id)
        with self.services.db.transaction() as conn:
            # Serialize finalization with client claims/results and cancellation.
            if owner is not None:
                self.services.project_task_repo.lock_owner(conn, owner)
            if self.services.project_task_turn_repo.finish(
                conn,
                turn.turn_id,
                turn.worker_id,
                status,
                reply,
                error,
                now,
                reasoning=reasoning,
                content_blocks_json=json.dumps(content.dump(), ensure_ascii=False)
                if content
                else None,
                content_revision=content.revision if content else None,
                recovery_json=recovery_json,
            ):
                final = self.services.project_task_turn_repo.get_in_transaction(conn, turn.turn_id)
                cancelled = final is not None and final.status == "cancelled"
                if final is not None:
                    interactions.close(self.services, conn, final, now)
                self.services.project_task_tool_call_repo.close_turn(
                    conn,
                    turn.turn_id,
                    "cancelled" if cancelled else "interrupted",
                    "" if cancelled else error,
                    now,
                )
                self.services.project_task_repo.finish(conn, turn.task_id, turn.turn_id, now)

    async def _execute(self, turn: ProjectTaskTurnRow) -> None:
        execution_task = asyncio.current_task()
        reply, reasoning, activity, status, error = (
            turn.reply,
            turn.reasoning,
            turn.activity,
            "completed",
            "",
        )
        content = TaskContent.restore(
            turn.turn_id, json.loads(turn.content_blocks_json), turn.content_revision
        )
        pause: dict[str, Any] | None = None
        interrupted: TaskInterruption | None = None
        failure: TaskRecovery | None = None
        events: AsyncGenerator[dict[str, Any], None] | None = None
        locale = normalize_locale(None)
        output_lock = asyncio.Lock()
        saved_output = (reply, reasoning, activity, content.revision)
        last_output_save = time.monotonic()
        narrator: TaskProgressNarrator | None = None
        narration_stopped = False

        async def stop_narration() -> None:
            nonlocal narration_stopped
            narration_stopped = True
            if narrator is not None:
                await narrator.close()

        async def save_output(*, require_active: bool = False) -> bool:
            nonlocal saved_output, last_output_save
            output = (reply, reasoning, activity, content.revision)
            stopped = await asyncio.to_thread(
                self.services.project_task_turn_repo.progress,
                turn.turn_id,
                turn.worker_id,
                reply,
                activity,
                now_ms(),
                reasoning=reasoning,
                content_blocks_json=json.dumps(content.dump(), ensure_ascii=False),
                content_revision=content.revision,
                require_active=require_active,
            )
            if not stopped:
                changed = output != saved_output
                saved_output = output
                last_output_save = time.monotonic()
                if changed:
                    self._notify_update(turn.task_id)
            return stopped

        async def persist_plan(plan: TaskPlan, call_id: str, arguments: dict[str, Any]) -> None:
            async with output_lock:
                previous = content.revision
                content.update_plan(plan, call_id=call_id, arguments=arguments)
                snapshot = narrate_plan(plan) if content.revision != previous else None
                if await save_output():
                    raise OctopError.localized(ErrorCode.TASK_INTERRUPTED)
                if snapshot is not None and narrator is not None:
                    narrator.submit(snapshot)

        def narrate_plan(plan: TaskPlan) -> ProgressSnapshot | None:
            if narrator is None or narration_stopped:
                return None
            step = next((s for s in plan.steps if s.step_id == plan.active_step_id), None)
            if step is None:
                return None
            key = f"plan:{plan.plan_id}:{plan.revision}"
            content.report_progress(
                f"narration:{key}",
                tr("projects.progress_plan", locale, step=step.content),
                plan_id=plan.plan_id,
                step_id=step.step_id,
                introduction=False,
            )
            return ProgressSnapshot(
                key=key,
                facts={
                    "stage": "plan_update",
                    "goal": turn.prompt[:2000],
                    "title": plan.title,
                    "step": step.content,
                    "step_status": step.status,
                    "revision": plan.revision,
                    "context": progress_context(content),
                },
                plan_id=plan.plan_id,
                step_id=step.step_id,
            )

        def tool_digest(block: TaskContentBlock) -> str:
            source = json.dumps(
                [block.status, block.output, block.started_at, block.finished_at],
                default=str,
                ensure_ascii=False,
                sort_keys=True,
            )
            return hashlib.sha256(source.encode()).hexdigest()

        restored_tool_sources = {
            block.tool_call_id: tool_digest(block)
            for block in content.blocks
            if block.type == "tool"
        }

        def narrate_tool(call_id: str, *, finished: bool) -> None:
            if narrator is None or narration_stopped:
                return
            block = next(
                (b for b in content.blocks if b.type == "tool" and b.tool_call_id == call_id),
                None,
            )
            if block is None or block.started_at is None:
                return
            facts = tool_progress_facts(content, block, finished=finished)
            facts["goal"] = turn.prompt[:2000]
            digest = tool_digest(block)
            if restored_tool_sources.get(call_id) == digest:
                return
            narrator.submit(
                ProgressSnapshot(
                    key=f"tool:{call_id}:{'end' if finished else 'start'}:{digest}",
                    facts=facts,
                    plan_id=block.plan_id,
                    step_id=block.step_id,
                    tool_call_id=call_id,
                    source_digest=digest,
                )
            )

        async def publish_narration(snapshot: ProgressSnapshot, text: str) -> None:
            nonlocal narration_stopped
            async with output_lock:
                if (
                    narration_stopped
                    or self.closing
                    or narrator is None
                    or not narrator.is_current(snapshot)
                ):
                    return
                current = await asyncio.to_thread(
                    self.services.project_task_turn_repo.get, turn.turn_id
                )
                if (
                    current is None
                    or current.status != "running"
                    or current.cancel_requested
                    or current.pause_requested
                    or current.worker_id != turn.worker_id
                    or narration_stopped
                ):
                    return
                previous_revision = content.revision
                if snapshot.tool_call_id is not None:
                    block = next(
                        (
                            b
                            for b in content.blocks
                            if b.type == "tool" and b.tool_call_id == snapshot.tool_call_id
                        ),
                        None,
                    )
                    if (
                        block is None
                        or (block.plan_id, block.step_id) != (snapshot.plan_id, snapshot.step_id)
                        or block.status != snapshot.facts["status"]
                        or tool_digest(block) != snapshot.source_digest
                    ):
                        return
                    previous_text = block.brief
                    content.refine_brief(snapshot.tool_call_id, text)
                else:
                    plan = content.plan
                    if snapshot.plan_id is not None and (
                        plan is None
                        or plan.plan_id != snapshot.plan_id
                        or plan.revision != snapshot.facts["revision"]
                    ):
                        return
                    if snapshot.key == "opening" and (reply or plan is not None):
                        return
                    block = next(
                        (
                            b
                            for b in content.blocks
                            if b.type == "progress"
                            and b.tool_call_id == f"narration:{snapshot.key}"
                        ),
                        None,
                    )
                    if block is None:
                        return
                    previous_text = block.text
                    content.refine_progress(f"narration:{snapshot.key}", text)
                if content.revision == previous_revision:
                    return
                try:
                    stopped = await save_output(require_active=True)
                except BaseException:
                    if snapshot.tool_call_id is not None:
                        block.brief = previous_text
                    else:
                        block.text = previous_text or ""
                    content.revision = previous_revision
                    raise
                if stopped:
                    if snapshot.tool_call_id is not None:
                        block.brief = previous_text
                    else:
                        block.text = previous_text or ""
                    content.revision = previous_revision
                    narration_stopped = True

        sink_token = _plan_persistence.set(persist_plan)

        async def persist_progress(
            text: str, call_id: str, plan_id: str | None, step_id: str | None
        ) -> bool:
            async with output_lock:
                introduction = _save_progress(content, text, call_id, plan_id, step_id, locale)
                if await save_output():
                    raise OctopError.localized(ErrorCode.TASK_INTERRUPTED)
                return introduction

        progress_sink_token = _progress_persistence.set(persist_progress)

        async def heartbeat() -> None:
            while True:
                await asyncio.sleep(_OUTPUT_SAVE_SECONDS)
                async with output_lock:
                    # Flush changing text promptly without increasing idle lease writes.
                    dirty = (reply, reasoning, activity, content.revision) != saved_output
                    if not dirty and time.monotonic() - last_output_save < _HEARTBEAT_SECONDS:
                        continue
                    cancelled = await save_output()
                if cancelled or self.closing:
                    assert execution_task is not None
                    if not execution_task.cancelling():
                        execution_task.cancel()
                    return

        beat = asyncio.create_task(heartbeat())
        try:
            if self.closing:
                raise asyncio.CancelledError
            task = await asyncio.to_thread(
                self.services.project_task_repo.runtime_owner, turn.task_id
            )
            if task is None:
                raise OctopError.localized(ErrorCode.FORBIDDEN)
            rows = await asyncio.to_thread(self.services.project_task_turn_repo.list, turn.task_id)
            user = await asyncio.to_thread(self.services.user_repo.get, task)
            locale = normalize_locale(user.locale if user else "zh")
            configuration = json.loads(turn.configuration_json)
            if not is_compaction(
                turn.prompt, configuration.get("attachment_ids", [])
            ) and configuration.get("action_timeline_enabled", True):

                async def record_narration_usage(input_tokens: int, output_tokens: int) -> None:
                    await asyncio.to_thread(
                        self.services.project_task_turn_repo.record_usage,
                        turn.turn_id,
                        input_tokens,
                        output_tokens,
                    )

                progress_agent = TaskProgressAgent(
                    self.services,
                    task,
                    turn.model_ref,
                    locale,
                    record_usage=record_narration_usage,
                    task_id=turn.task_id,
                    turn_id=turn.turn_id,
                    submission_id=turn.request_id,
                )
                narrator = TaskProgressNarrator(progress_agent.generate, publish_narration)
                narrator.start()
                if not reply and not any(
                    b.type == "progress" and b.tool_call_id == "narration:opening"
                    for b in content.blocks
                ):
                    async with output_lock:
                        content.report_progress(
                            "narration:opening",
                            tr("projects.progress_received", locale),
                            introduction=not any(
                                b.type == "progress" and b.introduction is True
                                for b in content.blocks
                            ),
                        )
                        if await save_output():
                            raise OctopError.localized(ErrorCode.TASK_INTERRUPTED)
                    narrator.submit(
                        ProgressSnapshot(
                            key="opening", facts={"stage": "received", "goal": turn.prompt[:2000]}
                        )
                    )
            context = await asyncio.to_thread(
                self.services.project_task_turn_repo.context, turn.task_id
            )
            history: list[Any] = messages_from_dict(json.loads(context[1])) if context else []
            for previous in rows:
                previous_config = json.loads(previous.configuration_json)
                if previous_config.get("execution_kind") == "resident_notification":
                    if not context or previous.pk > context[0]:
                        history.extend(messages_from_dict([previous_config["resident_message"]]))
                    continue
                if not json.loads(previous.configuration_json).get(
                    "imported_history"
                ) and is_compaction(
                    previous.prompt,
                    json.loads(previous.configuration_json).get("attachment_ids", []),
                ):
                    continue
                if context and previous.pk <= context[0]:
                    if previous.pk == context[0] and previous.status != "completed":
                        history.append(
                            AIMessage(content=tr("projects.task_incomplete_context", locale))
                        )
                    continue
                if previous.status not in {"completed", "failed", "cancelled"}:
                    continue
                reply_text = previous.reply
                if previous.status != "completed":
                    reply_text += "\n\n" + tr("projects.task_incomplete_context", locale)
                history.extend(
                    [
                        {
                            "role": "user",
                            "content": input_attachments.content(
                                previous.prompt,
                                json.loads(previous.configuration_json).get("attachment_ids", []),
                                [
                                    TaskInputAttachment.model_validate(item)
                                    for item in json.loads(previous.configuration_json).get(
                                        "attachments", []
                                    )
                                ],
                                locale=locale,
                            ),
                        },
                        {"role": "assistant", "content": reply_text},
                    ]
                )
            events = run_turn(self.services, task, turn, history)
            async for event in events:
                if event["type"] == "pause":
                    await stop_narration()
                async with output_lock:
                    if event["type"] == "team_message":
                        content.end_text()
                        content.append(
                            "text",
                            event["content"],
                            event.get("block_key", ""),
                            speaker_agent_id=event.get("speaker_agent_id"),
                            speaker_name=event.get("speaker_name"),
                        )
                        content.end_text()
                    elif event["type"] == "token":
                        first_answer_token = not reply
                        reply += event["content"]
                        content.append(
                            "text",
                            event["content"],
                            event.get("block_key", ""),
                            plan_id=event.get("plan_id"),
                            step_id=event.get("step_id"),
                            speaker_agent_id=event.get("speaker_agent_id"),
                            speaker_name=event.get("speaker_name"),
                        )
                        if first_answer_token and await save_output():
                            raise OctopError.localized(ErrorCode.TASK_INTERRUPTED)
                    elif event["type"] in {"reasoning", "reasoning_summary"}:
                        reasoning += event["content"]
                        content.append(
                            event["type"],
                            event["content"],
                            event.get("block_key", ""),
                            plan_id=event.get("plan_id"),
                            step_id=event.get("step_id"),
                        )
                    elif event["type"] == "message_end":
                        content.end_text()
                    elif event["type"] == "tool_start":
                        activity = event["name"]
                        content.start_tool(
                            event["call_id"],
                            activity,
                            event.get("label"),
                            plan_id=event.get("plan_id"),
                            step_id=event.get("step_id"),
                            arguments=event.get("arguments"),
                            started_at=event.get("started_at"),
                            brief=event.get("brief"),
                            speaker_agent_id=event.get("speaker_agent_id"),
                            speaker_name=event.get("speaker_name"),
                        )
                        if event.get("started_at") is not None:
                            if await save_output():
                                raise OctopError.localized(ErrorCode.TASK_INTERRUPTED)
                            narrate_tool(event["call_id"], finished=False)
                    elif event["type"] == "tool_end":
                        content.end_tool(
                            event["call_id"],
                            failed=event["failed"],
                            output=event.get("output"),
                            file_changes=event.get("file_changes"),
                            finished_at=event.get("finished_at"),
                            duration_ms=event.get("duration_ms"),
                        )
                        activity = ""
                        if await save_output():
                            raise OctopError.localized(ErrorCode.TASK_INTERRUPTED)
                        narrate_tool(event["call_id"], finished=True)
                    elif event["type"] == "activity":
                        activity = event["name"]
                        if activity:
                            content.start_tool("", activity)
                        else:
                            content.end_tool("")
                    elif event["type"] == "pause":
                        pause = event
                    elif event["type"] == "plan":
                        previous_revision = content.revision
                        content.update_plan(
                            event["plan"],
                            call_id=event.get("call_id"),
                            arguments=event.get("arguments"),
                        )
                        if content.revision != previous_revision:
                            snapshot = narrate_plan(event["plan"])
                            if await save_output():
                                raise OctopError.localized(ErrorCode.TASK_INTERRUPTED)
                            if snapshot is not None and narrator is not None:
                                narrator.submit(snapshot)
                    elif event["type"] == "progress":
                        content.report_progress(
                            event["call_id"],
                            event["content"],
                            plan_id=event.get("plan_id"),
                            step_id=event.get("step_id"),
                            introduction=event.get("introduction"),
                        )
                    elif event["type"] == "tool_receipt":
                        content.end_tool(
                            event["call_id"],
                            failed=event["failed"],
                            output=event["output"],
                            file_changes=event.get("file_changes"),
                            from_receipt=True,
                        )
                        if await save_output():
                            raise OctopError.localized(ErrorCode.TASK_INTERRUPTED)
                        narrate_tool(event["call_id"], finished=True)
            if pause is None and not reply.strip():
                raise OctopError.localized(ErrorCode.TASK_EXECUTION_FAILED)
        except TaskInterruption as exc:
            interrupted = exc
        except asyncio.CancelledError:
            status, error = ("failed", "TASK_INTERRUPTED") if self.closing else ("cancelled", "")
        except OctopError as exc:
            status, error = "failed", str(exc.code)
            failure = diagnostic("runtime", exc, can_resume=False, reason_code=error)
        except Exception as exc:
            # Record exception class/status for diagnosis without logging credential-bearing payloads.
            logger.warning(
                "Project task turn failed: %s (%s, status=%s)",
                turn.turn_id,
                type(exc).__name__,
                getattr(exc, "status_code", None),
            )
            status, error = "failed", "TASK_EXECUTION_FAILED"
            failure = diagnostic("runtime", exc, can_resume=False, reason_code=error)
        finally:
            if events is not None:
                try:
                    await events.aclose()
                except Exception as exc:
                    logger.warning("Project task stream close failed (%s)", type(exc).__name__)
                    status, error = "failed", "TASK_EXECUTION_FAILED"
            await stop_narration()
            _plan_persistence.reset(sink_token)
            _progress_persistence.reset(progress_sink_token)
            beat.cancel()
            try:
                await beat
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning(
                    "Project task heartbeat failed: %s (%s)", turn.turn_id, type(exc).__name__
                )
                status, error = "failed", "TASK_EXECUTION_FAILED"
            parked = False
            if interrupted is not None:
                await save_output()
                parked = await asyncio.to_thread(self._park_interruption, turn, interrupted)
            if not parked and pause is not None and status == "completed":
                try:
                    parked = await asyncio.to_thread(
                        self._pause, turn, pause, reply, reasoning, content
                    )
                except Exception as exc:
                    logger.warning(
                        "Project task checkpoint failed: %s (%s)",
                        turn.turn_id,
                        type(exc).__name__,
                    )
                    status, error = "failed", "TASK_EXECUTION_FAILED"
            if not parked:
                content.finish(completed=status == "completed")
                await asyncio.to_thread(
                    self._finish,
                    turn,
                    status,
                    reply,
                    error,
                    reasoning,
                    content,
                    json.dumps(failure.model_dump()) if failure else None,
                )
            self._notify_update(turn.task_id)

    def _park_interruption(self, turn: ProjectTaskTurnRow, interruption: TaskInterruption) -> bool:
        owner = self.services.project_task_repo.runtime_owner(turn.task_id)
        now = now_ms()
        with checkpoint_fence(self.services, turn), self.services.db.transaction() as conn:
            if owner is None or not self.services.project_task_repo.lock_owner(conn, owner):
                return False
            parked = self.services.project_task_turn_repo.park_recovery(
                conn,
                turn.turn_id,
                turn.worker_id,
                interruption.checkpoint_id,
                interruption.encode(),
                now,
            )
            if parked:
                logger.info(
                    "Project task interrupted: turn=%s diagnostic=%s phase=%s cause=%s",
                    turn.turn_id,
                    interruption.recovery.diagnostic_id,
                    interruption.recovery.phase,
                    interruption.recovery.cause_type,
                )
                self.services.project_task_repo.wait(
                    conn, turn.task_id, turn.turn_id, "interrupted", now
                )
            return parked

    def _pause(
        self,
        turn: ProjectTaskTurnRow,
        pause: dict[str, Any],
        reply: str,
        reasoning: str,
        content: TaskContent,
    ) -> bool:
        owner = self.services.project_task_repo.runtime_owner(turn.task_id)
        if owner is None:
            return False
        with checkpoint_fence(self.services, turn), self.services.db.transaction() as conn:
            self.services.project_task_repo.lock_owner(conn, owner)
            current = self.services.project_task_turn_repo.get_in_transaction(conn, turn.turn_id)
            if (
                current is None
                or current.status != "running"
                or current.cancel_requested
                or current.worker_id != turn.worker_id
            ):
                return False
            now = now_ms()
            items = (
                [TaskInteraction.model_validate(i) for i in json.loads(pause["interactions_json"])]
                if pause["interactions_json"]
                else []
            )
            for item in items:
                if item.interaction_id in pause["new_approval_ids"]:
                    approval_runtime.freeze_server(
                        self.services,
                        conn,
                        owner,
                        turn,
                        item,
                        [i for i in items if i.interaction_id != item.interaction_id],
                        now,
                    )
            if items:
                pause["interactions_json"] = interactions.dump(items)
                pause["wait_kind"] = approval_runtime.wait_kind(
                    items, clients=bool(pause["calls"]), user_paused=False
                )
            parked = self.services.project_task_turn_repo.pause(
                conn,
                turn.turn_id,
                turn.worker_id,
                pause["checkpoint_id"],
                json.dumps({key: pause[key] for key in ("checkpoint_id", "calls", "groups")}),
                reply,
                reasoning,
                json.dumps(content.dump(), ensure_ascii=False),
                content.revision,
                now,
                wait_kind=pause["wait_kind"],
                interactions_json=pause["interactions_json"],
            )
            if parked:
                current = self.services.project_task_turn_repo.get_in_transaction(
                    conn, turn.turn_id
                )
                self.services.project_task_repo.wait(
                    conn,
                    turn.task_id,
                    turn.turn_id,
                    "user" if current and current.pause_requested else pause["wait_kind"],
                    now,
                )
            return parked
