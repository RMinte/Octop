"""ACP setup checks observe the host without implicit package downloads."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from acp import PROTOCOL_VERSION, RequestError

from octop.infra.agents import acp_setup
from octop.infra.agents.acp_setup import ACPSetupService, _CheckClient
from octop.infra.agents.settings.acp import ACPSettingsStore
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.agents import AgentRepo
from octop.infra.db.repos.settings import SettingsRepo


@pytest.fixture
def setup(tmp_path, monkeypatch):
    db = SqlitePool(tmp_path / "setup.db")
    run_migrations(db)
    store = ACPSettingsStore(settings_repo=SettingsRepo(db), agents_repo=AgentRepo(db))
    service = ACPSetupService(store, tmp_path)
    monkeypatch.setattr(acp_setup, "tr", lambda key, locale: f"{locale}:{key}")
    monkeypatch.setattr(acp_setup, "runtime_environment", lambda root, env: dict(env))
    monkeypatch.setattr(
        acp_setup, "ensure_node", AsyncMock(side_effect=lambda root, env, progress: env)
    )
    installed = {"node", "npm", "opencode"}

    def find(command, env):
        if command in installed:
            return str(tmp_path / "bin" / command)
        return command if Path(command).is_absolute() and Path(command).name in installed else None

    monkeypatch.setattr(acp_setup, "_find_command", find)
    yield service, store, installed
    db.close()


@pytest.mark.asyncio
async def test_view_is_passive_and_npx_is_not_an_installed_runner(setup, monkeypatch):
    service, _, installed = setup
    installed.add("npx")
    spawn = AsyncMock(side_effect=AssertionError("GET must not spawn processes"))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    view = await service.view(1, "zh")
    assert view.host
    assert view.runners["opencode"].installation == "installed"
    assert view.runners["opencode"].connection == "unchecked"
    assert view.runners["codex"].installation == "not_installed"
    assert view.runners["codex"].can_install
    assert "codex-acp" in view.runners["codex"].install_command
    spawn.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_dependencies_and_custom_profiles_are_explicit(setup):
    service, store, installed = setup
    installed.clear()
    store.save_runners(1, {"custom": {"command": "npx", "args": ["-y", "unreviewed-package"]}})
    view = await service.view(1, "en")
    assert view.runners["codex"].installation == "missing_dependency"
    assert view.runners["codex"].can_install
    assert view.runners["custom"].installation == "manual"
    assert not view.runners["custom"].can_install
    assert view.runners["cursor_cli"].docs_url


@pytest.mark.asyncio
async def test_check_persists_per_user_and_invalidates_launch_changes(setup, monkeypatch):
    service, store, _ = setup
    probe = AsyncMock()
    monkeypatch.setattr(service, "_probe", probe)
    checked = await service.check(1, "opencode", "zh")
    assert checked.connection == "ready" and checked.checked_at
    assert not checked.enabled
    restarted = ACPSetupService(store, service._root.parent.parent)
    assert (await restarted.view(1, "zh")).runners["opencode"].connection == "ready"
    assert (await service.view(2, "zh")).runners["opencode"].connection == "unchecked"
    runners = store.load_runners(1)
    runners["opencode"]["enabled"] = True
    store.save_runners(1, runners)
    enabled = (await service.view(1, "zh")).runners["opencode"]
    assert enabled.enabled and enabled.connection == "ready"
    runners["opencode"]["env"] = {"TOKEN": "new-secret"}
    store.save_runners(1, runners)
    changed = (await service.view(1, "zh")).runners["opencode"]
    assert changed.connection == "unchecked" and changed.checked_at is None
    raw = json.dumps(store.load_setup_status(1, "opencode"))
    assert "new-secret" not in raw


def test_runner_environment_is_owner_scoped_and_never_adopts_host_credentials(
    setup, monkeypatch, tmp_path
):
    service, store, _ = setup
    host_home = tmp_path / "host-home"
    host_home.mkdir()
    (host_home / "auth.json").write_text("real-host-token")
    monkeypatch.setenv("HOME", str(host_home))
    monkeypatch.setenv("OPENAI_API_KEY", "host-provider-secret")
    monkeypatch.setenv("SSH_AUTH_SOCK", "host-ssh-agent")
    monkeypatch.setenv("HTTPS_PROXY", "https://proxy.invalid")
    config = {
        "env": {
            "OPENAI_API_KEY": "owner-provider-secret",
            "HOME": str(host_home),
            "CODEX_HOME": str(host_home),
            "XDG_CONFIG_HOME": str(host_home),
        }
    }
    first = service.runner_environment(1, "codex", config)
    other_owner = service.runner_environment(2, "codex", {})
    other_runner = service.runner_environment(1, "claude_code", {})
    assert len({first["HOME"], other_owner["HOME"], other_runner["HOME"]}) == 3
    assert first["OPENAI_API_KEY"] == "owner-provider-secret"
    assert "OPENAI_API_KEY" not in other_owner and "SSH_AUTH_SOCK" not in first
    assert first["HTTPS_PROXY"] == "https://proxy.invalid"
    assert not Path(first["HOME"]).exists()
    created = service.runner_environment(1, "codex", config, ensure=True)
    assert created == first and Path(created["HOME"]).is_dir()
    assert created["CODEX_HOME"] != str(host_home)
    assert created["XDG_CONFIG_HOME"] != str(host_home)
    assert str(service._root / "codex" / "node_modules" / ".bin") in created["PATH"]
    assert not (Path(created["HOME"]) / "auth.json").exists()
    assert (host_home / "auth.json").read_text() == "real-host-token"


async def test_old_host_login_check_is_not_reused_without_owner_auth_scope(setup, monkeypatch):
    service, store, _ = setup
    config = store.load_runners(1)["opencode"]
    store.save_setup_status(
        1, "opencode", {"fingerprint": acp_setup._fingerprint(config), "connection": "ready"}
    )
    assert (await service.view(1, "en")).runners["opencode"].connection == "unchecked"
    probe = AsyncMock()
    monkeypatch.setattr(service, "_probe", probe)
    assert (await service.check(1, "opencode", "en")).connection == "ready"
    assert store.load_setup_status(1, "opencode")["auth_scope_version"] == 1
    checked_env = probe.await_args.args[1]
    assert checked_env["HOME"] == service.runner_environment(1, "opencode", config)["HOME"]


def test_legacy_environment_overrides_harness_parent_overlay_without_dropping_owner_dotenv(
    setup, monkeypatch
):
    from octop_harness.runtime_env import overlay_env

    service, _, _ = setup
    monkeypatch.setenv("OPENAI_API_KEY", "host-openai-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "host-anthropic-secret")
    monkeypatch.setenv("SSH_AUTH_SOCK", "host-ssh-agent")
    workspace_env = {
        "OPENAI_API_KEY": "owner-dotenv-secret",
        "PROJECT_VAR": "owner-project",
        "HOME": "other-home",
    }
    config = {"env": {"ANTHROPIC_API_KEY": "owner-runner-secret"}}
    runner_env = service.legacy_runner_environment(1, "codex", config, workspace_env=workspace_env)
    simulated_harness_env = overlay_env(
        overlay_env(os.environ, workspace_env, protect=True), runner_env, protect=False
    )
    assert simulated_harness_env["OPENAI_API_KEY"] == "owner-dotenv-secret"
    assert simulated_harness_env["ANTHROPIC_API_KEY"] == "owner-runner-secret"
    assert simulated_harness_env["PROJECT_VAR"] == "owner-project"
    assert simulated_harness_env["SSH_AUTH_SOCK"] == ""
    assert simulated_harness_env["HOME"] == service.runner_environment(1, "codex", config)["HOME"]
    assert "host-openai-secret" not in simulated_harness_env.values()
    assert "host-anthropic-secret" not in simulated_harness_env.values()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected", "message"),
    [
        (RequestError.auth_required(), "auth_required", "auth_required"),
        (RequestError(-32603, "Authentication failed: secret-value"), "error", "check_failed"),
        (TimeoutError(), "error", "check_timeout"),
    ],
)
async def test_check_only_maps_protocol_auth_required_and_hides_raw_errors(
    setup, monkeypatch, error, expected, message
):
    service, _, _ = setup
    monkeypatch.setattr(service, "_probe", AsyncMock(side_effect=error))
    result = await service.check(1, "opencode", "en")
    assert result.connection == expected
    assert result.message == f"en:acp_setup.{message}"
    assert "secret-value" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_check_does_not_save_observation_for_a_changed_configuration(setup, monkeypatch):
    service, store, _ = setup

    async def changed(*args):
        runners = store.load_runners(1)
        runners["opencode"]["args"] = ["acp", "--new"]
        store.save_runners(1, runners)

    monkeypatch.setattr(service, "_probe", changed)
    result = await service.check(1, "opencode", "en")
    assert result.connection == "unchecked"
    assert result.message == "en:acp_setup.config_changed"
    assert store.load_setup_status(1, "opencode") == {}


async def test_connect_check_rejects_changed_configuration_before_starting_process(
    setup, monkeypatch
):
    service, store, _ = setup
    expected = store.load_runners(1)["opencode"]
    current = store.load_runners(1)
    current["opencode"]["trusted"] = not expected["trusted"]
    store.save_runners(1, current)
    probe = AsyncMock()
    monkeypatch.setattr(service, "_probe", probe)

    result = await service.check(1, "opencode", "en", expected_configuration=expected)

    assert result.connection == "error"
    assert result.message == "en:acp_setup.config_changed"
    probe.assert_not_awaited()


async def test_connect_check_rejects_tool_policy_change_during_probe(setup, monkeypatch):
    service, store, _ = setup
    expected = store.load_runners(1)["opencode"]

    async def edit_tool_policy(*args):
        current = store.load_runners(1)
        current["opencode"]["trusted"] = not expected["trusted"]
        store.save_runners(1, current)

    monkeypatch.setattr(service, "_probe", edit_tool_policy)
    result = await service.check(1, "opencode", "en", expected_configuration=expected)

    assert result.connection == "error"
    assert result.message == "en:acp_setup.config_changed"
    assert store.load_setup_status(1, "opencode") == {}


async def test_enable_checked_requires_matching_private_ready_fact_and_preserves_options(
    setup, monkeypatch
):
    service, store, _ = setup
    runners = store.load_runners(1)
    runners["opencode"]["trusted"] = False
    runners["opencode"]["tool_parse_mode"] = "update_detail"
    runners["codex"]["env"] = {"OWNER_OPTION": "keep"}
    store.save_runners(1, runners)
    expected = store.load_runners(1)["opencode"]
    monkeypatch.setattr(service, "_probe", AsyncMock())
    checked = await service.check(1, "opencode", "en", expected_configuration=expected)
    assert checked.connection == "ready" and not checked.enabled
    assert "_configuration" not in checked.model_dump()

    enabled = await service.enable_checked(1, "opencode", "en", checked._configuration)

    assert enabled.connection == "ready" and enabled.enabled
    assert enabled.message == "en:acp_setup.connected"
    current = store.load_runners(1)
    assert not current["opencode"]["trusted"]
    assert current["opencode"]["tool_parse_mode"] == "update_detail"
    assert current["codex"]["env"] == {"OWNER_OPTION": "keep"}


@pytest.mark.parametrize("changed", ["configuration", "authorization", "auth_scope"])
async def test_enable_checked_rejects_stale_ready_fact(setup, monkeypatch, changed):
    service, store, _ = setup
    monkeypatch.setattr(service, "_probe", AsyncMock())
    checked = await service.check(1, "opencode", "en")
    expected = checked._configuration
    if changed == "configuration":
        runners = store.load_runners(1)
        runners["opencode"]["trusted"] = not runners["opencode"]["trusted"]
        store.save_runners(1, runners)
    else:
        observation = store.load_setup_status(1, "opencode")
        if changed == "authorization":
            observation["connection"] = "auth_required"
        else:
            observation["auth_scope_version"] = 0
        store.save_setup_status(1, "opencode", observation)

    result = await service.enable_checked(1, "opencode", "en", expected)

    assert result.connection == "error" and not result.enabled
    assert result.message == "en:acp_setup.config_changed"
    assert not store.load_runners(1)["opencode"]["enabled"]


@pytest.mark.asyncio
async def test_explicit_check_adopts_existing_local_adapter_without_npx(setup, monkeypatch):
    service, store, installed = setup
    installed.add("codex-acp")
    monkeypatch.setattr(service, "_check_auth", AsyncMock())
    probe = AsyncMock()
    monkeypatch.setattr(service, "_probe", probe)
    before = store.load_runners(1)["codex"]
    assert before["command"] == "npx"
    assert (await service.view(1, "en")).runners["codex"].connection == "unchecked"
    assert store.load_runners(1)["codex"]["command"] == "npx"
    result = await service.check(1, "codex", "en")
    saved = store.load_runners(1)["codex"]
    assert result.connection == "ready"
    assert Path(saved["command"]).name == "codex-acp"
    assert saved["args"] == []
    assert probe.await_args.args[0] == [saved["command"]]


def write_managed_script(service, runner="opencode"):
    spec = acp_setup.RUNNER_SETUP_SPECS[runner]
    package = spec.npm_packages[0].rsplit("@", 1)[0]
    directory = service._root / runner / "node_modules" / package
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "package.json").write_text(
        json.dumps({"bin": {spec.binary: "entry.js"}}), encoding="utf-8"
    )
    (directory / "entry.js").write_text("#!/usr/bin/env node\n", encoding="utf-8")


@pytest.mark.asyncio
async def test_install_uses_private_prefix_and_saves_direct_launch_without_enabling(
    setup, monkeypatch
):
    service, store, installed = setup
    installed.remove("opencode")
    runners = store.load_runners(1)
    runners["opencode"]["enabled"] = True
    store.save_runners(1, runners)
    calls = []

    async def install(command, env, on_output=None):
        calls.append(command)
        write_managed_script(service)

    monkeypatch.setattr(service, "_capture", AsyncMock(return_value=(0, b"v24.10.0\n")))
    monkeypatch.setattr(service, "_run_install", install)
    probe = AsyncMock()
    monkeypatch.setattr(service, "_probe", probe)
    result = await service.install(1, "opencode", "en")
    assert result.installation == "installed" and result.connection == "unchecked"
    assert result.message == "en:acp_setup.installed"
    probe.assert_not_awaited()
    assert not result.enabled
    command = calls[0]
    assert "--prefix" in command and "--global" not in command and "-g" not in command
    assert command[command.index("--prefix") + 1] == str(service._root / "opencode")
    saved = store.load_runners(1)["opencode"]
    assert saved["enabled"] is False
    assert Path(saved["command"]).name == "node"
    assert saved["args"][-1] == "acp"
    assert str(service._root / "opencode" / "node_modules" / ".bin") in saved["env"]["PATH"]
    assert "node" in result.login_command and "auth login" in result.login_command
    assert (service._root / "opencode" / ".octop-install-complete").is_file()
    assert (await service.check(1, "opencode", "en")).connection == "ready"


@pytest.mark.asyncio
async def test_second_user_reuses_complete_managed_installation(setup, monkeypatch):
    service, store, installed = setup
    installed.remove("opencode")

    async def install(command, env, on_output=None):
        write_managed_script(service)

    monkeypatch.setattr(service, "_run_install", install)
    await service.install(1, "opencode", "en")
    runners = store.load_runners(1)
    runners["opencode"]["enabled"] = True
    store.save_runners(1, runners)
    original = store.load_runners(1)["opencode"]
    marker = service._root / "opencode" / ".octop-install-complete"
    ensure_node = AsyncMock(side_effect=AssertionError("Do not reinstall a complete runtime"))
    npm = AsyncMock(side_effect=AssertionError("Do not mutate a shared complete installation"))
    monkeypatch.setattr(acp_setup, "ensure_node", ensure_node)
    monkeypatch.setattr(service, "_run_install", npm)

    result = await service.install(2, "opencode", "en")

    assert result.installation == "installed" and not result.enabled
    assert store.load_runners(1)["opencode"] == original
    assert store.load_runners(2)["opencode"]["command"] == original["command"]
    assert store.load_runners(2)["opencode"]["args"] == original["args"]
    assert marker.is_file()
    ensure_node.assert_not_awaited()
    npm.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["marker", "package"])
async def test_known_managed_profile_remains_repairable_after_damage(setup, monkeypatch, damage):
    service, store, installed = setup
    installed.remove("opencode")

    async def install(command, env, on_output=None):
        write_managed_script(service)

    monkeypatch.setattr(service, "_run_install", install)
    monkeypatch.setattr(service, "_probe", AsyncMock())
    await service.install(1, "opencode", "en")
    await service.check(1, "opencode", "en")
    expected_launch = store.load_setup_status(1, "opencode")["managed_launch"]
    prefix = service._root / "opencode"
    if damage == "marker":
        (prefix / ".octop-install-complete").unlink()
    else:
        shutil.rmtree(prefix / "node_modules")

    status = (await service.view(1, "en")).runners["opencode"]
    assert status.installation == "not_installed" and status.can_install
    assert status.connection == "unchecked"
    failed = AsyncMock(side_effect=TimeoutError())
    monkeypatch.setattr(service, "_run_install", failed)
    result = await service.install(1, "opencode", "en")
    assert result.connection == "error"
    assert (await service.view(1, "en")).runners["opencode"].can_install
    assert store.load_setup_status(1, "opencode")["managed_launch"] == expected_launch

    monkeypatch.setattr(service, "_run_install", install)
    result = await service.install(1, "opencode", "en")
    assert result.installation == "installed" and not result.enabled
    assert result.connection == "unchecked"


@pytest.mark.asyncio
async def test_managed_receipt_does_not_allow_overwriting_a_customized_launch(setup, monkeypatch):
    service, store, installed = setup
    installed.remove("opencode")

    async def install(command, env, on_output=None):
        write_managed_script(service)

    monkeypatch.setattr(service, "_run_install", install)
    await service.install(1, "opencode", "en")
    runners = store.load_runners(1)
    runners["opencode"]["args"] = [str(service._root / "opencode" / "custom.js"), "acp"]
    store.save_runners(1, runners)
    custom = store.load_runners(1)["opencode"]
    npm = AsyncMock()
    monkeypatch.setattr(service, "_run_install", npm)

    result = await service.install(1, "opencode", "en")

    assert not result.can_install and result.connection == "error"
    assert store.load_runners(1)["opencode"] == custom
    npm.assert_not_awaited()


@pytest.mark.asyncio
async def test_runtime_setup_failure_does_not_run_npm(setup, monkeypatch):
    service, _, _ = setup
    monkeypatch.setattr(
        acp_setup, "ensure_node", AsyncMock(side_effect=RuntimeError("runtime failed"))
    )
    install = AsyncMock()
    monkeypatch.setattr(service, "_run_install", install)
    result = await service.install(1, "opencode", "en")
    assert result.connection == "error"
    install.assert_not_awaited()


@pytest.mark.asyncio
async def test_box_can_install_cursor_and_node_without_host_prerequisites(setup):
    service, _, installed = setup
    installed.clear()
    status = await service.view(1, "en")
    assert status.runners["cursor_cli"].can_install
    assert status.runners["codex"].can_install


@pytest.mark.asyncio
async def test_web_credentials_disable_runner_and_never_echo_secret(setup):
    service, store, _ = setup
    runners = store.load_runners(1)
    runners["pi"]["enabled"] = True
    store.save_runners(1, runners)
    result = await service.save_credentials(1, "pi", "google", "private-key-value", "en")
    assert not result.enabled and result.connection == "unchecked"
    assert "private-key-value" not in result.model_dump_json()
    saved = store.load_runners(1)["pi"]
    assert saved["env"]["GEMINI_API_KEY"] == "private-key-value"
    assert saved["env"]["OPENAI_API_KEY"] == ""
    assert all(option.id != "GOOGLE_API_KEY" for option in result.credential_options)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider,region", [("codebuddy_cn", "internal"), ("codebuddy_global", "public")]
)
async def test_codebuddy_credential_region_overrides_host_default(setup, provider, region):
    service, store, _ = setup
    await service.save_credentials(1, "codebuddy", provider, "test-region-key", "en")
    env = store.load_runners(1)["codebuddy"]["env"]
    assert env["CODEBUDDY_INTERNET_ENVIRONMENT"] == region
    assert env["CODEBUDDY_API_KEY"] == "test-region-key"


@pytest.mark.asyncio
async def test_manual_profile_never_runs_installer(setup, monkeypatch):
    service, store, _ = setup
    store.save_runners(1, {"codex": {"command": "my-codex", "args": ["custom"]}})
    install = AsyncMock()
    monkeypatch.setattr(service, "_run_install", install)
    result = await service.install(1, "codex", "en")
    assert result.installation == "manual"
    assert store.load_runners(1)["codex"]["command"] == "my-codex"
    install.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_install_does_not_make_partial_files_look_installed(setup, monkeypatch):
    service, store, installed = setup
    installed.remove("opencode")

    async def fail(command, env, on_output=None):
        write_managed_script(service)
        raise TimeoutError()

    monkeypatch.setattr(service, "_capture", AsyncMock(return_value=(0, b"v24.10.0\n")))
    monkeypatch.setattr(service, "_run_install", fail)
    result = await service.install(1, "opencode", "en")
    assert result.message == "en:acp_setup.install_timeout"
    assert store.load_runners(1)["opencode"]["command"] == "opencode"
    assert (await service.view(1, "en")).runners["opencode"].installation == "not_installed"


@pytest.mark.asyncio
async def test_duplicate_install_does_not_start_a_second_process(setup, monkeypatch):
    service, _, _ = setup
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def install(command, env, on_output=None):
        calls.append(command)
        started.set()
        await release.wait()
        write_managed_script(service)

    monkeypatch.setattr(service, "_capture", AsyncMock(return_value=(0, b"v24.10.0\n")))
    monkeypatch.setattr(service, "_run_install", install)
    first = asyncio.create_task(service.install(1, "opencode", "en"))
    await started.wait()
    duplicate = await service.install(1, "opencode", "en")
    assert duplicate.message == "en:acp_setup.busy"
    release.set()
    assert (await first).installation == "installed"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_batch_shims_are_not_launched_or_adopted(setup, monkeypatch, tmp_path):
    service, store, _ = setup

    def find(command, env):
        if command in {"codex-acp", "node", "npm"}:
            return str(tmp_path / f"{command}.cmd")
        return None

    monkeypatch.setattr(acp_setup, "_find_command", find)
    probe = AsyncMock()
    monkeypatch.setattr(service, "_probe", probe)
    await service.check(1, "codex", "en")
    assert store.load_runners(1)["codex"]["command"] == "npx"
    probe.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("runner", "code", "raw", "error"),
    [
        ("claude_code", 1, b'{"loggedIn": false}', RequestError),
        ("claude_code", 1, b"unknown option --cli", RuntimeError),
        ("claude_code", 0, b'{"loggedIn": true}', None),
        ("codex", 1, b"Not logged in\n", RequestError),
        ("codex", 1, b"Could not read configuration", RuntimeError),
    ],
)
async def test_auth_probes_require_reviewed_evidence(setup, monkeypatch, runner, code, raw, error):
    service, _, _ = setup
    monkeypatch.setattr(service, "_capture", AsyncMock(return_value=(code, raw)))
    call = service._check_auth(acp_setup.RUNNER_SETUP_SPECS[runner], ["local-adapter"], {})
    if error:
        with pytest.raises(error) as exc:
            await call
        if error is RequestError:
            assert exc.value.code == -32000
    else:
        await call


@pytest.mark.asyncio
async def test_probe_creates_no_prompt_and_always_closes_process(setup, monkeypatch, tmp_path):
    service, _, _ = setup
    connection = SimpleNamespace(
        initialize=AsyncMock(
            return_value=SimpleNamespace(
                protocol_version=PROTOCOL_VERSION,
                agent_capabilities=None,
                auth_methods=[{"id": "oauth"}],
            )
        ),
        new_session=AsyncMock(return_value=SimpleNamespace(session_id="setup")),
        prompt=AsyncMock(side_effect=AssertionError("setup must not send prompts")),
        close=AsyncMock(),
    )
    monkeypatch.setattr("acp.connect_to_agent", lambda *args: connection)
    process = SimpleNamespace(stdin=object(), stdout=object())
    spawn = AsyncMock(return_value=process)
    stop = AsyncMock()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(acp_setup, "_stop_process", stop)
    await service._probe(["local-adapter"], {})
    capability = connection.initialize.await_args.kwargs["client_capabilities"]
    assert not capability.terminal and not capability.fs.read_text_file
    directory = connection.new_session.await_args.kwargs["cwd"]
    assert not Path(directory).exists()
    connection.prompt.assert_not_awaited()
    connection.close.assert_awaited_once()
    stop.assert_awaited_once_with(process)
    assert await _CheckClient().request_permission() == {"outcome": {"outcome": "cancelled"}}
