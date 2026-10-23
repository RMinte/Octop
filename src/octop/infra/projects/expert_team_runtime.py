"""Assemble persisted team members using only the task user's resources."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.errors import GraphBubbleUp
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command
from octop_harness import HarnessAgent, HarnessAgentConfig
from octop_harness.context_usage import extract_usage_from_message
from octop_harness.mcp import aload_mcp_tools
from octop_harness.messages import extract_call_response
from octop_harness.plugins import collect_plugin_tool_configs
from octop_harness.request import ChatRequest

from octop.i18n import tool_display_name, tr
from octop.infra.agents.middleware.reasoning import CONFIG_KEY, ReasoningRequestMiddleware
from octop.infra.agents.plugins.plugin_tool_names import extract_original_plugin_label
from octop.infra.connectors.builder import gateway_mcp_server_names, inject_missing_gateway_tools
from octop.infra.connectors.service import ConnectorService
from octop.infra.db.repos.project_task_turns import ProjectTaskTurnRow
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.knowledge.hint import KnowledgeSearchHintMiddleware
from octop.infra.knowledge.tools import build_knowledge_tools
from octop.infra.projects import workbench
from octop.infra.projects.capabilities import plugin_capabilities, subagent_specs
from octop.infra.projects.checkpoint_saver import checkpoint_fence
from octop.infra.projects.collaboration import SubagentCommandGate, guard_subagents
from octop.infra.projects.control import TaskControlMiddleware
from octop.infra.projects.expert_memory import ExpertMemory
from octop.infra.projects.expert_profiles import (
    ExpertAccessMiddleware,
    expert_configuration,
    profile_prompt,
    runtime_plugin_configuration,
)
from octop.infra.projects.expert_teams import member_configuration
from octop.infra.projects.file_paths import TaskFilePathMiddleware
from octop.infra.projects.image_middleware import TaskImageMaterializeMiddleware
from octop.infra.projects.models import ProjectFields
from octop.infra.projects.resource_snapshot import load_snapshot, save_snapshot
from octop.infra.projects.source_access import (
    SourceAccessMiddleware,
    restrict_external_configuration,
)
from octop.infra.projects.workbench_browser import TaskBrowserMiddleware, task_skill_files
from octop.infra.usage.model_calls import model_call_scope
from octop.infra.users.resource_policy import assert_token_quota_available

SkillFiles = list[tuple[str, list[tuple[str, bytes]]]]


class _MemberModelUsage(AgentMiddleware[Any, Any]):
    def __init__(self, runtime: ExpertTeamRuntime, profile: dict[str, Any]) -> None:
        self.runtime, self.profile = runtime, profile
        self.sequence = 0

    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]
    ) -> ModelResponse:
        runtime = self.runtime
        await asyncio.to_thread(
            assert_token_quota_available,
            runtime.services.user_policy_repo,
            runtime.services.usage_repo,
            runtime.owner_id,
        )
        with model_call_scope(
            runtime.services,
            user_id=runtime.owner_id,
            purpose="team_member",
            agent_id=str(self.profile["expert_id"]),
            project_id=runtime.project_id,
            task_id=runtime.turn.task_id,
            turn_id=runtime.turn.turn_id,
            submission_id=runtime.turn.request_id,
        ):
            response = await handler(request)
        for message in response.result:
            usage = extract_usage_from_message(message) if isinstance(message, AIMessage) else None
            if usage is not None:
                self.sequence += 1
                key = message.id or f"{self.profile['expert_id']}:model:{self.sequence}"
                runtime.usage_dict[str(key)] = usage
        return response


class _MemberToolEvents(AgentMiddleware[Any, Any]):
    def __init__(self, runtime: ExpertTeamRuntime, profile: dict[str, Any]) -> None:
        self.runtime, self.profile = runtime, profile

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        sink = self.runtime.event_sink
        if sink is None:
            return await handler(request)
        call = request.tool_call
        token = hashlib.sha256(str(self.profile["expert_id"]).encode()).hexdigest()[:20]
        source = {
            "call_id": f"member:{token}:{call['id']}",
            "source_call_id": call["id"],
            "name": call["name"],
            "speaker_agent_id": str(self.profile["expert_id"]),
            "speaker_name": str(self.profile["name"]),
        }
        started = time.monotonic()
        sink(
            {
                **source,
                "type": "tool_start",
                "arguments": call["args"],
                "label": tool_display_name(call["name"], self.runtime.root_cfg.language),
                "started_at": datetime.now(UTC).isoformat(),
            }
        )

        def finish(message: ToolMessage | None, *, failed: bool) -> None:
            sink(
                {
                    **source,
                    "type": "tool_end",
                    "failed": failed,
                    "output": message.content if message is not None else None,
                    "finished_at": datetime.now(UTC).isoformat(),
                    "duration_ms": max(0, (time.monotonic() - started) * 1000),
                }
            )

        try:
            result = await handler(request)
        except GraphBubbleUp:
            # The owning task records a pause; an interrupted member is not a
            # completed or failed execution receipt.
            raise
        except BaseException:
            finish(None, failed=True)
            raise
        message = result if isinstance(result, ToolMessage) else None
        if isinstance(result, Command) and isinstance(result.update, dict):
            messages = result.update.get("messages")
            message = next(
                (
                    item
                    for item in reversed(messages if isinstance(messages, list) else [])
                    if isinstance(item, ToolMessage) and item.tool_call_id == call["id"]
                ),
                None,
            )
        finish(message, failed=message is not None and message.status == "error")
        return result


@dataclass
class _Member:
    profile: dict[str, Any]
    raw: dict[str, Any]
    fields: ProjectFields
    files: SkillFiles
    mcp: dict[str, Any]
    tools: list[Any]
    plugin_tool_configs: dict[str, Any]
    memory: ExpertMemory
    evidence_path: Path
    evidence: dict[str, tuple[str, str]]
    flushed: bool = False


class ExpertTeamRuntime:
    def __init__(
        self,
        services: SharedServices,
        owner_id: int,
        turn: ProjectTaskTurnRow,
        root_raw: dict[str, Any],
        root_cfg: HarnessAgentConfig,
        *,
        common_tools: list[Any],
        skill_files_factory: Callable[[SharedServices, ProjectFields], SkillFiles],
        action_gate_factory: Callable[[str, str, set[str]], Any],
        usage_dict: dict[str, dict[str, Any]],
        event_sink: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.services, self.owner_id, self.turn = services, owner_id, turn
        self.root_raw, self.root_cfg = root_raw, root_cfg
        self.common_tools, self.skill_files_factory = common_tools, skill_files_factory
        self.action_gate_factory, self.usage_dict = action_gate_factory, usage_dict
        self.event_sink = event_sink
        self.project_id: str | None = None
        self.members: dict[str, _Member] = {}
        self._evidence_lock = asyncio.Lock()
        self.connectors = ConnectorService(
            repo=services.connector_repo,
            secret_repo=services.secret_repo,
            settings_repo=services.settings_repo,
            config=services.config,
        )

    async def configure(self, profile: dict[str, Any], ephemeral_id: str) -> HarnessAgentConfig:
        token = hashlib.sha256(str(profile["expert_id"]).encode()).hexdigest()[:20]
        snapshot_dir = (
            self.services.paths.root
            / "project-tasks"
            / self.turn.task_id
            / self.turn.turn_id
            / "resources"
            / "team"
            / token
        )
        raw = await asyncio.to_thread(
            member_configuration,
            self.services,
            self.owner_id,
            self.root_raw,
            profile,
            cached_skills=await asyncio.to_thread((snapshot_dir / "skills.json").exists),
        )
        raw = restrict_external_configuration(raw)
        profile = raw.get("expert_profile", profile)
        fields = ProjectFields.model_validate({key: raw[key] for key in ProjectFields.model_fields})
        task = await asyncio.to_thread(
            self.services.project_task_repo.get, self.turn.task_id, self.owner_id
        )
        self.project_id = task.project_id if task else None
        browser_scope = await asyncio.to_thread(
            workbench.resolve, self.services, self.owner_id, self.turn.task_id
        )
        configuration_json = json.dumps(raw, sort_keys=True)
        cached = await asyncio.to_thread(load_snapshot, snapshot_dir, configuration_json)
        if cached is None and self.turn.checkpoint_id:
            raise OctopError.localized(ErrorCode.TASK_RESOURCE_UNAVAILABLE)
        files = (
            cached
            if cached is not None
            else await asyncio.to_thread(self.skill_files_factory, self.services, fields)
        )
        all_mcp = (
            await self.connectors.mcp_configs_for_user(self.owner_id) if fields.mcp_servers else {}
        )
        if not set(fields.mcp_servers) <= all_mcp.keys():
            raise OctopError.localized(ErrorCode.TASK_RESOURCE_UNAVAILABLE)
        selected_mcp = {name: all_mcp[name] for name in fields.mcp_servers}
        gateway = await asyncio.to_thread(
            gateway_mcp_server_names,
            connector_repo=self.services.connector_repo,
            user_id=self.owner_id,
        )
        remote = {name: config for name, config in selected_mcp.items() if name not in gateway}
        mcp_tools = await aload_mcp_tools(remote) if remote else []
        if any(not any(tool.name.startswith(name + "_") for tool in mcp_tools) for name in remote):
            raise OctopError.localized(ErrorCode.TASK_RESOURCE_UNAVAILABLE)
        plugin_settings = await asyncio.to_thread(
            runtime_plugin_configuration, self.services, self.owner_id, profile
        )
        denied = set(profile.get("capabilities", {}).get("tools_disabled", []))
        plugin_tools, plugin_middleware, plugin_skills = await asyncio.to_thread(
            plugin_capabilities,
            fields,
            reserved={tool.name for tool in [*self.common_tools, *mcp_tools]}
            | {
                "task",
                "ask_agent",
                "agent_list",
                "read_file",
                "write_file",
                "edit_file",
                "execute",
                "ls",
                "glob",
                "grep",
                "search_knowledge",
                "expert_memory_search",
                "expert_memory_get",
            },
            include_skills=cached is None,
            agent_plugins=plugin_settings,
        )
        plugin_tools = [
            tool
            for tool in plugin_tools
            if tool.name not in denied
            and extract_original_plugin_label(tool.description or "") not in denied
        ]
        if cached is None:
            files.extend(plugin_skills)
            if raw.get("execution_options", {}).get("skills") is None:
                for skill in profile.get("workspace_skills", []):
                    entries = []
                    for item in skill["files"]:
                        data = base64.b64decode(item["data"], validate=True)
                        if hashlib.sha256(data).hexdigest() != item["sha256"]:
                            raise OctopError.localized(ErrorCode.TASK_RESOURCE_UNAVAILABLE)
                        entries.append((item["path"], data))
                    files.append((skill["slug"], entries))
            if len({slug for slug, _entries in files}) != len(files):
                raise ValueError("duplicate runtime skill name")
            applied = await task_skill_files(browser_scope)
            applied_names = {slug for slug, _entries in applied}
            files = [(slug, entries) for slug, entries in files if slug not in applied_names]
            files.extend(applied)
            await asyncio.to_thread(save_snapshot, snapshot_dir, configuration_json, files)
        memory = ExpertMemory(
            self.services,
            self.owner_id,
            self.turn.task_id,
            self.turn.turn_id,
            self.project_id,
            profile,
            enabled=raw.get("expert_memory_enabled"),
            worker_id=self.turn.worker_id,
        )
        locale = self.root_cfg.language
        prompt = tr("projects.task_prompt", locale) + "\n\n" + profile_prompt(profile, locale)
        prompt += "\n\n" + tr("projects.task_file_paths", locale)
        if fields.instructions:
            prompt += "\n\n" + fields.instructions
        recalled = await memory.prompt(self.turn.prompt)
        if recalled:
            prompt += "\n\n" + tr("projects.expert_memory_prompt", locale, content=recalled)
        prompt += "\n\n" + tr(
            "projects.task_environment_prompt",
            locale,
            environment=json.dumps(
                {
                    "server_directory": str(self.root_cfg.workspace_dir),
                    "client_directory": fields.client_directory,
                },
                ensure_ascii=False,
            ),
        )
        readonly = set()
        for tool in mcp_tools:
            metadata = getattr(tool, "metadata", None) or {}
            annotations = metadata.get("annotations") or metadata
            if isinstance(annotations, dict) and annotations.get("readOnlyHint") is True:
                readonly.add(tool.name)
        gate = self.action_gate_factory(str(raw.get("action_mode") or "craft"), locale, readonly)
        tools = [
            *self.common_tools,
            *build_knowledge_tools(self.services),
            *plugin_tools,
            *memory.tools(locale),
        ]
        tools = [tool for tool in tools if gate.allows(tool.name)]
        cfg = replace(
            self.root_cfg,
            name=str(profile["name"]),
            system_prompt=prompt,
            system_files_path=f"{self.root_cfg.system_files_path}/team/{token}",
            default_model=self.turn.model_ref,
            mcp_server_configs={name: {} for name in selected_mcp},
            memory=(),
            memory_enabled=False,
            memory_capture_enabled=False,
            checkpointer=False,
            tools=tools,
            tools_disabled=frozenset(
                {
                    *({"browser_use"} if not browser_scope.view.capabilities.browser else set()),
                    "desktop_screenshot",
                    "send_file_to_user",
                    "read_env_file",
                    "write_env_file",
                    "execute",
                    "ask_user",
                    "ask_agent",
                    "agent_list",
                    *denied,
                    *({"write_file", "edit_file"} if gate.mode != "craft" else set()),
                }
            ),
            team_enabled=False,
            subagents=await asyncio.to_thread(subagent_specs, fields, locale),
            interrupt_on={},
            web_search_tools=raw.get("web_search_enabled", True),
            middleware=[
                *(
                    [
                        SourceAccessMiddleware(
                            self.services, self.owner_id, self.turn.task_id, raw["source_origin"]
                        )
                    ]
                    if raw.get("source_origin")
                    else []
                ),
                TaskFilePathMiddleware(self.root_cfg.workspace_dir, locale),
                _MemberToolEvents(self, profile),
                SubagentCommandGate(locale),
                ExpertAccessMiddleware(self.services, self.owner_id, profile),
                gate,
                _MemberModelUsage(self, profile),
                *plugin_middleware,
                TaskControlMiddleware(
                    self.services, self.owner_id, self.turn, locale, readonly_tools=readonly
                ),
                ReasoningRequestMiddleware(
                    summary=raw.get("reasoning_mode") not in {None, "disabled"}
                ),
                KnowledgeSearchHintMiddleware(),
                TaskImageMaterializeMiddleware(self.services, self.owner_id, self.turn.task_id),
                TaskBrowserMiddleware(
                    self.services,
                    self.owner_id,
                    self.turn.task_id,
                    browser_scope.view.scope_revision,
                ),
            ],
        )
        guard_subagents(cfg)
        evidence_path = snapshot_dir.parent.parent.parent / "team-evidence" / f"{token}.json"
        evidence = await asyncio.to_thread(
            self._read_evidence, evidence_path, str(profile["expert_id"])
        )
        self.members[ephemeral_id] = _Member(
            profile,
            raw,
            fields,
            files,
            selected_mcp,
            mcp_tools,
            collect_plugin_tool_configs(plugin_settings),
            memory,
            evidence_path,
            evidence,
        )
        return cfg

    async def prepare(self, agent: HarnessAgent, profile: dict[str, Any]) -> None:
        member = next(
            value
            for value in self.members.values()
            if value.profile["expert_id"] == profile["expert_id"]
        )
        for slug, entries in member.files:
            for path, data in entries:
                await agent.workspace.aupload_bytes(
                    agent.workspace.system_rel(f"skills/{slug}/{path}"), data
                )
        if member.tools:
            agent.inject_mcp_tools(member.tools)
        await asyncio.to_thread(
            inject_missing_gateway_tools,
            agent,
            svc=self.connectors,
            connector_repo=self.services.connector_repo,
            user_id=self.owner_id,
            agent_id=self.turn.task_id,
            mcp_server_configs=member.mcp,
        )

    async def enrich(self, agent_id: str, request: ChatRequest) -> ChatRequest:
        member = self.members[agent_id]
        await asyncio.to_thread(
            expert_configuration, self.services, self.owner_id, str(member.profile["expert_id"])
        )
        user = await asyncio.to_thread(self.services.user_repo.get, self.owner_id)
        request.agent_id, request.user = agent_id, str(self.owner_id)
        request.skills = [slug for slug, _ in member.files]
        request.mcp_servers = list(member.mcp)
        request.configurable = {
            **(request.configurable or {}),
            "user": str(self.owner_id),
            "user_is_admin": bool(user and user.role == "admin"),
            "locale": self.root_cfg.language,
            "knowledge_base_ids": member.raw["knowledge_base_ids"],
            "knowledge_base_catalog": member.raw["knowledge_base_catalog"],
            CONFIG_KEY: member.raw.get("reasoning_overrides", {}),
            "plugin_tool_configs": member.plugin_tool_configs,
        }
        return request

    async def finished(self, call: Any, thread_id: str, payload: dict[str, Any]) -> None:
        member = self.members.get(str(call.to_agent_id))
        if member is not None:
            prompt, reply = str(call.message), extract_call_response(payload)
            key = hashlib.sha256(
                json.dumps([thread_id, prompt, reply], ensure_ascii=False).encode()
            ).hexdigest()
            async with self._evidence_lock:
                member.evidence.setdefault(key, (prompt, reply))
                await asyncio.to_thread(self._write_evidence, member)

    def _read_evidence(self, path: Path, expert_id: str) -> dict[str, tuple[str, str]]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        if payload.get("identity") != [
            self.owner_id,
            self.turn.task_id,
            self.turn.turn_id,
            expert_id,
        ]:
            raise OctopError.localized(ErrorCode.TASK_RESOURCE_UNAVAILABLE)
        evidence = payload.get("evidence")
        if not isinstance(evidence, dict) or any(
            not isinstance(pair, list)
            or len(pair) != 2
            or any(not isinstance(text, str) for text in pair)
            for pair in evidence.values()
        ):
            raise OctopError.localized(ErrorCode.TASK_RESOURCE_UNAVAILABLE)
        return {key: (pair[0], pair[1]) for key, pair in evidence.items()}

    def _write_evidence(self, member: _Member) -> None:
        with checkpoint_fence(self.services, self.turn):
            current = self.services.project_task_turn_repo.get(self.turn.turn_id)
            if (
                current is None
                or current.status != "running"
                or current.worker_id != self.turn.worker_id
                or current.cancel_requested
            ):
                raise OctopError.localized(ErrorCode.TASK_INTERRUPTED)
            expert_configuration(self.services, self.owner_id, str(member.profile["expert_id"]))
            previous = self._read_evidence(member.evidence_path, str(member.profile["expert_id"]))
            member.evidence = {**previous, **member.evidence}
            member.evidence_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = member.evidence_path.with_suffix(".tmp")
            with temporary.open("w", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(
                        {
                            "identity": [
                                self.owner_id,
                                self.turn.task_id,
                                self.turn.turn_id,
                                member.profile["expert_id"],
                            ],
                            "evidence": member.evidence,
                        },
                        ensure_ascii=False,
                    )
                )
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(member.evidence_path)

    async def flush(self) -> None:
        """Capture each member once, only after the owning turn completes."""
        for member in self.members.values():
            if member.evidence and not member.flushed:
                pairs = list(member.evidence.values())
                await member.memory.capture(
                    "\n\n".join(pair[0] for pair in pairs),
                    "\n\n".join(pair[1] for pair in pairs),
                    source_kind="delegation",
                )
                member.flushed = True
