"""An isolated, tool-free approval reviewer and its durable one-attempt worker."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass
from typing import Literal, cast

from langchain_core.messages import HumanMessage, SystemMessage

from octop.i18n import tr
from octop.infra.agents.providers.box import OctopChatModelFactory as ChatModelFactory
from octop.infra.agents.providers.store import ProviderStore
from octop.infra.db.repos.project_task_reviews import TaskReviewRow
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects.tasks import now_ms
from octop.infra.usage.model_calls import model_call_scope
from octop.infra.users.resource_policy import assert_token_quota_available
from octop.infra.utils.locale import normalize_locale
from octop.infra.utils.ulid import new_ulid

logger = logging.getLogger(__name__)
_GENERATION_TIMEOUT = 45.0
_MAX_PROPOSAL_BYTES = 32_768
_MAX_RESPONSE_BYTES = 4_096
_DECISIONS = {"allow_once", "deny", "requires_user"}
ReviewDecision = Literal["allow_once", "deny", "requires_user"]


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate review response key")
        result[key] = value
    return result


def _response_text(content: object) -> str | None:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") in ("thinking", "reasoning"):
            continue
        elif (
            isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        ):
            parts.append(block["text"])
        else:
            return None
    return "".join(parts)


@dataclass(frozen=True)
class ApprovalReviewResult:
    decision: ReviewDecision = "requires_user"
    reason_code: str = "review_unavailable"
    input_tokens: int = 0
    output_tokens: int = 0


class TaskApprovalReviewer:
    """Review only fixed action facts: no task history, harness, tools or workspace."""

    def __init__(self, services: SharedServices) -> None:
        self.services = services

    async def generate(self, job: TaskReviewRow) -> ApprovalReviewResult:
        if len(job.proposal_json.encode("utf-8")) > _MAX_PROPOSAL_BYTES:
            return ApprovalReviewResult(reason_code="review_context_too_large")
        services = self.services
        await asyncio.to_thread(
            assert_token_quota_available,
            services.user_policy_repo,
            services.usage_repo,
            job.owner_id,
        )
        user = await asyncio.to_thread(services.user_repo.get, job.owner_id)
        if user is None or user.disabled:
            return ApprovalReviewResult()
        store = ProviderStore(services.provider_repo)
        if not await asyncio.to_thread(store.is_model_ref_usable, job.model_ref):
            return ApprovalReviewResult()
        providers = await asyncio.to_thread(store.build_harness_configs)
        task = await asyncio.to_thread(services.project_task_repo.get, job.task_id, job.owner_id)
        factory = ChatModelFactory(providers)
        try:
            model = await asyncio.to_thread(factory.get_chat_model, job.model_ref)
            limit = getattr(model, "_harness_max_output_tokens", 0) or 8_192
            with model_call_scope(
                services,
                user_id=job.owner_id,
                purpose="approval_review",
                task_id=job.task_id,
                turn_id=job.turn_id,
                project_id=task.project_id if task else None,
            ):
                result = await model.ainvoke(
                    [
                        SystemMessage(
                            tr("projects.approval_review_prompt", normalize_locale(user.locale))
                        ),
                        HumanMessage(job.proposal_json),
                    ],
                    max_tokens=min(limit, 8_192),
                )
            usage = result.usage_metadata
            input_tokens = usage["input_tokens"] if usage else 0
            output_tokens = usage["output_tokens"] if usage else 0
            invalid = ApprovalReviewResult(
                reason_code="review_invalid_response",
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
            content = _response_text(result.content)
            if (
                result.tool_calls
                or result.invalid_tool_calls
                or content is None
                or len(content.encode("utf-8")) > _MAX_RESPONSE_BYTES
            ):
                return invalid
            try:
                parsed = json.loads(content, object_pairs_hook=_unique_object)
            except (ValueError, TypeError, RecursionError):
                return invalid
            if (
                not isinstance(parsed, dict)
                or set(parsed) != {"decision"}
                or not isinstance(parsed["decision"], str)
                or parsed["decision"] not in _DECISIONS
            ):
                return invalid
            decision = cast(ReviewDecision, parsed["decision"])
            return ApprovalReviewResult(
                decision=decision,
                reason_code={
                    "allow_once": "review_allow",
                    "deny": "review_deny",
                    "requires_user": "review_requires_user",
                }[decision],
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
        finally:
            await factory.aclose()


class TaskApprovalReviewWorker:
    """A separate async slot with SQL fencing; unknown dispatched work is never retried."""

    def __init__(self, services: SharedServices) -> None:
        self.services = services
        self.agent = TaskApprovalReviewer(services)
        self.worker_id = new_ulid()
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    def wake(self) -> None:
        self._wake.set()

    async def _execute(self, job: TaskReviewRow) -> None:
        result = ApprovalReviewResult()
        try:
            result = await asyncio.wait_for(self.agent.generate(job), _GENERATION_TIMEOUT)
        except TimeoutError:
            result = ApprovalReviewResult(reason_code="review_timeout")
        except OctopError as exc:
            if exc.code == ErrorCode.TOKEN_QUOTA_EXCEEDED:
                result = ApprovalReviewResult(reason_code="review_quota_exceeded")
        except Exception as exc:
            # Action facts and provider exceptions can contain secrets.
            logger.warning("Approval review failed: %s (%s)", job.review_id, type(exc).__name__)
        await asyncio.to_thread(
            self.services.project_task_review_repo.finish,
            job,
            result.decision,
            result.reason_code,
            result.input_tokens,
            result.output_tokens,
            now_ms(),
        )

    async def _loop(self) -> None:
        while True:
            self._wake.clear()
            try:
                job = await asyncio.to_thread(
                    self.services.project_task_review_repo.claim, self.worker_id, now_ms()
                )
                if job is not None:
                    await self._execute(job)
                    continue
            except Exception as exc:
                logger.warning("Approval review worker failed (%s)", type(exc).__name__)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), 0.5)
