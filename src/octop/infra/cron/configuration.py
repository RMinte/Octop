"""Validate scheduled Task selection consistently across local entrypoints."""

from __future__ import annotations

from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects.expert_profiles import expert_configuration


def validate_task_selection(
    services: SharedServices,
    owner_id: int,
    *,
    conversation_policy: str,
    target_task_id: str | None,
    project_id: str | None,
    selected_expert_id: str | None,
) -> None:
    user = services.user_repo.get(owner_id)
    if user is None or user.disabled:
        raise OctopError.localized(ErrorCode.FORBIDDEN)
    if conversation_policy not in {"continue_target", "per_run", "per_job", "task"} or (
        (conversation_policy == "task") != bool(target_task_id)
    ):
        raise OctopError.localized(ErrorCode.TASK_REQUEST_CONFLICT)
    if selected_expert_id is not None:
        expert_configuration(services, owner_id, selected_expert_id)
    if project_id is not None:
        project = services.project_repo.get(project_id, owner_id)
        if project is None or project.archived:
            raise OctopError.localized(ErrorCode.NOT_FOUND)
    if target_task_id is not None:
        target = services.project_task_repo.get(target_task_id, owner_id)
        if target is None or target.archived:
            raise OctopError.localized(ErrorCode.NOT_FOUND)
        if project_id is not None and target.project_id != project_id:
            raise OctopError.localized(ErrorCode.TASK_REQUEST_CONFLICT)
