"""Stateless, tool-free naming agent and its durable, bounded background worker."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.messages.ai import UsageMetadata
from octop_harness.llm.factory import ChatModelFactory

from octop.i18n import tr
from octop.infra.agents.providers.store import ProviderStore
from octop.infra.db.repos.project_task_titles import TaskTitleJob
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects.tasks import TaskDetail, detail, now_ms
from octop.infra.projects.title_policy import normalized_title
from octop.infra.users.resource_policy import assert_token_quota_available
from octop.infra.utils.llm_text import llm_text_content
from octop.infra.utils.locale import normalize_locale
from octop.infra.utils.ulid import new_ulid

logger = logging.getLogger(__name__)
_GENERATION_TIMEOUT = 30.0


def open_task(services: SharedServices, owner_id: int, task_id: str) -> TaskDetail:
    """Explicit user attention can admit one naming attempt, without replaying the task."""
    with services.db.transaction() as conn:
        tasks, titles = services.project_task_repo, services.project_task_title_repo
        if not tasks.lock_owner(conn, owner_id):
            raise OctopError.localized(ErrorCode.FORBIDDEN)
        if tasks.get_in_transaction(conn, task_id, owner_id) is None:
            raise OctopError.localized(ErrorCode.NOT_FOUND)
        state = titles.state(conn, task_id)
        if state is not None:
            # v36 preserved existing titles as manual. A later explicit rename
            # always advances title_revision, even if its text did not change.
            legacy_default = (
                state.source == "manual"
                and state.revision == 1
                and state.status is None
                and state.title == state.prompt[:80]
            )
            eligible = state.source == "provisional" or legacy_default
            if eligible and state.status is None:
                if legacy_default:
                    tasks.allow_legacy_title_generation(conn, task_id, owner_id, state.revision)
                titles.enqueue(conn, task_id, title_revision=state.revision, attempt_limit=1)
            elif eligible and state.status == "failed" and state.retry_at <= now_ms():
                titles.retry_on_open(conn, task_id, now_ms())
    return detail(services, owner_id, task_id)


@dataclass(frozen=True)
class TitleResult:
    title: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0


class TaskTitleAgent:
    """One isolated model request; never creates a harness, expert or workspace."""

    def __init__(self, services: SharedServices) -> None:
        self.services = services

    async def generate(self, job: TaskTitleJob) -> TitleResult:
        services = self.services
        await asyncio.to_thread(
            assert_token_quota_available,
            services.user_policy_repo,
            services.usage_repo,
            job.owner_id,
        )
        user = await asyncio.to_thread(services.user_repo.get, job.owner_id)
        if user is None or user.disabled:
            return TitleResult()
        store = ProviderStore(services.provider_repo)
        if not await asyncio.to_thread(store.is_model_ref_usable, job.model_ref):
            return TitleResult()
        providers = await asyncio.to_thread(store.build_harness_configs)
        factory = ChatModelFactory(providers)
        try:
            model = await asyncio.to_thread(factory.get_chat_model, job.model_ref)
            # Reasoning models include hidden reasoning in their output budget.
            limit = getattr(model, "_harness_max_output_tokens", 0) or 8_192
            result = await model.ainvoke(
                [
                    SystemMessage(tr("projects.title_prompt", normalize_locale(user.locale))),
                    HumanMessage(job.prompt[:4_000]),
                ],
                max_tokens=min(limit, 8_192),
            )
            usage: UsageMetadata | None = result.usage_metadata
            return TitleResult(
                None if result.tool_calls else normalized_title(llm_text_content(result)),
                usage["input_tokens"] if usage else 0,
                usage["output_tokens"] if usage else 0,
            )
        finally:
            await factory.aclose()


class TaskTitleWorker:
    """One naming request per process, with SQL leases across HTTP workers."""

    def __init__(self, services: SharedServices) -> None:
        self.services = services
        self.agent = TaskTitleAgent(services)
        self.worker_id = new_ulid()
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    def wake(self) -> None:
        self._wake.set()

    async def _execute(self, job: TaskTitleJob) -> None:
        result = TitleResult()
        try:
            result = await asyncio.wait_for(self.agent.generate(job), _GENERATION_TIMEOUT)
        except Exception as exc:
            # Never log the prompt, provider exception text or credentials.
            logger.warning("Task title generation failed: %s (%s)", job.task_id, type(exc).__name__)
        await asyncio.to_thread(
            self.services.project_task_title_repo.finish,
            job,
            result.title,
            result.input_tokens,
            result.output_tokens,
            now_ms(),
        )

    async def _loop(self) -> None:
        while True:
            self._wake.clear()
            try:
                job = await asyncio.to_thread(
                    self.services.project_task_title_repo.claim, self.worker_id, now_ms()
                )
                if job is not None:
                    await self._execute(job)
                    continue
            except Exception as exc:
                logger.warning("Task title worker failed (%s)", type(exc).__name__)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), 0.5)
