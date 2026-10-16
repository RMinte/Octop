"""Read-only review receipts; they never grant cross-host execution authority."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from octop.infra.projects.models import ProjectModel


class ApprovalReview(ProjectModel):
    review_id: str
    status: Literal["queued", "reviewing", "completed", "cancelled"]
    decision: Literal["allow_once", "deny", "requires_user"] | None
    reason_code: str
    action_digest: str
    context_digest: str | None = None
    model_ref: str
    profile_version: Literal[1] = 1
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
