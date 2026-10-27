"""tests/unit/test_acp_settings.py — user-scoped global ACP runner settings."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from octop.infra.agents.settings.acp import ACPSettingsStore
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.agents import AgentRepo
from octop.infra.db.repos.settings import SettingsRepo
from octop.infra.db.repos.users import UserRepo


@pytest.fixture
def acp_store(tmp_path: Path) -> tuple[ACPSettingsStore, AgentRepo, int]:
    db = SqlitePool(tmp_path / "test.db")
    run_migrations(db)
    settings = SettingsRepo(db)
    agents = AgentRepo(db)
    user_id = UserRepo(db).create(username="u", password_hash="h", role="user")
    store = ACPSettingsStore(settings_repo=settings, agents_repo=agents)
    return store, agents, user_id


def test_load_defaults_include_codebuddy(
    acp_store: tuple[ACPSettingsStore, AgentRepo, int],
) -> None:
    store, _, user_id = acp_store
    runners = store.load_runners(user_id=user_id)
    assert "opencode" in runners
    assert "codebuddy" in runners
    assert "kimi_code" in runners
    assert "cursor_cli" in runners
    assert "pi" in runners
    assert runners["kimi_code"]["command"] == "kimi"
    assert runners["kimi_code"]["args"] == ["acp"]
    assert runners["cursor_cli"]["command"] == "agent"
    assert runners["cursor_cli"]["args"] == ["acp"]
    assert runners["pi"]["command"] == "npx"
    assert runners["pi"]["args"] == ["-y", "pi-acp"]
    assert "qwen_code" not in runners


def test_save_and_load_round_trip(acp_store: tuple[ACPSettingsStore, AgentRepo, int]) -> None:
    store, _, user_id = acp_store
    saved = store.save_runners(
        user_id,
        {
            "opencode": {
                "enabled": True,
                "command": "opencode",
                "args": ["acp"],
                "env": {},
                "trusted": True,
                "tool_parse_mode": "update_detail",
                "stdio_buffer_limit_bytes": 52428800,
            },
        },
    )
    assert saved["opencode"]["enabled"] is True
    reloaded = store.load_runners(user_id)
    assert reloaded["opencode"]["enabled"] is True


def test_migrate_legacy_per_agent_runners(
    acp_store: tuple[ACPSettingsStore, AgentRepo, int],
) -> None:
    store, agents, user_id = acp_store
    legacy = {
        "acp": {
            "tool_enabled": True,
            "runners": {
                "opencode": {
                    "enabled": True,
                    "command": "opencode",
                    "args": ["acp"],
                    "env": {},
                    "trusted": True,
                    "tool_parse_mode": "update_detail",
                    "stdio_buffer_limit_bytes": 52428800,
                },
            },
        },
    }
    agents.create(
        agent_id="bot1",
        user_id=user_id,
        name="bot",
        config_json=json.dumps(legacy),
    )
    runners = store.load_runners(user_id)
    assert runners["opencode"]["enabled"] is True
    row = agents.get("bot1")
    assert row is not None
    cfg = json.loads(row.config_json or "{}")
    assert "runners" not in (cfg.get("acp") or {})
    assert cfg["acp"]["tool_enabled"] is True


def test_conditional_runner_update_starts_from_unstored_defaults(acp_store) -> None:
    store, _, user_id = acp_store
    expected = store.load_runners(user_id)["codex"]
    assert store._settings.get(f"acp_runners:user:{user_id}") is None

    saved = store.update_runner_if_matches(user_id, "codex", expected, {"enabled": True})

    assert saved is not None and saved["enabled"]
    assert store.load_runners(user_id)["opencode"]["command"] == "opencode"
    assert store.load_runners(user_id)["codex"] == {**expected, "enabled": True}


def test_conditional_runner_update_retries_without_losing_peer_changes(acp_store, monkeypatch):
    store, _, user_id = acp_store
    runners = store.load_runners(user_id)
    runners["codex"]["trusted"] = False
    runners["codex"]["tool_parse_mode"] = "update_detail"
    store.save_runners(user_id, runners)
    expected = store.load_runners(user_id)["codex"]
    compare_and_set = store._settings.compare_and_set
    attempts = 0

    def concurrent_update(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            latest = store.load_runners(user_id)
            latest["opencode"]["env"] = {"PROJECT_OPTION": "peer-change"}
            latest["opencode"]["trusted"] = False
            store.save_runners(user_id, latest)
        return compare_and_set(*args, **kwargs)

    monkeypatch.setattr(store._settings, "compare_and_set", concurrent_update)
    saved = store.update_runner_if_matches(user_id, "codex", expected, {"enabled": True})

    assert attempts == 2
    assert saved is not None and saved["enabled"] and not saved["trusted"]
    current = store.load_runners(user_id)
    assert current["opencode"]["env"] == {"PROJECT_OPTION": "peer-change"}
    assert not current["opencode"]["trusted"]


def test_conditional_runner_update_preserves_concurrently_changed_target(acp_store, monkeypatch):
    store, _, user_id = acp_store
    expected = store.load_runners(user_id)["codex"]
    compare_and_set = store._settings.compare_and_set

    def concurrent_update(*args, **kwargs):
        latest = store.load_runners(user_id)
        latest["codex"]["env"] = {"OWNER_OPTION": "new-value"}
        store.save_runners(user_id, latest)
        return compare_and_set(*args, **kwargs)

    monkeypatch.setattr(store._settings, "compare_and_set", concurrent_update)
    assert store.update_runner_if_matches(user_id, "codex", expected, {"enabled": True}) is None
    current = store.load_runners(user_id)["codex"]
    assert not current["enabled"]
    assert current["env"] == {"OWNER_OPTION": "new-value"}


def test_conditional_runner_update_rejects_stale_connection_observation(acp_store, monkeypatch):
    store, _, user_id = acp_store
    expected = store.load_runners(user_id)["codex"]
    observation = {"connection": "ready", "checked_at": 1}
    store.save_setup_status(user_id, "codex", observation)
    compare_and_set = store._settings.compare_and_set

    def expire_observation(*args, **kwargs):
        store.save_setup_status(user_id, "codex", {"connection": "auth_required"})
        return compare_and_set(*args, **kwargs)

    monkeypatch.setattr(store._settings, "compare_and_set", expire_observation)
    saved = store.update_runner_if_matches(
        user_id, "codex", expected, {"enabled": True}, expected_status=observation
    )
    assert saved is None
    assert not store.load_runners(user_id)["codex"]["enabled"]
    assert store.load_setup_status(user_id, "codex")["connection"] == "auth_required"


def test_settings_compare_and_set_checks_existing_and_missing_values(acp_store):
    store, _, _ = acp_store
    repo = store._settings
    assert repo.compare_and_set("new-setting", None, "initial")
    assert not repo.compare_and_set("new-setting", None, "overwrite")
    repo.set("connection", "ready")
    assert not repo.compare_and_set(
        "new-setting", "initial", "enabled", checks={"connection": "auth_required"}
    )
    assert repo.get("new-setting") == "initial"
    assert repo.compare_and_set("new-setting", "initial", "enabled", checks={"connection": "ready"})
    assert repo.get("new-setting") == "enabled"


def test_postgres_compare_and_set_locks_all_existing_guards_in_key_order():
    database = MagicMock()
    database.dialect = "postgresql"
    connection = database.transaction.return_value.__enter__.return_value
    connection.execute.return_value.fetchone.side_effect = [
        {"value": "ready"},
        {"value": "disabled"},
    ]
    connection.execute.return_value.rowcount = 1
    repo = SettingsRepo(database)

    assert repo.compare_and_set("z-config", "disabled", "enabled", checks={"a-status": "ready"})

    calls = connection.execute.call_args_list
    assert calls[0].args == (
        "SELECT value FROM settings WHERE key=? FOR UPDATE",
        ("a-status",),
    )
    assert calls[1].args == (
        "SELECT value FROM settings WHERE key=? FOR UPDATE",
        ("z-config",),
    )
    assert calls[2].args == (
        "UPDATE settings SET value=? WHERE key=? AND value=?",
        ("enabled", "z-config", "disabled"),
    )
