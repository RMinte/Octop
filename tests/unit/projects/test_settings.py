"""Task defaults follow shared persisted configuration, not stale worker state."""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects.settings import (
    ACTION_TIMELINE_ENV,
    STEP_PANEL_ENV,
    load_settings,
    presentation_for_turn,
    preserve_env_values,
    validate_env_values,
)
from octop.infra.projects.tasks import TaskExecutionOptions
from octop.infra.utils.env_file import save_env_file


@pytest.mark.parametrize("value", ["true", "1", "yes", "on", " TrUe ", "YES"])
def test_true_values(value, tmp_path):
    save_env_file(tmp_path / "env", {ACTION_TIMELINE_ENV: value, STEP_PANEL_ENV: value})
    result = load_settings(SimpleNamespace(paths=SimpleNamespace(root=tmp_path)))
    assert result.action_timeline_enabled and result.step_panel_enabled


@pytest.mark.parametrize("value", ["false", "0", "no", "off", " FaLsE ", "NO"])
def test_false_values(value, tmp_path):
    save_env_file(tmp_path / "env", {ACTION_TIMELINE_ENV: value, STEP_PANEL_ENV: value})
    result = load_settings(SimpleNamespace(paths=SimpleNamespace(root=tmp_path)))
    assert not result.action_timeline_enabled and not result.step_panel_enabled


def test_absent_file_uses_process_environment_but_persisted_file_is_authoritative(
    tmp_path, monkeypatch
):
    services = SimpleNamespace(paths=SimpleNamespace(root=tmp_path))
    monkeypatch.setenv(ACTION_TIMELINE_ENV, "false")
    monkeypatch.setenv(STEP_PANEL_ENV, "false")
    assert load_settings(services).model_dump() == {
        "action_timeline_enabled": False,
        "step_panel_enabled": False,
    }
    save_env_file(tmp_path / "env", {ACTION_TIMELINE_ENV: "true"})
    assert load_settings(services).model_dump() == {
        "action_timeline_enabled": True,
        "step_panel_enabled": True,
    }
    save_env_file(tmp_path / "env", {})
    assert load_settings(services).action_timeline_enabled
    assert load_settings(services).step_panel_enabled


def test_missing_values_default_true_without_a_persisted_file(tmp_path, monkeypatch):
    monkeypatch.delenv(ACTION_TIMELINE_ENV, raising=False)
    monkeypatch.delenv(STEP_PANEL_ENV, raising=False)
    result = load_settings(SimpleNamespace(paths=SimpleNamespace(root=tmp_path)))
    assert result.action_timeline_enabled and result.step_panel_enabled


@pytest.mark.parametrize("key", [ACTION_TIMELINE_ENV, STEP_PANEL_ENV])
@pytest.mark.parametrize("value", ["", "enabled", "2", "unexpected private text"])
def test_invalid_switch_values_do_not_disclose_the_value(key, value):
    with pytest.raises(OctopError) as error:
        validate_env_values({key: value})
    assert error.value.code == ErrorCode.TASK_SETTINGS_INVALID
    assert error.value.details == {"setting": key}
    assert value not in str(error.value) or value == ""


def test_unrelated_environment_values_are_not_boolean_validated():
    validate_env_values({"UNRELATED_SETTING": "arbitrary"})


@pytest.mark.parametrize("env_file_exists", [False, True])
def test_env_replacement_keeps_implicit_defaults_out_of_the_file(monkeypatch, env_file_exists):
    monkeypatch.delenv(ACTION_TIMELINE_ENV, raising=False)
    monkeypatch.delenv(STEP_PANEL_ENV, raising=False)
    monkeypatch.setenv("UNRELATED_SETTING", "process value")
    assert preserve_env_values(
        {"OTHER": "replacement"}, {"REMOVED": "old value"}, env_file_exists=env_file_exists
    ) == {"OTHER": "replacement"}


@pytest.mark.parametrize("value", ["true", 1, 0, [], {}])
def test_per_turn_override_requires_an_actual_boolean(value):
    with pytest.raises(ValidationError):
        TaskExecutionOptions(action_timeline_enabled=value)


def test_per_turn_override_is_optional_and_does_not_become_project_configuration():
    assert TaskExecutionOptions().action_timeline_enabled is None
    assert TaskExecutionOptions(action_timeline_enabled=False).action_timeline_enabled is False


@pytest.mark.parametrize("actions", [True, False])
def test_history_without_a_snapshot_uses_fixed_compatibility_values(actions):
    configuration = {"action_timeline_enabled": actions}
    assert presentation_for_turn(configuration).model_dump() == {
        "action_timeline_enabled": actions,
        "step_panel_enabled": True,
    }
    assert configuration == {"action_timeline_enabled": actions}


def test_saved_presentation_is_independent_of_generation_and_legacy_defaults():
    assert presentation_for_turn(
        {
            "action_timeline_enabled": True,
            "presentation": {"action_timeline_enabled": False, "step_panel_enabled": False},
        }
    ).model_dump() == {"action_timeline_enabled": False, "step_panel_enabled": False}
