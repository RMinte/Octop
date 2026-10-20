"""Run explicit task compaction under the existing turn lease, without task tools."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_core.messages import RemoveMessage, convert_to_messages, message_to_dict
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from octop_harness import HarnessAgent, HarnessAgentConfig
from octop_harness.compaction import display_offload_path, force_compact_thread

from octop.i18n import tr
from octop.infra.agents.providers.store import ProviderStore
from octop.infra.db.repos.project_task_turns import ProjectTaskTurnRow
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects.checkpoint_saver import TaskCheckpointSaver
from octop.infra.projects.context import close_pending_tools, model_context, task_backend
from octop.infra.projects.stream import TASK_PROTOCOL
from octop.infra.projects.usage import compact_usage, latest_usage, save_usage, stamp_usage
from octop.infra.users.resource_policy import assert_token_quota_available
from octop.infra.utils.locale import normalize_locale

logger = logging.getLogger(__name__)


class _CompactionGraph:
    """Name the writer explicitly for a fresh graph that has never run a node."""

    def __init__(self, graph: Any) -> None:
        self.graph = graph

    async def aget_state(self, config: Any) -> Any:
        return await self.graph.aget_state(config)

    async def aupdate_state(self, config: Any, values: Any) -> Any:
        return await self.graph.aupdate_state(config, values, as_node="model")


async def run_compaction(
    services: SharedServices,
    owner_id: int,
    turn: ProjectTaskTurnRow,
    history: list[Any],
    *,
    factory: Callable[..., HarnessAgent],
) -> AsyncIterator[dict[str, Any]]:
    raw = json.loads(turn.configuration_json)
    providers = ProviderStore(services.provider_repo)
    if not await asyncio.to_thread(providers.is_model_ref_usable, turn.model_ref):
        raise OctopError.localized(ErrorCode.TASK_MODEL_UNAVAILABLE)
    await asyncio.to_thread(
        assert_token_quota_available, services.user_policy_repo, services.usage_repo, owner_id
    )
    user = await asyncio.to_thread(services.user_repo.get, owner_id)
    locale = normalize_locale(user.locale if user else "zh")
    runtime_dir = services.paths.root / "project-tasks" / turn.task_id
    workspace = Path(raw["directory"]) if raw.get("directory") else runtime_dir / "workspace"
    if not raw.get("directory"):
        await asyncio.to_thread(workspace.mkdir, parents=True, exist_ok=True)
    try:
        system_prefix = (runtime_dir / turn.turn_id).relative_to(workspace).as_posix()
    except ValueError:
        system_prefix = f".octop/tasks/{turn.task_id}/{turn.turn_id}"
    cfg = HarnessAgentConfig(
        name="project-task-compact",
        language=locale,
        workspace_dir=str(workspace),
        system_files_path=system_prefix,
        backend=task_backend(workspace, runtime_dir),
        default_model=turn.model_ref,
        providers=await asyncio.to_thread(providers.build_harness_configs),
        default_timezone=services.config.default_timezone,
        memory=(),
        memory_enabled=False,
        model_retry_enabled=False,
        session_log_enabled=False,
        bootstrap_enabled=False,
        subagents_auto_load=False,
        web_search_tools=False,
        protocol=TASK_PROTOCOL,
    )
    agent = await asyncio.to_thread(factory, cfg, agent_id=turn.task_id)
    usage = UsageMetadataCallbackHandler()
    try:
        if agent.checkpointer:
            saver = TaskCheckpointSaver(services, owner_id, turn, agent.checkpointer)
            agent._checkpointer_instance = saver
            agent._base_graph.checkpointer = saver
            agent.graph.checkpointer = saver
            cfg.checkpointer = saver
        reply = tr("projects.compact_nothing", locale)
        snapshot = latest_usage(convert_to_messages(history))
        if history:
            # A recovered maintenance round starts from the durable task context,
            # rather than merging it with a partially compacted checkpoint.
            await agent.protocol._graph.aupdate_state(
                {"configurable": {"thread_id": turn.turn_id}},
                {
                    "messages": [
                        RemoveMessage(id=REMOVE_ALL_MESSAGES),
                        *convert_to_messages(history),
                    ],
                    "_summarization_event": None,
                    "_summarization_session_id": turn.task_id,
                },
                as_node="model",
            )
            summary_model = await asyncio.to_thread(
                agent._model_factory.get_chat_model, turn.model_ref
            )
            summarizer = agent._summarization_mw
            assert summarizer is not None
            result = await force_compact_thread(
                graph=_CompactionGraph(agent.graph),
                backend=summarizer._backend,
                model=summary_model.with_config(callbacks=[usage]),
                thread_id=turn.turn_id,
                model_ref=turn.model_ref,
                summarization_middleware=summarizer,
            )
            if result.ok:
                snapshot = compact_usage(snapshot, result.removed_tokens)
                path = result.display_path or display_offload_path(result.file_path)
                reply = tr("projects.compact_done", locale, count=result.summarized_count)
                if path:
                    reply += "\n\n" + tr("projects.compact_history", locale, path=path)
            elif result.reason != "nothing_to_compact":
                logger.warning(
                    "Task compaction failed task=%s reason=%s", turn.task_id, result.reason
                )
                raise OctopError.localized(ErrorCode.TASK_EXECUTION_FAILED)
        # Maintenance commands stay in the public journal, outside model context
        # and long-term memory. Saving only on success leaves failed attempts inert.
        messages = await model_context(agent, turn.turn_id) if history else []
        messages = stamp_usage(messages, snapshot)
        # Publish context and its snapshot under one owner/lease/cancellation
        # check; a cancellation cannot land between their two publications.
        await asyncio.to_thread(
            save_usage,
            services,
            owner_id,
            turn,
            snapshot,
            context_messages=json.dumps(
                [message_to_dict(m) for m in close_pending_tools(messages, locale)]
            ),
        )
        yield {"type": "token", "content": reply}
    finally:
        try:
            await asyncio.to_thread(
                services.project_task_turn_repo.record_usage,
                turn.turn_id,
                sum(item["input_tokens"] for item in usage.usage_metadata.values()),
                sum(item["output_tokens"] for item in usage.usage_metadata.values()),
            )
        finally:
            await agent.aclose()
