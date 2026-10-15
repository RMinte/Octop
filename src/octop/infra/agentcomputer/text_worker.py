"""Single-process, loopback-only executor for opt-in text Runs.

The durable command and dispatch journals remain authoritative. A worker may
redrive only an accepted Run that has never been claimed; progressed Runs are
fenced by daemon recovery and are never replayed here.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from octop_harness import HarnessAgent, HarnessAgentConfig
from octop_harness.llm.factory import ChatModelFactory

from octop.infra.agentcomputer.bootstrap import settle_task, settled_to_thread
from octop.infra.agentcomputer.capabilities import text_task_available
from octop.infra.agentcomputer.commands import CommandAdmissionError, StorageDataError
from octop.infra.agentcomputer.dispatch import claim_run, dispatch_run
from octop.infra.agentcomputer.engine import EngineEntryMiddleware, invoke_engine_entry
from octop.infra.agentcomputer.identity import read_identity
from octop.infra.agentcomputer.models import EngineInvocation, execution_configuration
from octop.infra.agentcomputer.project_document_tool import read_project_document
from octop.infra.agentcomputer.runs import get_run

if TYPE_CHECKING:
    from octop.infra.agentcomputer.active_runs import ActiveRunRegistry
    from octop.infra.agents.manager import AgentManager
    from octop.infra.db.services import SharedServices
    from octop.infra.history.service import HistoryArchive

logger = logging.getLogger(__name__)


class LocalTextTaskWorker:
    """Own queued text Runs until shutdown, with at most one model loop at a time."""

    def __init__(
        self,
        services: SharedServices,
        registry: AgentManager,
        history_archive: HistoryArchive,
        active_runs: ActiveRunRegistry,
    ) -> None:
        self._services = services
        self._registry = registry
        self._history_archive = history_archive
        self._active_runs = active_runs
        self._tasks: dict[tuple[int, UUID], asyncio.Task[None]] = {}
        self._serial = asyncio.Semaphore(1)
        self._sweeper: asyncio.Task[None] | None = None
        self._scan_after_pk = 0
        self._closing = False

    async def start(self) -> None:
        """Redrive only durable never-dispatched Runs after daemon recovery."""
        await self.scan_queued()
        self._sweeper = asyncio.create_task(self._poll(), name="ac-text-queue-sweep")

    async def _poll(self) -> None:
        while not self._closing:
            await asyncio.sleep(10)
            try:
                await self.scan_queued()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("AgentComputer queued text Run scan failed")

    async def scan_queued(self) -> None:
        """Retry eligible accepted work after a provider becomes available again."""
        if not await settled_to_thread(text_task_available, self._services):
            return
        active = set(self._tasks)
        slots = max(0, 16 - len(active))
        if slots == 0:
            return

        def candidates() -> tuple[list[tuple[int, UUID]], int]:
            result: list[tuple[int, UUID]] = []
            with self._services.db.connect() as conn:
                through_pk = self._services.ac_run_repo.queued_high_watermark(conn)
            if through_pk == 0:
                return result, 0
            identity = read_identity(self._services.paths.agentcomputer_identity)
            after_pk = self._scan_after_pk
            wrapped = False
            while len(result) < slots:
                with self._services.db.connect() as conn:
                    batch = self._services.ac_run_repo.list_queued_candidates(
                        conn, str(identity.boxID), after_pk=after_pk, through_pk=through_pk
                    )
                if not batch:
                    if after_pk == 0 or wrapped:
                        break
                    after_pk = 0
                    wrapped = True
                    continue
                for row in batch:
                    key = (row.owner_id, UUID(row.run_id))
                    if key not in active:
                        result.append(key)
                    after_pk = row.pk
                    if len(result) == slots:
                        break
                if after_pk >= through_pk:
                    after_pk = 0
                    break
            return result, after_pk

        queued, self._scan_after_pk = await settled_to_thread(candidates)
        for owner_id, run_id in queued:
            self.schedule(owner_id=owner_id, run_id=run_id)

    def schedule(self, *, owner_id: int, run_id: UUID) -> bool:
        """Queue a previously accepted Run; duplicate calls are harmless."""
        if self._closing:
            return False
        key = (owner_id, run_id)
        if key in self._tasks:
            return True
        task = asyncio.create_task(self._run(owner_id, run_id), name=f"ac-text-run-{run_id}")
        self._tasks[key] = task
        task.add_done_callback(
            lambda done: self._tasks.pop(key, None) if self._tasks.get(key) is done else None
        )
        return True

    async def _run(self, owner_id: int, run_id: UUID) -> None:
        async with self._serial:
            if self._closing:
                return
            try:
                view = await settled_to_thread(
                    get_run, self._services, owner_id=owner_id, run_id=run_id
                )
                if view is None or view.state != "queued" or view.dispatchPhase != "notDispatched":
                    return
                claim = await settled_to_thread(
                    claim_run,
                    self._services,
                    owner_id=owner_id,
                    run_id=run_id,
                    claimant_id=uuid4(),
                    expected_revision=view.revision,
                )
                await dispatch_run(
                    self._services,
                    claim=claim,
                    invoke=self._invoke,
                    active_runs=self._active_runs,
                )
            except CommandAdmissionError as exc:
                if exc.code not in {"revisionConflict", "runtimeUnavailable"}:
                    logger.warning("AgentComputer text Run %s rejected: %s", run_id, exc.code)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("AgentComputer text Run %s failed; inspect persisted Run", run_id)

    def _managed_config(self, invocation: EngineInvocation) -> dict[str, Any]:
        snapshot = invocation.intent.snapshot
        if invocation.intent.projectID is None:
            self._registry.workspace_for_task_preparation(
                snapshot.agentID, str(invocation.intent.taskID)
            )
        else:
            self._registry.workspace_for_project_preparation(
                snapshot.agentID, str(invocation.intent.projectID)
            )
        row = self._services.agent_repo.get(snapshot.agentID)
        if row is None or row.config_json is None:
            raise StorageDataError("missing managed agent configuration")
        saved = json.loads(row.config_json)
        if not isinstance(saved, dict) or set(saved) != {
            "backend",
            "workspace_dir",
            "system_files_path",
        }:
            raise StorageDataError("invalid managed agent configuration")
        return saved

    async def _invoke(self, invocation: EngineInvocation) -> None:
        snapshot = invocation.intent.snapshot
        configuration = execution_configuration(snapshot)
        if not await asyncio.to_thread(
            text_task_available,
            self._services,
            model_ref=configuration.modelRef,
        ):
            raise CommandAdmissionError("runtimeUnavailable", reason="executionUnavailable")
        saved = await settled_to_thread(self._managed_config, invocation)
        checkpoint_dir = self._services.paths.root / "agentcomputer-checkpoints"
        await settled_to_thread(checkpoint_dir.mkdir, mode=0o700, parents=True, exist_ok=True)
        checkpoint_name = (
            f"task-{invocation.intent.taskID}"
            if invocation.intent.projectID is None
            else str(invocation.intent.projectID)
        )
        checkpoint_path = checkpoint_dir / f"{checkpoint_name}.sqlite"
        async with AsyncSqliteSaver.from_conn_string(str(checkpoint_path)) as saver:
            providers = await settled_to_thread(self._registry.providers.build_harness_configs)
            factory = ChatModelFactory(providers)
            config = HarnessAgentConfig(
                name="agentcomputer-text",
                workspace_dir=saved["workspace_dir"],
                system_files_path=saved["system_files_path"],
                backend=saved["backend"],
                default_model=configuration.modelRef,
                system_prompt=configuration.instructions,
                checkpointer=saver,
                middleware=[
                    EngineEntryMiddleware(
                        self._services,
                        invocation,
                        self._history_archive,
                        registry=self._registry
                        if invocation.intent.projectID is not None
                        else None,
                    )
                ],
                tools=[read_project_document] if invocation.intent.projectID is not None else [],
                tools_disabled=frozenset(
                    {
                        "ls",
                        "read_file",
                        "write_file",
                        "edit_file",
                        "glob",
                        "grep",
                        "execute",
                        "task",
                        "current_time",
                        "web_fetch",
                        "browser_use",
                        "desktop_screenshot",
                        "send_file_to_user",
                        "read_env_file",
                        "write_env_file",
                    }
                ),
                bootstrap_enabled=False,
                memory_enabled=False,
                memory_jsonl_enabled=False,
                memory=(),
                session_log_enabled=False,
                subagents=[],
                subagents_auto_load=False,
                todos_enabled=False,
                team_enabled=False,
                ask_user_enabled=False,
                media_offload_enabled=False,
                model_retry_enabled=False,
                web_search_tools=False,
                mcp_server_configs={},
                acp_delegate_enabled=False,
            )
            agent = await settled_to_thread(
                HarnessAgent,
                config,
                agent_id=snapshot.agentID,
                model_factory=factory,
            )
            try:
                await invoke_engine_entry(
                    agent,
                    invocation,
                    active_runs=self._active_runs,
                    history_archive=self._history_archive,
                )
            finally:
                _, cancelled = await settle_task(asyncio.create_task(agent.aclose()))
                if cancelled:
                    raise asyncio.CancelledError

    async def close(self) -> None:
        self._closing = True
        tasks = tuple(self._tasks.values()) + (
            (self._sweeper,) if self._sweeper is not None else ()
        )
        for task in tasks:
            task.cancel()
        if tasks:

            async def join() -> None:
                await asyncio.gather(*tasks, return_exceptions=True)

            _, cancelled = await settle_task(asyncio.create_task(join()))
            if cancelled:
                raise asyncio.CancelledError
