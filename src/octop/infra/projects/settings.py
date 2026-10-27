"""Global task-process defaults from the persisted environment file."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from pydantic import Field, StrictBool

from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects.models import ProjectModel
from octop.infra.utils.env_file import env_file_path, load_env_file, save_env_file

ACTION_TIMELINE_ENV = "OCTOP_TASK_ACTION_TIMELINE_ENABLED"
STEP_PANEL_ENV = "OCTOP_TASK_STEP_PANEL_ENABLED"
_TRUE_VALUES = frozenset({"true", "1", "yes", "on"})
_FALSE_VALUES = frozenset({"false", "0", "no", "off"})


class TaskPresentation(ProjectModel):
    action_timeline_enabled: bool = Field(
        default=True, description="本轮固定的动作链路展示方式，不随之后的全局设置改变。"
    )
    step_panel_enabled: bool = Field(
        default=True, description="本轮固定的步骤面板显示方式，不改变计划生成或执行。"
    )


class TaskSettings(ProjectModel):
    action_timeline_enabled: bool = Field(
        default=True, description="新轮次的动作生成及展示默认值；已有轮次保留入队快照。"
    )
    step_panel_enabled: bool = Field(
        default=True, description="新轮次的步骤面板显示默认值；已有轮次保留入队快照。"
    )


class TaskSettingsUpdate(ProjectModel):
    action_timeline_enabled: StrictBool = Field(
        description="新轮次是否生成公开进展及动作说明，并默认展示动作链路。"
    )
    step_panel_enabled: StrictBool = Field(
        description="新轮次是否默认显示已有计划的步骤面板，不改变计划生成或执行。"
    )


def presentation_for_turn(configuration: Mapping[str, Any]) -> TaskPresentation:
    """Read immutable preferences, using fixed defaults for pre-snapshot history."""
    if configuration.get("presentation") is not None:
        return TaskPresentation.model_validate(configuration["presentation"])
    return TaskPresentation(
        action_timeline_enabled=configuration.get("action_timeline_enabled", True),
        step_panel_enabled=True,
    )


def snapshot_presentation(
    services: SharedServices, action_timeline_enabled: bool | None = None
) -> TaskPresentation:
    defaults = load_settings(services)
    return TaskPresentation(
        action_timeline_enabled=(
            action_timeline_enabled
            if action_timeline_enabled is not None
            else defaults.action_timeline_enabled
        ),
        step_panel_enabled=defaults.step_panel_enabled,
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


def preserve_env_values(
    values: Mapping[str, str], previous: Mapping[str, str], *, env_file_exists: bool
) -> dict[str, str]:
    """Keep omitted task settings when replacing the editable environment list."""
    source = previous if env_file_exists else os.environ
    retained = {key: source[key] for key in (ACTION_TIMELINE_ENV, STEP_PANEL_ENV) if key in source}
    return {**retained, **values}


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


def save_settings(services: SharedServices, settings: TaskSettingsUpdate) -> TaskSettings:
    """Save the two task defaults without replacing unrelated environment values."""
    path = env_file_path(services.paths.root)
    values = load_env_file(path)
    updates = {
        ACTION_TIMELINE_ENV: "true" if settings.action_timeline_enabled else "false",
        STEP_PANEL_ENV: "true" if settings.step_panel_enabled else "false",
    }
    save_env_file(path, {**values, **updates})
    os.environ.update(updates)
    return TaskSettings(**settings.model_dump())
