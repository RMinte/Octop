"""Claim-bound review advice for a client action, never a client execution grant."""

from __future__ import annotations

import hmac
import json
import time

from octop.infra.db.repos.project_task_reviews import TaskReviewRow
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects import approval_policy, client_tools, interactions
from octop.infra.projects.client_tool_models import ClientToolCall, ClientToolReviewRequest
from octop.infra.utils.ulid import new_ulid


def request_review(
    services: SharedServices,
    owner: int,
    client_id: str,
    task_id: str,
    turn_id: str,
    call_id: str,
    body: ClientToolReviewRequest,
) -> ClientToolCall:
    error = None
    with services.db.transaction() as conn:
        row = client_tools._load(services, conn, owner, client_id, task_id, turn_id, call_id)
        context = json.loads(row.approval_context_json)
        settings = approval_policy.settings(services)
        arguments = json.loads(row.arguments_json)
        turn = services.project_task_turn_repo.get_in_transaction(conn, turn_id)
        assert turn is not None
        command = arguments.get("command")
        existing = services.project_task_review_repo.get(conn, "client", call_id)
        proposal = interactions.encode(
            {
                "execution_host": "client",
                "tool_name": row.name,
                "arguments": arguments,
                "action_digest": body.action_digest,
                "mode": context.get("mode"),
                "approval_revision": context.get("revision"),
                "user_goal": turn.prompt[:4000],
                **body.context.model_dump(mode="json"),
            }
        )
        if not row.claim_token or not hmac.compare_digest(row.claim_token, body.claim_token):
            error = ErrorCode.FORBIDDEN
        elif not context or body.action_digest != context.get("action_digest"):
            error = ErrorCode.TASK_APPROVAL_REVIEW_CONFLICT
        elif existing is not None:
            if existing.proposal_json != proposal:
                error = ErrorCode.TASK_APPROVAL_REVIEW_CONFLICT
        elif row.status != "claimed" or client_tools._previous_denial(
            services, conn, turn_id, client_id, row.name, arguments
        ):
            error = ErrorCode.TASK_CLIENT_TOOL_CLOSED
        elif (
            row.name != "exec_command"
            or not isinstance(command, list)
            or not command
            or not all(isinstance(arg, str) and arg and "\x00" not in arg for arg in command)
            or context.get("requires_user")
            or row.name in settings.require_human_tools
            or "client__" + row.name in settings.require_human_tools
        ):
            error = ErrorCode.TASK_APPROVAL_REVIEW_INVALID
        elif (
            context.get("mode") != "approve_for_me"
            or "approve_for_me" not in settings.allowed_modes
        ):
            error = ErrorCode.TASK_APPROVAL_MODE_UNAVAILABLE
        else:
            now = int(time.time() * 1000)
            services.project_task_review_repo.enqueue(
                conn,
                TaskReviewRow(
                    review_id="review_" + new_ulid(),
                    task_id=task_id,
                    turn_id=turn_id,
                    owner_id=owner,
                    execution_host="client",
                    target_id=call_id,
                    action_digest=body.action_digest,
                    proposal_json=proposal,
                    model_ref=turn.model_ref,
                    created_at=now,
                    updated_at=now,
                    expires_at=row.expires_at,
                ),
            )
    if error:
        raise OctopError.localized(error)
    return client_tools.get_call(services, owner, client_id, task_id, turn_id, call_id)
