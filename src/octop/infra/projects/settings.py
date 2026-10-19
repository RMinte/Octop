"""Global task-process defaults from the persisted environment file."""

from __future__ import annotations

import os
from collections.abc import Mapping

from pydantic import Field

from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects.models import ProjectModel
from octop.infra.utils.env_file import env_file_path, load_env_file

ACTION_TIMELINE_ENV = "OCTOP_TASK_ACTION_TIMELINE_ENABLED"
STEP_PANEL_ENV = "OCTOP_TASK_STEP_PANEL_ENABLED"
_TRUE_VALUES = frozenset({"true", "1", "yes", "on"})
_FALSE_VALUES = frozenset({"false", "0", "no", "off"})


class TaskSettings(ProjectModel):
    action_timeline_enabled: bool = Field(
        default=True, description="新轮次的结构化动作链路默认开关；已有轮次保留入队快照。"
    )
    step_panel_enabled: bool = Field(
        default=True, description="客户端步骤面板的全局显示默认值，不改变计划生成或执行。"
    )


def _boolean(values: Mapping[str, str], key: str) -> bool:
    if key not in values:
        return True
    value = values[key].strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    raise OctopError.localized(
        ErrorCode.TASK_SETTINGS_INVALID, details={"setting": key}, setting=key
    )


def validate_env_values(values: Mapping[str, str]) -> None:
    """Reject invalid task switches before saving any environment-file changes."""
    for key in (ACTION_TIMELINE_ENV, STEP_PANEL_ENV):
        _boolean(values, key)


def load_settings(services: SharedServices) -> TaskSettings:
    """Read shared file defaults afresh, ignoring stale worker environment values.

    Once the persisted file exists, absent switches use the documented defaults.
    Process environment is a startup-only fallback for installations without it.
    """
    path = env_file_path(services.paths.root)
    values = load_env_file(path) if path.exists() else os.environ
    return TaskSettings(
        action_timeline_enabled=_boolean(values, ACTION_TIMELINE_ENV),
        step_panel_enabled=_boolean(values, STEP_PANEL_ENV),
    )
