"""Health-aware execution routes without changing the admitted turn snapshot."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from octop_harness.config import ProviderConfig

from octop.infra.agents.middleware.model_failover import (
    ModelExecutionSafety,
    SafeModelFailoverMiddleware,
)
from octop.infra.agents.middleware.reasoning import CONFIG_KEY
from octop.infra.agents.providers.box import BoxProviderConfig, OctopChatModelFactory
from octop.infra.agents.providers.health import model_health_for
from octop.infra.agents.providers.store import AutoRouteCandidate, ProviderStore
from octop.infra.db.repos.project_task_turns import ProjectTaskTurnRow
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects.checkpoint_saver import checkpoint_fence
from octop.infra.projects.tasks import TaskExecutionOptions, _reasoning_configuration


def automatic_route(configuration: dict[str, Any]) -> bool:
    """Only routes recorded as automatic may choose another model."""
    if configuration.get("execution_kind") == "resident":
        return configuration.get("resident_configuration", {}).get("automatic_model_route") is True
    options = configuration.get("execution_options")
    if not isinstance(options, dict) or options.get("model_ref"):
        return False
    profile = configuration.get("expert_profile") or {}
    routing = configuration.get("routing") or {}
    return routing.get("target") != "explicit" and (
        bool(routing) or not profile.get("default_model")
    )


async def task_failover_middleware(
    services: SharedServices,
    owner_id: int,
    turn: ProjectTaskTurnRow,
    configuration: dict[str, Any],
    provider_configs: list[ProviderConfig],
) -> SafeModelFailoverMiddleware:
    """Preserve Box boundaries, reasoning choices and an audit trail per route."""
    providers = ProviderStore(services.provider_repo)
    source = next(p for p in provider_configs if p.id == turn.model_ref.partition("/")[0])
    allowed = {
        p.id
        for p in provider_configs
        if isinstance(p, BoxProviderConfig) == isinstance(source, BoxProviderConfig)
    }
    options = TaskExecutionOptions.model_validate(configuration.get("execution_options") or {})

    def catalog() -> tuple[list[AutoRouteCandidate], set[str], dict[str, dict[str, object]]]:
        compatible: list[AutoRouteCandidate] = []
        reasoning: dict[str, dict[str, object]] = {}
        for candidate in providers.list_auto_route_candidates(needs_multimodal=False):
            if candidate.ref.partition("/")[0] not in allowed:
                continue
            try:
                reasoning[candidate.ref] = _reasoning_configuration(
                    providers, candidate.ref, options
                )
            except OctopError:
                continue
            compatible.append(candidate)
        vision = {
            candidate.ref
            for candidate in providers.list_auto_route_candidates(needs_multimodal=True)
        }
        return compatible, vision, reasoning

    candidates, vision, reasoning = await asyncio.to_thread(catalog)
    factory = OctopChatModelFactory(provider_configs)
    current_ref = turn.model_ref

    async def selected(ref: str, configurable: dict[str, Any]) -> None:
        nonlocal current_ref
        previous_ref = current_ref

        def record() -> None:
            with checkpoint_fence(services, turn):
                with services.db.transaction() as conn:
                    if not services.project_task_repo.lock_owner(conn, owner_id):
                        raise OctopError.localized(ErrorCode.FORBIDDEN)
                    row = services.project_task_turn_repo.get_in_transaction(conn, turn.turn_id)
                    if (
                        row is None
                        or row.status != "running"
                        or row.worker_id != turn.worker_id
                        or row.cancel_requested
                    ):
                        raise OctopError.localized(ErrorCode.TASK_INTERRUPTED)
                services.audit_repo.user_event(
                    str(owner_id),
                    "task.model_failover",
                    turn.turn_id,
                    json.dumps(
                        {
                            "task_id": turn.task_id,
                            "planned_model_ref": turn.model_ref,
                            "from_model_ref": previous_ref,
                            "to_model_ref": ref,
                        }
                    ),
                )

        await asyncio.to_thread(record)
        current_ref = ref
        configurable[CONFIG_KEY] = reasoning[ref].get("reasoning_overrides", {})

    return SafeModelFailoverMiddleware(
        health=model_health_for(services.provider_repo),
        candidates=lambda _current, needs_multimodal: [
            candidate for candidate in candidates if not needs_multimodal or candidate.ref in vision
        ],
        model_for_ref=factory.get_chat_model,
        max_retries=0,
        initial_delay=1,
        max_delay=4,
        on_failure="error",
        on_model_selected=selected,
        close_models=factory.aclose,
        execution_safety=ModelExecutionSafety(
            allowed=not bool(turn.checkpoint_id or turn.reply or turn.recovery_json)
        ),
    )
