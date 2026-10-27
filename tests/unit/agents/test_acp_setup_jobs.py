"""Setup jobs retain only owner-scoped, sanitized progress and fixed login processes."""

from __future__ import annotations

import asyncio
import os
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from octop.infra.agents import acp_setup_jobs as module
from octop.infra.agents.acp_setup import ACPRunnerSetup, ACPSetupService
from octop.infra.agents.acp_setup_jobs import ACPSetupJob, ACPSetupJobs, _Job, _safe_url
from octop.infra.errors import ErrorCode, OctopError


def setup_result(
    connection="unchecked", installation="installed", *, configuration=None, enabled=False
):
    result = ACPRunnerSetup(
        runner="codex", installation=installation, connection=connection, enabled=enabled
    )
    result._configuration = dict(configuration) if configuration is not None else None
    return result


@pytest.fixture
def jobs(monkeypatch):
    monkeypatch.setattr(module, "tr", lambda key, locale: f"{locale}:{key}")
    service = Mock(spec=ACPSetupService)
    service._load.return_value = {"command": "codex-acp", "args": [], "env": {}}
    service._is_standard.return_value = True
    service._launch.return_value = ["codex-acp"]
    service._env.return_value = {"PATH": "opaque-bin-path"}
    service.runner_environment.return_value = {"PATH": "opaque-bin-path"}
    service._managed_environment.side_effect = lambda runner, env: dict(env)
    service.install = AsyncMock(return_value=setup_result())
    service.check = AsyncMock(return_value=setup_result("ready"))
    return ACPSetupJobs(service)


async def finish(jobs, job):
    await jobs._jobs[job.job_id].task
    return jobs.get(1, job.job_id)


@pytest.mark.asyncio
async def test_install_is_background_and_reload_runs_once_only_on_success(jobs):
    completed = Mock()
    jobs._on_complete = completed
    started = await jobs.start(1, "codex", "install", "en")
    assert started.status == "running"
    result = await finish(jobs, started)
    assert result.status == "succeeded" and result.setup.installation == "installed"
    assert result.phase == "completed" and result.input_mode == "none"
    completed.assert_called_once_with(1)
    jobs.get(1, started.job_id)
    jobs.active(1)
    completed.assert_called_once_with(1)


@pytest.mark.asyncio
async def test_failed_install_does_not_reload(jobs):
    completed = Mock()
    jobs._on_complete = completed
    jobs._service.install.return_value = setup_result("error", "not_installed")
    started = await jobs.start(1, "codex", "install", "en")
    assert (await finish(jobs, started)).status == "failed"
    completed.assert_not_called()


@pytest.mark.asyncio
async def test_owner_is_required_for_every_job_operation(jobs):
    wait = asyncio.Event()

    async def blocked(*args, **kwargs):
        await wait.wait()
        return setup_result()

    jobs._service.install.side_effect = blocked
    started = await jobs.start(1, "codex", "install", "en")
    for operation in (
        lambda: jobs.get(2, started.job_id),
        lambda: jobs._owned(2, started.job_id),
    ):
        with pytest.raises(OctopError) as exc:
            operation()
        assert exc.value.code == ErrorCode.NOT_FOUND
    with pytest.raises(OctopError):
        await jobs.cancel(2, started.job_id)
    with pytest.raises(OctopError):
        await jobs.input(2, started.job_id, "secret\r")
    assert jobs.active(2) == {}
    assert jobs.active(1)["codex"].job_id == started.job_id
    assert (await jobs.cancel(1, started.job_id)).status == "cancelled"


@pytest.mark.asyncio
async def test_duplicate_start_reuses_same_owner_and_never_leaks_other_owners_job(jobs):
    wait = asyncio.Event()

    async def blocked(*args, **kwargs):
        await wait.wait()
        return setup_result()

    jobs._service.install.side_effect = blocked
    first = await jobs.start(1, "codex", "install", "en")
    second = await jobs.start(1, "codex", "install", "en")
    assert first.job_id == second.job_id
    with pytest.raises(OctopError):
        await jobs.start(2, "codex", "login", "en")
    await jobs.cancel(1, first.job_id)


@pytest.mark.asyncio
async def test_close_cancels_new_and_running_jobs_and_prevents_new_operations(jobs):
    started = await jobs.start(1, "codex", "install", "en")
    await jobs.close()
    assert jobs.get(1, started.job_id).status == "cancelled"
    assert jobs.active(1) == {}
    with pytest.raises(OctopError):
        await jobs.start(1, "codex", "install", "en")


@pytest.mark.parametrize("runner", ["pi", "codebuddy"])
@pytest.mark.asyncio
async def test_full_assistant_tui_is_never_started_for_login(jobs, monkeypatch, runner):
    spawn = AsyncMock(side_effect=AssertionError("must not run a full assistant"))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    started = await jobs.start(1, runner, "login", "zh")
    result = await finish(jobs, started)
    assert result.status == "failed"
    assert result.message == "zh:acp_setup.login_not_supported"
    spawn.assert_not_awaited()


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        ("https://auth.openai.com/codex/device", True),
        ("https://auth.openai.com.evil.example/device", False),
        ("http://localhost:1234/callback", False),
        ("https://auth.openai.com/device?device_code=secret", False),
        ("https://me:secret@auth.openai.com/device", False),
        ("https://auth.openai.com:444/device", False),
    ],
)
def test_authorization_url_allowlist(url, allowed):
    assert bool(_safe_url(url, "codex")) is allowed


def test_output_is_bounded_and_auth_fields_do_not_expose_credentials(jobs):
    job = _Job(1, "en", ACPSetupJob(job_id="test", runner="codex", kind="login"))
    job.secrets = ["configured-secret"]
    jobs._append(job, "\x1b[31mhttps://auth.openai.com/codex/device?user_code=ABCD-1234\x1b[0m\n")
    jobs._append(
        job,
        "User code: ABCD-1234\naccess_token=credential\nconfigured-secret\nAuthorization: Bearer another-secret-token\n",
    )
    assert job.view.authorization.url.startswith("https://auth.openai.com/")
    assert job.view.authorization.user_code == "ABCD-1234"
    assert job.view.authorization.expires_at
    assert "credential" not in job.view.output
    assert "configured-secret" not in job.view.output
    assert "another-secret-token" not in job.view.output
    assert "https://" not in job.view.output and "\x1b" not in job.view.output
    jobs._append(job, "x" * 50_000)
    assert len(job.view.output) <= module._OUTPUT_LIMIT


def test_codex_multiline_device_code_waits_for_complete_line_across_output_chunks(jobs):
    job = _Job(1, "en", ACPSetupJob(job_id="codex-device", runner="codex", kind="login"))
    jobs._append(job, "https://auth.openai.com/codex/device\n")
    prompt = (
        "2. Enter this one-time code (expires in 15 minutes)\r\n\r\n   \x1b[1mABCD-EFGHI\x1b[0m"
    )
    for character in prompt:
        jobs._append(job, character)
        assert job.view.authorization.user_code is None
    jobs._append(job, "\r\n")
    assert job.view.authorization.user_code == "ABCD-EFGHI"
    assert job.view.status == "waiting_for_user"
    jobs._append(job, "\nContinue only if you started this login in Codex.\n")
    assert job.view.authorization.user_code == "ABCD-EFGHI"
    jobs._append(job, "access_token=private-access-secret\nBearer private-bearer-secret\n")
    assert "https://" not in job.view.output
    assert "private-access-secret" not in job.view.output
    assert "private-bearer-secret" not in job.view.output


@pytest.mark.parametrize(
    "output",
    [
        "User code: Please sign in to your account\n",
        "Verification code:\n\nContinue only if you started this login.\n",
        "Enter this one-time code (expires in 15 minutes)\n\n   Continue in your browser.\n",
        "User code:\nPLEASE WAIT\n",
        "User code: CONTINUE\n",
        "Authorization code: ABCD-EFGHI-extra-suffix\n",
    ],
)
def test_device_code_parser_does_not_capture_instructions_or_partial_tokens(jobs, output):
    job = _Job(1, "en", ACPSetupJob(job_id="instructions", runner="codex", kind="login"))
    jobs._append(job, "https://auth.openai.com/codex/device\n" + output)
    assert job.view.authorization.user_code is None


@pytest.mark.parametrize(
    ("label", "code"),
    [
        ("User code:", "ABCD-1234"),
        ("verification code=", "123456"),
        ("Authorization code:", "AB12CD34"),
    ],
)
def test_single_line_device_codes_still_work_with_fragmented_labels_and_values(jobs, label, code):
    job = _Job(1, "en", ACPSetupJob(job_id="single-line", runner="codex", kind="login"))
    jobs._append(job, label[:4])
    jobs._append(job, label[4:] + " " + code[:3])
    jobs._append(job, code[3:] + "\nhttps://auth.openai.com/codex/device\n")
    assert job.view.authorization.user_code == code
    assert "https://" not in job.view.output


def test_install_output_redacts_http_and_npm_credentials(jobs):
    job = _Job(1, "en", ACPSetupJob(job_id="install", runner="codex", kind="install"))
    jobs._append(job, "Authorization: Basic dXNlcjpwYXNzd29yZA==\nBearer opaque-bearer-secret\n")
    jobs._append(
        job,
        "npm_1234567890abcdef\nNPM_TOKEN=plain-npm-secret\n//registry.npmjs.org/:_authToken=configured-npm-secret\n",
    )
    for secret in (
        "dXNlcjpwYXNzd29yZA==",
        "opaque-bearer-secret",
        "npm_1234567890abcdef",
        "plain-npm-secret",
        "configured-npm-secret",
    ):
        assert secret not in job.view.output


@pytest.mark.parametrize("status", ["succeeded", "failed", "cancelled"])
def test_late_progress_cannot_change_a_finished_job(jobs, status):
    job = _Job(
        1,
        "en",
        ACPSetupJob(job_id="finished", runner="codex", kind="install", status=status, phase=status),
    )
    jobs._progress(job, "installing_runner")
    jobs._progress(job, "late output")
    assert job.view.phase == status
    assert job.view.output == ""


@pytest.mark.asyncio
async def test_install_progress_callback_is_streamed_without_exposing_urls(jobs):
    async def install(*args, on_progress):
        on_progress("downloading_runtime")
        on_progress("https://example.test/?access_token=secret\napi_key=secret\n")
        await asyncio.sleep(0)
        return setup_result()

    jobs._service.install.side_effect = install
    started = await jobs.start(1, "codex", "install", "en")
    result = await finish(jobs, started)
    assert result.status == "succeeded"
    assert "https://" not in result.output and "api_key=secret" not in result.output


@pytest.mark.asyncio
async def test_fixed_login_launch_uses_catalog_arguments_and_headless_environment(jobs):
    view = ACPSetupJob(job_id="fixed", runner="codex", kind="login")
    command, env = jobs._login_launch(_Job(1, "en", view))
    assert command == ["codex-acp", "cli", "login", "--device-auth"]
    assert env["NO_OPEN_BROWSER"] == "1" and env["BROWSER"] == "none"
    jobs._service.runner_environment.assert_called_once_with(
        1, "codex", jobs._service._load.return_value, ensure=True
    )
    jobs._service._is_standard.return_value = False
    assert jobs._login_launch(_Job(1, "en", view)) is None


@pytest.mark.skipif(os.name != "posix", reason="restricted PTY login is POSIX-only")
@pytest.mark.asyncio
async def test_login_process_streams_url_accepts_owner_input_and_always_cleans_up(
    jobs, monkeypatch
):
    process = SimpleNamespace(wait=AsyncMock(return_value=0))
    held_slave = []

    def spawn_process(*args, **kwargs):
        held_slave.append(os.dup(kwargs["stdin"]))
        return process

    def cleanup(process):
        for descriptor in held_slave:
            os.close(descriptor)

    spawn = AsyncMock(side_effect=spawn_process)
    stop = AsyncMock(side_effect=cleanup)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(module, "_stop_process", stop)
    waiting, release = asyncio.Event(), asyncio.Event()
    count = 0

    async def read(descriptor):
        nonlocal count
        count += 1
        if count == 1:
            return b"https://auth.openai.com/codex/device\nUser code: ABCD-1234\n"
        waiting.set()
        await release.wait()
        return b""

    monkeypatch.setattr(jobs, "_read_pty", read)
    started = await jobs.start(1, "codex", "login", "en")
    await waiting.wait()
    current = jobs.get(1, started.job_id)
    assert current.status == "waiting_for_user" and current.input_mode == "terminal"
    assert current.authorization.user_code == "ABCD-1234"
    sent = await jobs.input(1, started.job_id, "authorization-code\r")
    assert sent.job_id == started.job_id
    release.set()
    result = await finish(jobs, started)
    assert result.status == "succeeded" and result.authorization is None
    assert result.input_mode == "none"
    stop.assert_awaited_once_with(process)
    argv = spawn.await_args.args
    assert argv == ("codex-acp", "cli", "login", "--device-auth")
    assert "shell" not in spawn.await_args.kwargs


@pytest.mark.skipif(os.name != "posix", reason="restricted PTY login is POSIX-only")
@pytest.mark.asyncio
async def test_cancelling_login_reaps_process_and_clears_authorization(jobs, monkeypatch):
    process = SimpleNamespace(wait=AsyncMock(return_value=0))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    stop = AsyncMock()
    monkeypatch.setattr(module, "_stop_process", stop)
    waiting = asyncio.Event()

    async def block(descriptor):
        waiting.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(jobs, "_read_pty", block)
    started = await jobs.start(1, "codex", "login", "en")
    await waiting.wait()
    result = await jobs.cancel(1, started.job_id)
    assert result.status == "cancelled" and result.input_mode == "none"
    assert result.authorization is None
    stop.assert_awaited_once_with(process)


def connect_setup(jobs, connection="ready", installation="installed"):
    config = jobs._service._load.return_value
    jobs._service._status.return_value = setup_result(
        installation=installation, configuration=config
    )
    jobs._service.check = AsyncMock(return_value=setup_result(connection, configuration=config))
    jobs._service.enable_checked = AsyncMock(
        return_value=setup_result("ready", configuration=config, enabled=True)
    )
    return config


async def test_connect_reuses_existing_authorization_checks_and_enables(jobs, monkeypatch):
    config = connect_setup(jobs)
    login = AsyncMock()
    monkeypatch.setattr(jobs, "_login", login)
    job = await jobs.start(1, "codex", "connect", "en")
    result = await finish(jobs, job)
    assert result.kind == "connect" and result.status == "succeeded"
    assert result.setup.connection == "ready" and result.setup.enabled
    jobs._service.check.assert_awaited_once_with(1, "codex", "en", expected_configuration=config)
    jobs._service.enable_checked.assert_awaited_once()
    jobs._service.install.assert_not_awaited()
    login.assert_not_awaited()
    assert "_configuration" not in result.model_dump_json()


async def test_connect_repairs_missing_managed_launch_before_checking_standard_profile(jobs):
    original = connect_setup(jobs, installation="not_installed")
    jobs._service._is_standard.return_value = False
    repaired = {**original, "command": "managed-codex-acp", "enabled": False}
    jobs._service.install.return_value = setup_result(configuration=repaired)
    jobs._service.check.return_value = setup_result("ready", configuration=repaired)
    job = await jobs.start(1, "codex", "connect", "en")
    result = await finish(jobs, job)
    assert result.status == "succeeded"
    assert jobs._service.install.await_args.kwargs["expected_configuration"] == original
    assert jobs._service.check.await_args.kwargs["expected_configuration"] == repaired


async def test_connect_runs_real_login_job_path_only_for_missing_authorization(jobs, monkeypatch):
    config = connect_setup(jobs)
    jobs._service.check.side_effect = [
        setup_result("auth_required", configuration=config),
        setup_result("ready", configuration=config),
    ]
    chunks = [b"https://auth.openai.com/codex/device\nUser code: ABCD-1234\n", b""]
    process = SimpleNamespace(
        wait=AsyncMock(return_value=0),
        stdout=SimpleNamespace(read=AsyncMock(side_effect=list(chunks))),
    )
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(module, "_stop_process", AsyncMock())
    monkeypatch.setattr(jobs, "_read_pty", AsyncMock(side_effect=list(chunks)))
    job = await jobs.start(1, "codex", "connect", "en")
    result = await finish(jobs, job)
    assert result.status == "succeeded" and result.setup.enabled
    assert jobs._service.check.await_count == 2
    assert spawn.await_args.args == ("codex-acp", "cli", "login", "--device-auth")
    assert result.authorization is None


async def test_connect_does_not_replace_an_existing_install_or_login_job(jobs):
    waiting = asyncio.Event()

    async def install(*args, **kwargs):
        await waiting.wait()

    jobs._service.install.side_effect = install
    original = await jobs.start(1, "codex", "install", "en")
    with pytest.raises(OctopError):
        await jobs.start(1, "codex", "connect", "en")
    assert jobs.get(1, original.job_id).kind == "install"
    await jobs.cancel(1, original.job_id)


async def test_cancel_connect_before_enable_cannot_enable_later(jobs):
    connect_setup(jobs)
    started = asyncio.Event()

    async def check(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    jobs._service.check.side_effect = check
    job = await jobs.start(1, "codex", "connect", "en")
    await started.wait()
    result = await jobs.cancel(1, job.job_id)
    assert result.status == "cancelled"
    jobs._service.enable_checked.assert_not_awaited()


async def test_cancel_during_final_thread_commit_waits_for_truthful_terminal_state(jobs):
    config = connect_setup(jobs)
    started, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()

    async def enable(*args):
        def commit():
            loop.call_soon_threadsafe(started.set)
            assert release.wait(3)
            return setup_result("ready", configuration=config, enabled=True)

        return await loop.run_in_executor(None, commit)

    jobs._service.enable_checked.side_effect = enable
    job = await jobs.start(1, "codex", "connect", "en")
    await started.wait()
    cancelling = asyncio.create_task(jobs.cancel(1, job.job_id))
    await asyncio.sleep(0)
    assert not cancelling.done() and jobs.get(1, job.job_id).phase == "enabling"
    release.set()
    result = await cancelling
    assert result.status == "succeeded" and result.setup.enabled


async def test_connect_check_failure_never_logs_in_or_enables(jobs, monkeypatch):
    connect_setup(jobs, connection="error")
    login = AsyncMock()
    monkeypatch.setattr(jobs, "_login", login)
    result = await finish(jobs, await jobs.start(1, "codex", "connect", "en"))
    assert result.status == "failed"
    login.assert_not_awaited()
    jobs._service.enable_checked.assert_not_awaited()


def test_codex_challenge_uses_its_login_deadline_without_extending_other_runners(jobs, monkeypatch):
    monkeypatch.setattr(module.time, "time", lambda: 100)
    for runner, seconds in [("codex", 900), ("claude_code", 600)]:
        job = _Job(1, "en", ACPSetupJob(job_id=runner, runner=runner, kind="login"))
        url = (
            "https://auth.openai.com/codex/device"
            if runner == "codex"
            else "https://claude.ai/oauth/authorize"
        )
        jobs._append(job, url + "\n")
        assert job.view.authorization.expires_at == (100 + seconds) * 1000
        job.login_deadline = 123
        job.view.authorization = None
        jobs._append(job, "\n")
        assert job.view.authorization.expires_at == 123_000
