"""Small durable task runner. Each turn uses a fresh harness, never an expert.

Completed conversation text is replayed as context. Interrupted tool calls are
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

from octop_harness import HarnessAgent, HarnessAgentConfig
from octop_harness.mcp import aload_mcp_tools
from octop_harness.request import ChatRequest

from octop.i18n import tr
from octop.infra.agents.providers.store import ProviderStore
from octop.infra.agents.security.policy_store import SecuritySettingsStore
from octop.infra.connectors.builder import gateway_mcp_server_names, inject_missing_gateway_tools
from octop.infra.connectors.service import ConnectorService
from octop.infra.db.repos.project_task_turns import ProjectTaskTurnRow
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects.models import ProjectFields, SkillBinding
from octop.infra.projects.service import _validate
from octop.infra.projects.tasks import now_ms
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
    services: SharedServices, owner_id: int, turn: ProjectTaskTurnRow, history: list[dict[str, Any]]
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
        mcp_server_configs={name: {} for name in selected},
        memory=(),
        memory_enabled=False,
        model_retry_enabled=False,
        memory_jsonl_enabled=False,
        session_log_enabled=False,
        bootstrap_enabled=False,
        subagents=[],
        subagents_auto_load=False,
        team_enabled=False,
        ask_user_enabled=False,
        media_offload_enabled=False,
        tools_disabled=frozenset(
            {
                "task",
                "browser_use",
                "desktop_screenshot",
                "send_file_to_user",
                "read_env_file",
                "write_env_file",
            }
        ),
        web_search_tools=False,
    )
    policy = await asyncio.to_thread(
        SecuritySettingsStore(settings_repo=services.settings_repo).load
    )
    cfg = policy.apply_to_config(cfg)
    agent = await asyncio.to_thread(HarnessAgent, cfg, agent_id=turn.task_id)
    usage: dict[str, dict[str, Any]] = {}
    try:
        for slug, entries in files:
            for name, data in entries:
                await agent.workspace.aupload_bytes(
                    agent.workspace.system_rel(f"skills/{slug}/{name}"), data
                )
        if tools:
            agent.inject_mcp_tools(tools)
        await asyncio.to_thread(
            inject_missing_gateway_tools,
            agent,
            svc=connectors,
            connector_repo=services.connector_repo,
            user_id=owner_id,
            agent_id=turn.task_id,
            mcp_server_configs=selected,
        )
        request = ChatRequest(
            messages=[*history, {"role": "user", "content": turn.prompt}],
            thread_id=turn.turn_id,
            user=str(owner_id),
            source="project-task",
            skills=[slug for slug, _ in files],
            mcp_servers=fields.mcp_servers,
        )
        async for event in agent.stream(request, protocol="langgraph"):
            kind = event.get("type")
            if kind == "token":
                yield {"type": "token", "content": str(event.get("content") or "")}
            elif kind == "tool_call_chunk" and event.get("name"):
                yield {"type": "activity", "name": str(event["name"])}
            elif kind == "tool_result":
                yield {"type": "activity", "name": ""}
            elif kind == "usage":
                usage[str(event.get("call_id") or "model")] = dict(event.get("usage") or {})
            elif kind == "hitl_required":
                # No approval-resume API in this slice; never claim the paused action completed.
                raise OctopError.localized(ErrorCode.TASK_EXECUTION_FAILED)
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
            await agent.aclose()


class ProjectTaskWorker:
    def __init__(self, services: SharedServices) -> None:
        self.services = services
        self.worker_id = new_ulid()
        self.closing = False
        self._wake = asyncio.Event()
        self._loop_task: asyncio.Task[None] | None = None
        self._current: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._loop_task = asyncio.create_task(self._loop())

    def wake(self) -> None:
        self._wake.set()

    async def close(self) -> None:
        self.closing = True
        self._wake.set()
        if self._loop_task:
            await self._loop_task

    def _recover(self) -> None:
        before, now = now_ms() - 90_000, now_ms()
        for turn in self.services.project_task_turn_repo.stale(before):
            with self.services.db.transaction() as conn:
                if self.services.project_task_turn_repo.interrupt(conn, turn.turn_id, before, now):
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

    def _finish(self, turn: ProjectTaskTurnRow, status: str, reply: str, error: str) -> None:
        now = now_ms()
        with self.services.db.transaction() as conn:
            if self.services.project_task_turn_repo.finish(
                conn, turn.turn_id, self.worker_id, status, reply, error, now
            ):
                self.services.project_task_repo.finish(conn, turn.task_id, turn.turn_id, now)

    async def _execute(self, turn: ProjectTaskTurnRow) -> None:
        reply, activity, status, error = "", "", "completed", ""

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
            history: list[dict[str, Any]] = []
            for previous in rows:
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
            await asyncio.to_thread(self._finish, turn, status, reply, error)
