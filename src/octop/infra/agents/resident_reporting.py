"""Reentrant task-result delivery to canonical history and graph context."""

from __future__ import annotations

import asyncio
import json
import time
from typing import cast

from langchain_core.messages import AIMessage, message_to_dict, messages_from_dict

from octop.i18n import tr
from octop.infra.agents.manager import AgentManager
from octop.infra.agents.resident_identity import resident_execution_scope
from octop.infra.db.repos.resident_delegations import ResidentTaskReportRow
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.history.projection import message_inputs
from octop.infra.history.service import HistoryArchive


class ResidentReportDelivery:
    """Caller must hold the resident runtime's per-thread execution lock."""

    def __init__(
        self, services: SharedServices, registry: AgentManager, archive: HistoryArchive | None
    ) -> None:
        self.services, self.registry, self.archive = services, registry, archive

    def _message(self, row: ResidentTaskReportRow) -> AIMessage:
        user = self.services.user_repo.get(row.owner_id)
        locale = user.locale if user else "en"
        artifacts = json.loads(row.artifacts_json)
        content = tr(
            "agents.resident.task_report",
            locale,
            task_id=row.task_id,
            turn_id=row.task_turn_id,
            status=tr(f"agents.resident.task_status_{row.status}", locale),
            text=row.text,
        )
        if artifacts:
            content += "\n" + tr(
                "agents.resident.task_artifacts",
                locale,
                count=len(artifacts),
                references=json.dumps(artifacts[:20], ensure_ascii=False),
            )
        return AIMessage(
            id=row.message_id,
            content=content,
            additional_kwargs={
                "checkpoint_ts": row.created_at,
                "source": "task_report",
                "task_report": {
                    "report_id": row.report_id,
                    "task_id": row.task_id,
                    "turn_id": row.task_turn_id,
                    "status": row.status,
                    "error_code": row.error_code,
                    "artifacts": artifacts[:50],
                    "artifact_count": len(artifacts),
                },
            },
        )

    async def deliver(self, report: ResidentTaskReportRow) -> None:
        # Cancelling an await cannot stop a SQLite write already dispatched to
        # an executor. Drain all admitted writes before releasing the thread lock.
        operation = asyncio.create_task(self._deliver(report))
        try:
            await asyncio.shield(operation)
        except asyncio.CancelledError:
            await operation
            raise

    async def _deliver(self, report: ResidentTaskReportRow) -> None:
        repo = self.services.resident_task_report_repo
        row = await asyncio.to_thread(repo.get, report.owner_id, report.report_id)
        if row is None or row.delivery_status == "delivered":
            return
        binding = await asyncio.to_thread(
            self.services.resident_conversation_repo.get_by_thread, row.thread_id
        )
        if binding is None or binding.owner_id != row.owner_id or binding.status != "ready":
            raise OctopError.localized(ErrorCode.NOT_FOUND)
        agent = await asyncio.to_thread(self.services.agent_repo.get, binding.agent_id)
        if agent is None or agent.user_id != row.owner_id or agent.is_shared or not agent.enabled:
            raise OctopError.localized(ErrorCode.NOT_FOUND)
        message = await asyncio.to_thread(self._message, row)
        frozen = await asyncio.to_thread(
            repo.prepare_message,
            row.report_id,
            json.dumps(message_to_dict(message), ensure_ascii=False),
        )
        message = cast(AIMessage, messages_from_dict([json.loads(frozen)])[0])
        if row.archive_written_at is None:
            if self.archive is not None:
                await asyncio.to_thread(
                    self.archive.append_external_message,
                    binding.agent_id,
                    row.thread_id,
                    row.message_id,
                    message_to_dict(message),
                )
            else:
                await asyncio.to_thread(
                    self.services.thread_message_repo.append_legacy_interval,
                    row.thread_id,
                    message_inputs([message]),
                )
            await asyncio.to_thread(repo.mark_archived, row.report_id, time.time_ns() // 1_000_000)
        if row.context_written_at is None:
            try:
                harness = self.registry.get_agent(binding.agent_id)
            except OctopError:
                await self.registry.start(binding.agent_id)
                harness = self.registry.get_agent(binding.agent_id)
            with resident_execution_scope(row.owner_id, row.thread_id):
                await harness.aappend_messages(row.thread_id, [message])
            await asyncio.to_thread(repo.mark_context, row.report_id, time.time_ns() // 1_000_000)
        await asyncio.to_thread(repo.mark_delivered, row.report_id, time.time_ns() // 1_000_000)
