"""Freeze knowledge-base selection for a task turn without expert defaults."""

from __future__ import annotations

from typing import Any

from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.knowledge.default_open import merge_knowledge_base_ids
from octop.infra.knowledge.hint import catalog_for_selected_bases


def selection_snapshot(
    services: SharedServices, owner_id: int, explicit_ids: list[str] | None
) -> dict[str, Any]:
    user = services.user_repo.get(owner_id)
    bases = (
        services.knowledge_repo.list_all()
        if user and user.role == "admin"
        else services.knowledge_repo.list_visible(owner_id)
    )
    selected = merge_knowledge_base_ids(bases, explicit_ids, owner_user_id=owner_id)
    if not set(selected) <= {base.id for base in bases}:
        raise OctopError.localized(ErrorCode.KNOWLEDGE_NOT_FOUND)
    return {
        "knowledge_base_ids": selected,
        "knowledge_base_catalog": catalog_for_selected_bases(bases, selected),
    }
