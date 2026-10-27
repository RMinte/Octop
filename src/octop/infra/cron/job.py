"""CronJob — APScheduler callable with status and audit bookkeeping."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from octop.infra.cron.delivery import CronDeliveryCommand
from octop.infra.cron.task_type import normalize_cron_task_type
from octop.infra.db.repos.audit import ACTOR_SYSTEM

if TYPE_CHECKING:
    from octop.infra.cron.delivery import CronDeliveryService
    from octop.infra.db.repos.audit import AuditRepo
    from octop.infra.db.repos.cron import CronJobRepo, CronJobRow

logger = logging.getLogger(__name__)


class CronJob:
    def __init__(
        self,
        *,
        cron_id: str,
        name: str,
        agent_id: str | None,
        prompt: str,
        fresh_thread: bool,
        session_key: str,
        model: str | None,
        task_type: str,
        mcp_servers: list[str] | None,
        user_id: int,
        delivery_service: CronDeliveryService,
        cron_repo: CronJobRepo,
        audit_repo: AuditRepo,
        conversation_policy: str = "continue_target",
        target_task_id: str | None = None,
        project_id: str | None = None,
        selected_expert_id: str | None = None,
        skip_confirmation: bool = False,
    ) -> None:
        self._cron_id = cron_id
        self._name = name
        self._agent_id = agent_id
        self._prompt = prompt
        self._fresh_thread = fresh_thread
        self._session_key = session_key
        self._model = model
        self._task_type = normalize_cron_task_type(task_type)
        self._mcp_servers = list(mcp_servers or [])
        self._user_id = user_id
        self._delivery_service = delivery_service
        self._cron_repo = cron_repo
        self._audit_repo = audit_repo
        (
            self._conversation_policy,
            self._target_task_id,
            self._project_id,
            self._selected_expert_id,
        ) = conversation_policy, target_task_id, project_id, selected_expert_id
        self._skip_confirmation = skip_confirmation

    @classmethod
    def from_row(
        cls,
        row: CronJobRow,
        *,
        delivery_service: CronDeliveryService,
        cron_repo: CronJobRepo,
        audit_repo: AuditRepo,
    ) -> CronJob:
        return cls(
            cron_id=row.cron_id,
            name=row.name,
            agent_id=row.agent_id,
            prompt=row.prompt,
            fresh_thread=bool(row.fresh_thread),
            session_key=row.session_key,
            model=row.model,
            task_type=row.task_type,
            mcp_servers=list(row.mcp_servers),
            user_id=row.user_id,
            delivery_service=delivery_service,
            cron_repo=cron_repo,
            audit_repo=audit_repo,
            conversation_policy=row.conversation_policy,
            target_task_id=row.target_task_id,
            project_id=row.project_id,
            selected_expert_id=row.selected_expert_id,
            skip_confirmation=row.skip_confirmation,
        )

    async def run(self, *, raise_on_error: bool = False, event_key: str = "") -> None:
        from octop.infra.metrics import METRICS  # noqa: PLC0415

        METRICS.inc("cron_runs_total")
        ts = int(time.time())

        try:
            await self._delivery_service.deliver(
                CronDeliveryCommand(
                    cron_id=self._cron_id,
                    cron_name=self._name,
                    agent_id=self._agent_id,
                    user_id=self._user_id,
                    session_key=self._session_key,
                    prompt=self._prompt,
                    fresh_thread=self._fresh_thread,
                    task_type=self._task_type,
                    model=self._model,
                    mcp_servers=tuple(self._mcp_servers),
                    event_key=event_key,
                    conversation_policy=self._conversation_policy,
                    target_task_id=self._target_task_id,
                    project_id=self._project_id,
                    selected_expert_id=self._selected_expert_id,
                    skip_confirmation=self._skip_confirmation,
                )
            )
        except Exception as exc:
            METRICS.inc("cron_errors_total")
            logger.exception("cron job %s failed", self._cron_id)
            err = str(exc)
            self._cron_repo.set_run_status(self._cron_id, ts=ts, status="error", error=err)
            self._audit_repo.write(
                actor=ACTOR_SYSTEM,
                action="cron.run_failed",
                target=self._cron_id,
                payload=err,
            )
            if raise_on_error:
                raise
            return
        self._cron_repo.set_run_status(self._cron_id, ts=ts, status="ok", error=None)
        self._audit_repo.write(
            actor=ACTOR_SYSTEM,
            action="cron.run_ok",
            target=self._cron_id,
        )

    def command(self, event_key: str) -> CronDeliveryCommand:
        return CronDeliveryCommand(
            cron_id=self._cron_id,
            cron_name=self._name,
            agent_id=self._agent_id,
            user_id=self._user_id,
            session_key=self._session_key,
            prompt=self._prompt,
            fresh_thread=self._fresh_thread,
            task_type=self._task_type,
            model=self._model,
            mcp_servers=tuple(self._mcp_servers),
            event_key=event_key,
            conversation_policy=self._conversation_policy,
            target_task_id=self._target_task_id,
            project_id=self._project_id,
            selected_expert_id=self._selected_expert_id,
            skip_confirmation=self._skip_confirmation,
        )
