"""Explicit task branches copy visible history, never an execution or authorization."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from typing import Any, Literal, Self

from pydantic import Field, model_validator

from octop.infra.db.repos.project_task_branches import ProjectTaskBranchRow
from octop.infra.db.repos.project_task_turns import ProjectTaskTurnRow
from octop.infra.db.repos.project_tasks import ProjectTaskRow
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects import approval_policy, input_attachments
from octop.infra.projects.content import TaskContentBlock
from octop.infra.projects.conversation_import_models import ImportedTurnHistory
from octop.infra.projects.deletion_state import assert_project_writable
from octop.infra.projects.engine_catalog import (
    EngineSelection,
    execution_json,
    read_engine,
    validate_engine,
)
from octop.infra.projects.expert_profiles import resolve_expert_profile
from octop.infra.projects.input_attachment_models import TaskInputAttachment
from octop.infra.projects.legacy_conversation_models import LegacySourceAttachment
from octop.infra.projects.models import ProjectModel, Reference
from octop.infra.projects.tasks import ActionMode, Prompt, RequestID, now_ms
from octop.infra.utils.ulid import new_ulid


class TaskFork(ProjectModel):
    engine: EngineSelection | None = Field(
        default=None, description="省略继承来源执行引擎；分支创建新的原生会话。"
    )
    action_mode: ActionMode | None = Field(default=None, description="分支执行模式；省略继承来源。")
    request_id: RequestID = Field(description="分支创建的幂等标识；相同标识必须重发相同请求。")
    operation: Literal["fork", "edit", "regenerate"] = Field(
        default="fork", description="fork 包含锚点；edit 和 regenerate 从锚点前继续。"
    )
    turn_id: Reference | None = Field(
        default=None,
        description="来源中已完成的轮次，包含继承的历史副本；省略使用来源最后一轮，活动来源不可分支。",
    )
    prompt: Prompt | None = Field(
        default=None,
        description="edit 的新用户文字；fork 可用于分支后继续发送；regenerate 沿用原文。",
    )
    project_id: Reference | None = Field(
        default=None, description="省略继承来源项目；显式 null 选择无项目。"
    )
    selected_expert_id: Reference | None = Field(
        default=None, description="省略继承来源专家；显式 null 使用通用助手。"
    )

    @model_validator(mode="after")
    def validate_operation(self) -> Self:
        if self.operation == "edit" and self.prompt is None:
            raise ValueError("edit requires prompt")
        if self.operation == "regenerate" and self.prompt is not None:
            raise ValueError("regenerate uses the original prompt")
        return self


@dataclass(frozen=True)
class BranchResult:
    task_id: str
    continuation_prompt: str | None
    continuation_attachment_ids: list[str]


def _result(row: ProjectTaskBranchRow) -> BranchResult:
    return BranchResult(
        row.task_id, row.continuation_prompt, json.loads(row.continuation_attachments_json)
    )


def _title(services: SharedServices, conn: Any, source: ProjectTaskRow) -> str:
    tasks = services.project_task_repo
    base = source.title
    if (
        services.project_task_branch_repo.origin(conn, source.task_id) is not None
        and tasks.title_source(conn, source.task_id) == "generated"
    ):
        base = re.sub(r" \([0-9]+\)$", "", base)
    number = 2
    while True:
        suffix = f" ({number})"
        title = base[: 128 - len(suffix)].rstrip() + suffix
        if not tasks.title_exists(conn, source.owner_id, title):
            return title
        number += 1


def _replace_ids(value: Any, replacements: dict[str, str]) -> Any:
    if isinstance(value, str):
        for original in sorted(replacements, key=len, reverse=True):
            value = value.replace(original, replacements[original])
        return value
    if isinstance(value, list):
        return [_replace_ids(item, replacements) for item in value]
    if isinstance(value, dict):
        return {key: _replace_ids(item, replacements) for key, item in value.items()}
    return value


class _HistoryCopy:
    def __init__(
        self, services: SharedServices, conn: Any, owner_id: int, source_id: str, task_id: str
    ) -> None:
        self.services, self.conn, self.owner_id = services, conn, owner_id
        self.source_id, self.task_id = source_id, task_id
        self.attachments: dict[str, TaskInputAttachment] = {}
        self.replacements: dict[str, str] = {}

    def attachment(self, attachment_id: str) -> TaskInputAttachment:
        if attachment_id in self.attachments:
            return self.attachments[attachment_id]
        repo = self.services.project_task_attachment_repo
        stored = repo.download(self.conn, self.owner_id, attachment_id)
        if stored is None or stored[0].task_id != self.source_id:
            raise OctopError.localized(ErrorCode.TASK_ATTACHMENT_UNAVAILABLE)
        row, data = stored
        if len(data) != row.byte_count or hashlib.sha256(data).hexdigest() != row.sha256:
            raise OctopError.localized(ErrorCode.TASK_ATTACHMENT_UNAVAILABLE)
        copied = replace(row, attachment_id="input_" + new_ulid(), task_id=self.task_id)
        repo.insert(self.conn, copied, data)
        view = input_attachments.view(copied)
        self.attachments[attachment_id] = view
        self.replacements[attachment_id] = view.attachment_id
        return view

    def artifacts(self, source_turn: ProjectTaskTurnRow, copied_turn_id: str) -> None:
        repo = self.services.project_task_artifact_repo
        for row in repo.for_turns(self.conn, self.source_id, [source_turn.turn_id]):
            stored = repo.download(self.conn, self.source_id, row.artifact_id)
            if stored is None:
                raise OctopError.localized(ErrorCode.TASK_ARTIFACT_UNAVAILABLE)
            _, data = stored
            if len(data) != row.byte_count or hashlib.sha256(data).hexdigest() != row.sha256:
                raise OctopError.localized(ErrorCode.TASK_ARTIFACT_UNAVAILABLE)
            copied = replace(
                row,
                artifact_id="artifact_" + new_ulid(),
                task_id=self.task_id,
                turn_id=copied_turn_id,
                tool_call_id="history_" + row.artifact_id,
                request_hash=hashlib.sha256((self.task_id + row.artifact_id).encode()).hexdigest(),
                source_path=None,
            )
            repo.insert(self.conn, copied, data)
            self.replacements[row.artifact_id] = copied.artifact_id
            self.replacements[
                f"/api/project-tasks/{self.source_id}/artifacts/{row.artifact_id}"
            ] = f"/api/project-tasks/{self.task_id}/artifacts/{copied.artifact_id}"

    def configuration(self, source: ProjectTaskTurnRow, operation: str) -> dict[str, Any]:
        original = json.loads(source.configuration_json)
        attached = [self.attachment(item) for item in original.get("attachment_ids", [])]
        expert = original.get("expert_profile") or {}
        configuration: dict[str, Any] = {
            "branch_origin": {
                "source_task_id": self.source_id,
                "source_turn_id": source.turn_id,
                "operation": operation,
                "history_only": True,
                "source_status": source.status,
            },
            "action_mode": original.get("action_mode", "craft"),
            "expert_profile": {
                key: expert[key]
                for key in ("expert_id", "name", "config_revision")
                if key in expert
            },
            "attachment_ids": [item.attachment_id for item in attached],
            "attachments": [item.model_dump(mode="json") for item in attached],
        }
        if original.get("imported_history"):
            imported = ImportedTurnHistory.model_validate(original["imported_history"])
            imported.assistant_attachments = [
                self.attachment(item.attachment_id) for item in imported.assistant_attachments
            ]
            configuration["imported_history"] = imported.model_dump(mode="json")
        if original.get("legacy_attachments"):
            configuration["legacy_attachments"] = [
                LegacySourceAttachment.model_validate(item).model_dump(mode="json")
                for item in original["legacy_attachments"]
            ]
        if original.get("migrated_history"):
            configuration["migrated_history"] = original["migrated_history"]
        return configuration

    def turn(self, source: ProjectTaskTurnRow, operation: str) -> None:
        copied_id = "turn_" + new_ulid()
        configuration = self.configuration(source, operation)
        blocks = [
            TaskContentBlock.model_validate(item) for item in json.loads(source.content_blocks_json)
        ]
        for index, block in enumerate(blocks):
            block.block_id = f"{copied_id}:{index + 1}"
            if block.status == "streaming":
                block.status = "interrupted"
        # The target turn must exist before its immutable artifact copies can reference it.
        copied = replace(
            source,
            turn_id=copied_id,
            task_id=self.task_id,
            request_id="history_" + new_ulid(),
            configuration_json=json.dumps(configuration),
            model_ref="",
            worker_id="",
            cancel_requested=False,
            input_tokens=0,
            output_tokens=0,
            pk=0,
            content_blocks_json="[]",
            checkpoint_id="",
            pause_json="",
            interactions_json="[]",
            interaction_revision=0,
            pause_requested=False,
            wait_kind="",
            requests_json="[]",
            started_at=None,
            recovery_json="",
        )
        self.services.project_task_branch_repo.insert_history(self.conn, copied)
        self.artifacts(source, copied_id)
        # File references become branch-owned IDs; no copied path points at the source workspace.
        self.services.project_task_branch_repo.update_history_content(
            self.conn,
            copied_id,
            prompt=_replace_ids(source.prompt, self.replacements),
            reply=_replace_ids(source.reply, self.replacements),
            reasoning=_replace_ids(source.reasoning, self.replacements),
            content_blocks_json=json.dumps(
                _replace_ids([item.model_dump(mode="json") for item in blocks], self.replacements)
            ),
            configuration_json=json.dumps(_replace_ids(configuration, self.replacements)),
        )


def create(
    services: SharedServices, owner_id: int, source_task_id: str, body: TaskFork
) -> BranchResult:
    encoded = json.dumps(
        [source_task_id, body.model_dump(exclude_unset=True)], sort_keys=True, separators=(",", ":")
    )
    fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
    with services.db.transaction() as conn:
        tasks, branches = services.project_task_repo, services.project_task_branch_repo
        if not tasks.lock_owner(conn, owner_id):
            raise OctopError.localized(ErrorCode.FORBIDDEN)
        previous = branches.by_request(conn, owner_id, body.request_id)
        if previous is not None:
            if previous.fingerprint != fingerprint:
                raise OctopError.localized(ErrorCode.TASK_REQUEST_CONFLICT)
            return _result(previous)
        source = tasks.get_in_transaction(conn, source_task_id, owner_id)
        if source is None:
            raise OctopError.localized(ErrorCode.NOT_FOUND)
        assert_project_writable(services, conn, owner_id, source.project_id)
        if source.active_turn_id is not None:
            raise OctopError.localized(ErrorCode.TASK_BUSY)
        history = services.project_task_turn_repo.list_in_transaction(conn, source_task_id)
        anchor = (
            next((turn for turn in history if turn.turn_id == body.turn_id), None)
            if body.turn_id
            else (history[-1] if history else None)
        )
        if anchor is None or anchor.status != "completed":
            raise OctopError.localized(ErrorCode.TASK_RETRY_UNAVAILABLE)
        project_id = body.project_id if "project_id" in body.model_fields_set else source.project_id
        selected = (
            body.selected_expert_id
            if "selected_expert_id" in body.model_fields_set
            else source.selected_expert_id
        )
        if project_id is not None:
            project = services.project_repo.get_in_transaction(conn, project_id, owner_id)
            if project is None:
                raise OctopError.localized(ErrorCode.NOT_FOUND)
            assert_project_writable(services, conn, owner_id, project_id)
            if project.archived:
                raise OctopError.localized(ErrorCode.PROJECT_ARCHIVED)
        resolve_expert_profile(services, owner_id, selected)
        engine = body.engine or read_engine(source.execution_json)
        action_mode = body.action_mode or source.action_mode
        if body.engine is not None or body.action_mode is not None:
            validate_engine(services, owner_id, engine, action_mode=action_mode)
        policy = approval_policy.in_transaction(conn, services, owner_id)
        task_id, now = "task_" + new_ulid(), now_ms()
        continuation = anchor.prompt if body.operation == "regenerate" else body.prompt
        if body.operation == "fork" and not continuation:
            continuation = None
        tasks.insert(
            conn,
            ProjectTaskRow(
                task_id,
                owner_id,
                project_id,
                _title(services, conn, source),
                "fork_" + hashlib.sha256(f"{owner_id}:{body.request_id}".encode()).hexdigest(),
                None,
                now,
                now,
                status="completed",
                selected_expert_id=selected,
                action_mode=action_mode,
                execution_json=execution_json(engine),
                web_search_enabled=source.web_search_enabled,
                approval_mode=policy.mode,
                approval_revision=policy.revision,
                approval_updated_at=int(policy.updated_at.timestamp() * 1000),
            ),
            generated_title=True,
        )
        copying = _HistoryCopy(services, conn, owner_id, source_task_id, task_id)
        for turn in history:
            if turn.pk > anchor.pk or (turn.pk == anchor.pk and body.operation != "fork"):
                break
            if turn.status not in {"completed", "failed", "cancelled"}:
                raise OctopError.localized(ErrorCode.TASK_RETRY_UNAVAILABLE)
            copying.turn(turn, body.operation)
        continuation_attachments: list[str] = []
        if body.operation in {"edit", "regenerate"}:
            original = json.loads(anchor.configuration_json)
            if original.get("imported_history", {}).get("missing_user_attachments"):
                raise OctopError.localized(ErrorCode.TASK_ATTACHMENT_UNAVAILABLE)
            continuation_attachments = [
                copying.attachment(item).attachment_id
                for item in original.get("attachment_ids", [])
            ]
            if not continuation and not continuation_attachments:
                raise OctopError.localized(ErrorCode.TASK_RETRY_UNAVAILABLE)
        receipt = ProjectTaskBranchRow(
            task_id,
            owner_id,
            body.request_id,
            fingerprint,
            source_task_id,
            anchor.turn_id,
            body.operation,
            continuation,
            json.dumps(continuation_attachments),
            now,
        )
        branches.insert(conn, receipt)
        return _result(receipt)
