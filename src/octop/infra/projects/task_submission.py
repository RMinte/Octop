"""Classify routed requests outside SQL transactions, then admit one stable turn."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

from langchain_core.messages import messages_from_dict

from octop.infra.agents.providers.store import ProviderStore
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.gateway.media.attachment_hints import content_blocks_need_vision
from octop.infra.projects import engine_catalog, input_attachments, tasks
from octop.infra.projects.context import is_compaction
from octop.infra.projects.model_routing import (
    ModelRoutingDecision,
    ModelRoutingSettings,
    RoutingRequest,
    decide_model,
    load_settings,
)
from octop.infra.projects.routing_classifier import (
    DifficultyAssessment,
    DifficultyAttachment,
    DifficultyFacts,
    classify_difficulty,
)
from octop.infra.usage.model_calls import model_call_scope

AdmissionHook = Callable[[Any, str | None, str | None], None]


@dataclass(frozen=True)
class _PreparedRouting:
    facts: DifficultyFacts
    settings: ModelRoutingSettings
    request: RoutingRequest
    requires_images: bool
    digest: str
    project_id: str | None


def _history_text(value: object) -> str:
    """Extract text from the stored effective context without loading any files."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(_history_text(item) for item in value)
    if isinstance(value, dict):
        if "data" in value:
            return _history_text(value["data"])
        if value.get("type") in {"image_url", "image"}:
            return "[image]"
        return _history_text(value.get("content", value.get("text", "")))
    return ""


def _prepare_in_transaction(
    services: SharedServices,
    conn: Any,
    owner_id: int,
    body: tasks.TaskMessage | tasks.TaskRetry,
    *,
    task_id: str | None,
    project_id: str | None,
    retry_of_turn_id: str | None,
) -> _PreparedRouting | str:
    task, message, previous = tasks._submission_state(
        services,
        conn,
        owner_id,
        body,
        task_id=task_id,
        project_id=project_id,
        retry_of_turn_id=retry_of_turn_id,
    )
    if previous is not None:
        assert task is not None
        return task.task_id
    configuration = tasks._base_configuration(
        services,
        owner_id,
        task.project_id if task else project_id,
        message.options,
        selected_expert_id=task.selected_expert_id
        if task
        else message.selected_expert_id
        if isinstance(message, tasks.ProjectTaskCreate)
        else None,
        action_mode=cast(
            tasks.ActionMode,
            task.action_mode
            if task
            else message.action_mode
            if isinstance(message, tasks.ProjectTaskCreate)
            else "craft",
        ),
        configuration_revision=task.configuration_revision if task else 1,
    )
    assert message.options.routing is not None
    settings = load_settings(services)
    attached = [
        input_attachments._available(
            services.project_task_attachment_repo.get(conn, owner_id, attachment_id)
        )
        for attachment_id in message.attachment_ids
    ]
    current_id = task.task_id if task else None
    if any(row.task_id not in {None, current_id} for row in attached):
        raise OctopError.localized(ErrorCode.TASK_ATTACHMENT_UNAVAILABLE)
    existing = (
        services.project_task_attachment_repo.for_task(conn, owner_id, task.task_id) if task else []
    )
    metadata = {row.attachment_id: row for row in [*attached, *reversed(existing)]}
    requires_images = not is_compaction(message.prompt, message.attachment_ids) and any(
        input_attachments.is_image(row.media_type) for row in metadata.values()
    )
    context = services.project_task_turn_repo.context(task.task_id) if task else None
    if context and not is_compaction(message.prompt, message.attachment_ids):
        requires_images = requires_images or any(
            content_blocks_need_vision(item.content)
            for item in messages_from_dict(json.loads(context[1]))
        )
    history = [_history_text(json.loads(context[1]))] if context else []
    rows = services.project_task_turn_repo.list_in_transaction(conn, task.task_id) if task else []
    for turn in rows:
        if context and turn.pk <= context[0]:
            continue
        if turn.status not in {"completed", "failed", "cancelled"} or is_compaction(
            turn.prompt, json.loads(turn.configuration_json).get("attachment_ids", [])
        ):
            continue
        history.extend([f"user: {turn.prompt}", f"assistant: {turn.reply}"])
    full_history = "\n".join(history)
    facts = DifficultyFacts(
        prompt=message.prompt,
        project_instructions=str(configuration.get("instructions", "")),
        history_summary=full_history[-16000:],
        attachments=[
            DifficultyAttachment(
                name=row.name, media_type=row.media_type, byte_count=row.byte_count
            )
            for row in metadata.values()
        ],
        truncated=len(full_history) > 16000,
    )
    fingerprint = json.dumps(
        {
            "configuration": configuration,
            "settings": settings.model_dump(),
            "history": full_history,
            "context_digest": hashlib.sha256(context[1].encode()).hexdigest() if context else None,
            "prompt": message.prompt,
            "attachments": [
                {"id": row.attachment_id, "sha256": row.sha256, "media_type": row.media_type}
                for row in metadata.values()
            ],
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return _PreparedRouting(
        facts,
        settings,
        message.options.routing,
        requires_images,
        hashlib.sha256(fingerprint.encode()).hexdigest(),
        task.project_id if task else project_id,
    )


def _prepare(
    services: SharedServices,
    owner_id: int,
    body: tasks.TaskMessage | tasks.TaskRetry,
    admission_hook: AdmissionHook | None = None,
    **identity: Any,
) -> _PreparedRouting | str:
    with services.db.transaction() as conn:
        if admission_hook is not None:
            admission_hook(conn, None, None)
        return _prepare_in_transaction(services, conn, owner_id, body, **identity)


def _admit(
    services: SharedServices,
    owner_id: int,
    body: tasks.TaskMessage | tasks.TaskRetry,
    prepared: _PreparedRouting,
    decision: ModelRoutingDecision,
    admission_hook: AdmissionHook | None = None,
    **identity: Any,
) -> tasks.TaskDetail:
    with services.db.transaction() as conn:
        if admission_hook is not None:
            admission_hook(conn, None, None)
        current = _prepare_in_transaction(services, conn, owner_id, body, **identity)
        if isinstance(current, str):
            task_id = current
        else:
            if current.digest != prepared.digest:
                raise OctopError.localized(ErrorCode.TASK_REVISION_CONFLICT)
            task_id, _ = tasks.send_in_transaction(
                services, conn, owner_id, body, routing_decision=decision, **identity
            )
        if admission_hook is not None:
            _, turn_id = tasks.send_in_transaction(
                services, conn, owner_id, body, routing_decision=decision, **identity
            )
            admission_hook(conn, task_id, turn_id)
    return tasks.detail(services, owner_id, task_id)


def _send_with_hook(
    services: SharedServices,
    owner_id: int,
    body: tasks.TaskMessage | tasks.TaskRetry,
    hook: AdmissionHook,
    **identity: Any,
) -> tasks.TaskDetail:
    with services.db.transaction() as conn:
        hook(conn, None, None)
        task_id, turn_id = tasks.send_in_transaction(services, conn, owner_id, body, **identity)
        hook(conn, task_id, turn_id)
    return tasks.detail(services, owner_id, task_id)


async def submit(
    services: SharedServices,
    owner_id: int,
    body: tasks.TaskMessage | tasks.TaskRetry,
    *,
    task_id: str | None = None,
    project_id: str | None = None,
    retry_of_turn_id: str | None = None,
    admission_hook: AdmissionHook | None = None,
) -> tasks.TaskDetail:
    identity = {
        "task_id": task_id,
        "project_id": project_id,
        "retry_of_turn_id": retry_of_turn_id,
    }
    selected_engine = (
        body.engine
        if isinstance(body, tasks.ProjectTaskCreate) and task_id is None
        else engine_catalog.read_engine(row.execution_json)
        if task_id
        and (row := await asyncio.to_thread(services.project_task_repo.get, task_id, owner_id))
        else engine_catalog.EngineSelection()
    )
    if (
        body.options.routing is None
        or body.options.model_ref
        or selected_engine.engine_id != "octop"
    ):
        if admission_hook is not None:
            return await asyncio.to_thread(
                _send_with_hook, services, owner_id, body, admission_hook, **identity
            )
        return await asyncio.to_thread(tasks.send, services, owner_id, body, **identity)
    prepared = await asyncio.to_thread(
        _prepare, services, owner_id, body, admission_hook, **identity
    )
    if isinstance(prepared, str):
        if admission_hook is not None:
            return await asyncio.to_thread(
                _send_with_hook, services, owner_id, body, admission_hook, **identity
            )
        return await asyncio.to_thread(tasks.detail, services, owner_id, prepared)
    providers = ProviderStore(services.provider_repo)
    decision = await asyncio.to_thread(
        decide_model,
        providers,
        prepared.settings,
        mode=prepared.request.mode,
        level=prepared.request.level,
        requires_images=prepared.requires_images,
    )
    if (
        decision.level > 0
        and decision.target == "box"
        and decision.reason_code != "capability_image_box"
    ):
        configs = await asyncio.to_thread(providers.build_harness_configs)
        provider_name, _, model_id = decision.model_ref.partition("/")
        provider = next((p for p in configs if p.id == provider_name), None)
        model = next((m for m in provider.models if m.id == model_id), None) if provider else None
        assessment = DifficultyAssessment(difficulty=None, reason_code="classifier_unavailable")
        if provider is not None and model is not None:
            try:
                with model_call_scope(
                    services,
                    user_id=owner_id,
                    purpose="routing_classifier",
                    task_id=task_id,
                    project_id=prepared.project_id,
                    submission_id=body.request_id,
                ):
                    assessment = await classify_difficulty(provider, model, prepared.facts)
            except Exception:
                # Caller cancellation remains a BaseException and is propagated.
                assessment = DifficultyAssessment(difficulty=None, reason_code="classifier_error")
        decision = await asyncio.to_thread(
            decide_model,
            providers,
            prepared.settings,
            mode=prepared.request.mode,
            level=prepared.request.level,
            difficulty=assessment.difficulty,
            reason_code=assessment.reason_code,
            requires_images=prepared.requires_images,
        )
        decision = decision.model_copy(
            update={
                "classifier_input_tokens": assessment.input_tokens,
                "classifier_output_tokens": assessment.output_tokens,
                "classifier_duration_ms": assessment.duration_ms,
                "classifier_input_truncated": assessment.input_truncated,
            }
        )
    decision = decision.model_copy(update={"input_digest": prepared.digest})
    return await asyncio.to_thread(
        _admit, services, owner_id, body, prepared, decision, admission_hook, **identity
    )
