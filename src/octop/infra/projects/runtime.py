"""Small durable task runner. Each turn uses a fresh harness, never an expert.

Canonical messages and tool results are restored as context. Interrupted tool calls are
never automatically replayed. SQL leases keep multiple HTTP workers from claiming
the same turn and release abandoned runs after a process crash.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, message_to_dict, messages_from_dict
from octop_harness import HarnessAgent, HarnessAgentConfig
from octop_harness.mcp import aload_mcp_tools
from octop_harness.messages import extract_call_response
from octop_harness.request import ChatRequest

from octop.i18n import tr
from octop.infra.agents.middleware.reasoning import CONFIG_KEY, ReasoningRequestMiddleware
from octop.infra.agents.providers.store import ProviderStore
from octop.infra.agents.security.policy_store import SecuritySettingsStore
from octop.infra.connectors.builder import gateway_mcp_server_names, inject_missing_gateway_tools
from octop.infra.connectors.service import ConnectorService
from octop.infra.db.repos.project_task_turns import ProjectTaskTurnRow
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects.artifacts import SERVER_ARTIFACT_TOOLS, TaskArtifactPublisher
from octop.infra.projects.capabilities import plugin_capabilities, subagent_specs
from octop.infra.projects.client_tool_models import ClientToolDefinition
from octop.infra.projects.client_tools import ClientToolBroker
from octop.infra.projects.collaboration import TaskTeam, guard_subagents
from octop.infra.projects.context import capture_memory, close_pending_tools
from octop.infra.projects.models import ProjectFields, SkillBinding
from octop.infra.projects.service import _validate
from octop.infra.projects.tasks import now_ms
from octop.infra.projects.title import TaskTitleWorker
from octop.infra.skills.install import prepare_skillhub_package
from octop.infra.skills.skill_package_store import SkillPackageStore
from octop.infra.skills.skillhub_market import download_skillhub_package_files
from octop.infra.users.resource_policy import assert_token_quota_available
from octop.infra.utils.locale import normalize_locale
from octop.infra.utils.ulid import new_ulid

logger = logging.getLogger(__name__)


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
        result.append((skill.slug, files))
    return result


async def run_turn(
    services: SharedServices, owner_id: int, turn: ProjectTaskTurnRow, history: list[Any]
) -> AsyncIterator[dict[str, Any]]:
    """Yield only public text/tool-progress events; credentials stay in-process."""
    raw = json.loads(turn.configuration_json)
    fields = ProjectFields.model_validate(
        {key: raw[key] for key in ProjectFields.model_fields if key in raw}
    )
    try:
        await asyncio.to_thread(_validate, services, owner_id, fields)
        files = await asyncio.to_thread(_skill_files, services, fields)
        connectors = ConnectorService(
            repo=services.connector_repo,
            secret_repo=services.secret_repo,
            settings_repo=services.settings_repo,
            config=services.config,
        )
        # Only selected servers are loaded; no credentials or tool arguments are returned by this API.
        configs = await connectors.mcp_configs_for_user(owner_id) if fields.mcp_servers else {}
        if not set(fields.mcp_servers) <= configs.keys():
            raise ValueError("selected MCP unavailable")
        selected = {name: configs[name] for name in fields.mcp_servers}
        gateway_names = await asyncio.to_thread(
            gateway_mcp_server_names, connector_repo=services.connector_repo, user_id=owner_id
        )
        remote = {name: spec for name, spec in selected.items() if name not in gateway_names}
        tools = await aload_mcp_tools(remote) if remote else []
        if any(not any(t.name.startswith(name + "_") for t in tools) for name in remote):
            raise ValueError("MCP tools unavailable")
    except Exception as exc:
        raise OctopError.localized(ErrorCode.TASK_RESOURCE_UNAVAILABLE) from exc
    providers = ProviderStore(services.provider_repo)
    if not await asyncio.to_thread(providers.is_model_ref_usable, turn.model_ref):
        raise OctopError.localized(ErrorCode.TASK_MODEL_UNAVAILABLE)
    configs_provider = await asyncio.to_thread(providers.build_harness_configs)
    runtime_dir = services.paths.root / "project-tasks" / turn.task_id
    workspace = Path(fields.directory) if fields.directory else runtime_dir / "workspace"
    if fields.directory is None:
        await asyncio.to_thread(workspace.mkdir, parents=True, exist_ok=True)
    system_dir = runtime_dir / turn.turn_id
    try:
        system_prefix = system_dir.relative_to(workspace).as_posix()
    except ValueError:
        system_prefix = f".octop/tasks/{turn.task_id}/{turn.turn_id}"
    user = await asyncio.to_thread(services.user_repo.get, owner_id)
    locale = normalize_locale(user.locale if user else "zh")
    await asyncio.to_thread(
        assert_token_quota_available, services.user_policy_repo, services.usage_repo, owner_id
    )
    prompt = tr("projects.task_prompt", locale)
    if fields.instructions:
        prompt += "\n\n" + fields.instructions
    options = raw.get("execution_options", {})
    definitions = [
        ClientToolDefinition.model_validate(t) for t in options.get("client_tools") or []
    ]
    broker = ClientToolBroker(services, owner_id, turn, options.get("client_id") or "", definitions)
    client_tools = broker.tools()
    if ({tool.name for tool in client_tools} | SERVER_ARTIFACT_TOOLS) & {
        tool.name for tool in tools
    }:
        raise OctopError.localized(ErrorCode.TASK_RESOURCE_UNAVAILABLE)
    active_mcp = [*fields.mcp_servers, *(["client"] if definitions else [])]
    if definitions:
        prompt += "\n\n" + tr("projects.client_tools_prompt", locale)
    publisher = TaskArtifactPublisher(services, owner_id, turn, workspace, locale)
    prompt += "\n\n" + tr("projects.artifacts_prompt", locale)
    try:
        plugin_tools, plugin_middleware, plugin_skills = await asyncio.to_thread(
            plugin_capabilities,
            fields,
            reserved={t.name for t in [*tools, *client_tools]}
            | SERVER_ARTIFACT_TOOLS
            | {
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
            },
        )
        roles = await asyncio.to_thread(subagent_specs, fields, locale)
        files.extend(plugin_skills)
        if len({slug for slug, _ in files}) != len(files):
            raise ValueError("duplicate runtime skill name")
    except Exception as exc:
        raise OctopError.localized(ErrorCode.TASK_RESOURCE_UNAVAILABLE) from exc
    await asyncio.to_thread(runtime_dir.mkdir, parents=True, exist_ok=True)
    cfg = HarnessAgentConfig(
        name="project-task",
        language=locale,
        workspace_dir=str(workspace),
        system_files_path=system_prefix,
        backend={"type": "local_shell", "root_dir": str(workspace), "virtual_mode": True},
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
        team_enabled=fields.team_enabled,
        peer_invoke_mode="sync",
        middleware=[*plugin_middleware, ReasoningRequestMiddleware()],
        ask_user_enabled=False,
        media_offload_enabled=False,
        tools_disabled=frozenset(
            {
                "browser_use",
                "desktop_screenshot",
                "send_file_to_user",
                "read_env_file",
                "write_env_file",
            }
        ),
        web_search_tools=False,
        tools=[*publisher.tools(), *plugin_tools],
    )
    policy = await asyncio.to_thread(
        SecuritySettingsStore(settings_repo=services.settings_repo).load
    )
    cfg = policy.apply_to_config(cfg)
    guard_subagents(cfg)
    team = TaskTeam(owner_id)
    agent = await asyncio.to_thread(
        HarnessAgent,
        cfg,
        agent_id=turn.task_id,
        team_manager=team.manager if fields.team_enabled else None,
    )
    team.register(turn.task_id, agent)
    publisher.workspace = agent.workspace
    usage: dict[str, dict[str, Any]] = {}
    reply_text = ""
    completed = False

    async def prepare(agent: HarnessAgent) -> None:
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
        request.agent_id = agent_id
        request.skills = [slug for slug, _ in files]
        request.mcp_servers = active_mcp
        request.configurable = {
            **(request.configurable or {}),
            CONFIG_KEY: raw.get("reasoning_overrides", {}),
        }
        return request

    async def peer_finished(call: Any, thread_id: str, payload: dict[str, Any]) -> None:
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
            await team.load(
                HarnessAgent,
                cfg,
                roles,
                turn.task_id,
                runtime_dir,
                [*tools, *client_tools],
                prepare,
            )
        request = ChatRequest(
            messages=[*history, {"role": "user", "content": turn.prompt}],
            thread_id=turn.turn_id,
            agent_id=turn.task_id,
            user=str(owner_id),
            source="project-task",
            skills=[slug for slug, _ in files],
            mcp_servers=active_mcp,
            configurable={CONFIG_KEY: raw.get("reasoning_overrides", {})},
        )
        delegating = False
        async for event in agent.stream(request, protocol="langgraph"):
            if broker.failure:
                raise OctopError.localized(broker.failure)
            kind = event.get("type")
            if kind == "token":
                if not delegating:
                    content = str(event.get("content") or "")
                    reply_text += content
                    yield {"type": "token", "content": content}
            elif kind == "reasoning":
                if not delegating:
                    yield {"type": "reasoning", "content": str(event.get("content") or "")}
            elif kind == "tool_call_chunk" and event.get("name"):
                if event["name"] in {"task", "ask_agent"}:
                    delegating = True
                yield {"type": "activity", "name": str(event["name"])}
            elif kind == "tool_result":
                delegating = False
                yield {"type": "activity", "name": ""}
            elif kind == "state_update":
                for message in (event.get("data") or {}).get("messages", []):
                    if isinstance(message, AIMessage) and any(
                        call["name"] in {"task", "ask_agent"} for call in message.tool_calls
                    ):
                        delegating = True
            elif kind == "usage":
                usage[str(event.get("call_id") or "model")] = dict(event.get("usage") or {})
            elif kind == "hitl_required":
                # No approval-resume API in this slice; never claim the paused action completed.
                raise OctopError.localized(ErrorCode.TASK_EXECUTION_FAILED)
        if broker.failure:
            raise OctopError.localized(broker.failure)
        completed = True
    finally:
        try:
            messages = await agent.aget_history(turn.turn_id, limit=1_000_000)
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
        finally:
            try:
                await asyncio.to_thread(
                    services.project_task_turn_repo.record_usage,
                    turn.turn_id,
                    turn.worker_id,
                    sum(int(values.get("input_tokens") or 0) for values in usage.values()),
                    sum(int(values.get("output_tokens") or 0) for values in usage.values()),
                )
            finally:
                await team.close()


class ProjectTaskWorker:
    def __init__(self, services: SharedServices) -> None:
        self.services = services
        self.worker_id = new_ulid()
        self.closing = False
        self._wake = asyncio.Event()
        self._loop_task: asyncio.Task[None] | None = None
        self._current: asyncio.Task[None] | None = None
        self.title_worker = TaskTitleWorker(services)

    async def start(self) -> None:
        self._loop_task = asyncio.create_task(self._loop())
        await self.title_worker.start()

    def wake(self) -> None:
        self._wake.set()
        self.title_worker.wake()

    async def close(self) -> None:
        self.closing = True
        self._wake.set()
        await self.title_worker.close()
        if self._loop_task:
            await self._loop_task

    def _recover(self) -> None:
        before, now = now_ms() - 90_000, now_ms()
        for turn in self.services.project_task_turn_repo.stale(before):
            owner = self.services.project_task_repo.runtime_owner(turn.task_id)
            with self.services.db.transaction() as conn:
                if owner is not None:
                    self.services.project_task_repo.lock_owner(conn, owner)
                if self.services.project_task_turn_repo.interrupt(conn, turn.turn_id, before, now):
                    self.services.project_task_tool_call_repo.close_turn(
                        conn, turn.turn_id, "interrupted", "TASK_INTERRUPTED", now
                    )
                    self.services.project_task_repo.finish(conn, turn.task_id, turn.turn_id, now)

    async def _loop(self) -> None:
        while not self.closing:
            self._wake.clear()
            try:
                await asyncio.to_thread(self._recover)
                turn = await asyncio.to_thread(
                    self.services.project_task_turn_repo.claim, self.worker_id, now_ms()
                )
                if turn:
                    self._current = asyncio.create_task(self._execute(turn))
                    await self._current
                    self._current = None
                    continue
            except Exception:
                logger.exception("Project task worker iteration failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), 1.0)

    def _finish(
        self,
        turn: ProjectTaskTurnRow,
        status: str,
        reply: str,
        error: str,
        reasoning: str | None = None,
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
                self.worker_id,
                status,
                reply,
                error,
                now,
                reasoning=reasoning,
            ):
                final = self.services.project_task_turn_repo.get_in_transaction(conn, turn.turn_id)
                cancelled = final is not None and final.status == "cancelled"
                self.services.project_task_tool_call_repo.close_turn(
                    conn,
                    turn.turn_id,
                    "cancelled" if cancelled else "interrupted",
                    "" if cancelled else error,
                    now,
                )
                self.services.project_task_repo.finish(conn, turn.task_id, turn.turn_id, now)

    async def _execute(self, turn: ProjectTaskTurnRow) -> None:
        reply, reasoning, activity, status, error = "", "", "", "completed", ""

        async def heartbeat() -> None:
            while True:
                await asyncio.sleep(0.5)
                cancelled = await asyncio.to_thread(
                    self.services.project_task_turn_repo.progress,
                    turn.turn_id,
                    self.worker_id,
                    reply,
                    activity,
                    now_ms(),
                    reasoning=reasoning,
                )
                if cancelled or self.closing:
                    assert self._current is not None
                    self._current.cancel()
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
            context = await asyncio.to_thread(
                self.services.project_task_turn_repo.context, turn.task_id
            )
            history: list[Any] = messages_from_dict(json.loads(context[1])) if context else []
            for previous in rows:
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
                        {"role": "user", "content": previous.prompt},
                        {"role": "assistant", "content": reply_text},
                    ]
                )
            async for event in run_turn(self.services, task, turn, history):
                if event["type"] == "token":
                    reply += event["content"]
                elif event["type"] == "reasoning":
                    reasoning += event["content"]
                else:
                    activity = event["name"]
            if not reply.strip():
                raise OctopError.localized(ErrorCode.TASK_EXECUTION_FAILED)
        except asyncio.CancelledError:
            status, error = ("failed", "TASK_INTERRUPTED") if self.closing else ("cancelled", "")
        except OctopError as exc:
            status, error = "failed", str(exc.code)
        except Exception as exc:
            # Record exception class/status for diagnosis without logging credential-bearing payloads.
            logger.warning(
                "Project task turn failed: %s (%s, status=%s)",
                turn.turn_id,
                type(exc).__name__,
                getattr(exc, "status_code", None),
            )
            status, error = "failed", "TASK_EXECUTION_FAILED"
        finally:
            beat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beat
            await asyncio.to_thread(self._finish, turn, status, reply, error, reasoning)
