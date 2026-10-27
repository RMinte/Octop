"""Owner-scoped background setup jobs with fixed, login-only processes."""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import errno
import os
import re
import shutil
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Literal
from urllib.parse import parse_qs, urlsplit

from pydantic import BaseModel, Field

from octop.i18n import tr
from octop.infra.agents.acp_setup import (
    ACPRunnerSetup,
    ACPSetupService,
    _stop_process,
)
from octop.infra.agents.acp_setup_catalog import RUNNER_SETUP_SPECS
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.utils import posix_compat

JobKind = Literal["install", "login", "connect"]
JobStatus = Literal["running", "waiting_for_user", "succeeded", "failed", "cancelled"]
_ACTIVE = {"running", "waiting_for_user"}
_INSTALL_PHASES = {
    "preparing",
    "downloading_runtime",
    "installing_runtime",
    "downloading_runner",
    "installing_runner",
    "saving_config",
}
_LOGIN_TIMEOUT = 600.0
_CODEX_LOGIN_TIMEOUT = 900.0
_OUTPUT_LIMIT = 16_384
_LOGIN_RUNNERS = frozenset({"opencode", "claude_code", "codex", "kimi_code", "cursor_cli"})
_AUTH_HOSTS: dict[str, tuple[str, ...]] = {
    "opencode": (
        "opencode.ai",
        "auth.openai.com",
        "chatgpt.com",
        "openai.com",
        "claude.ai",
        "console.anthropic.com",
    ),
    "claude_code": ("claude.ai", "console.anthropic.com", "platform.claude.com"),
    "codex": ("auth.openai.com", "chatgpt.com", "openai.com"),
    "kimi_code": ("auth.kimi.com", "kimi.com", "moonshot.cn"),
    "cursor_cli": ("cursor.com",),
}
_URL = re.compile(r"https?://[^\s<>\"'\x1b]+")
_ANSI = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]")
_SECRET = re.compile(
    r"(?i)(access[_-]?token|refresh[_-]?token|npm[_-]?token|_authToken|_auth|api[_-]?key|client[_-]?secret|device[_-]?code|password|authorization)"
    r"([\"']?\s*[:=]\s*[\"']?|\s+)([^\s\"',}\]]+)"
)
_TOKEN = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{8,}|npm_[A-Za-z0-9_-]{8,}|[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,})\b"
)
_AUTH_HEADER = re.compile(r"(?i)\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/-]+=*")
# Require a complete, code-shaped line rather than a partial chunk or instruction.
_USER_CODE = re.compile(
    r"(?i:(?:user|verification|one[- ]time|authorization)[ \t]*code)"
    r"[ \t]*(?:\([^\n()]{0,80}\)[ \t]*)?"
    r"(?:[:=][ \t]*|\n)[ \t\n]*"
    r"(?=[A-Z0-9 -]*[-0-9])"
    r"([A-Z0-9]{3,8}(?:[- ][A-Z0-9]{3,8})?)[ \t]*(?=\n)"
)
_SENSITIVE_QUERY = {
    "access_token",
    "refresh_token",
    "device_code",
    "token",
    "id_token",
    "api_key",
    "client_secret",
}


class ACPSetupAuthorization(BaseModel):
    url: str | None = None
    user_code: str | None = None
    expires_at: int | None = Field(default=None, description="Unix timestamp in milliseconds")


class ACPSetupJob(BaseModel):
    job_id: str
    runner: str
    kind: JobKind
    status: JobStatus = "running"
    phase: str = "preparing"
    output: str = Field(
        default="", description="Bounded, sanitized process output for the job owner"
    )
    message: str | None = None
    authorization: ACPSetupAuthorization | None = None
    input_mode: Literal["none", "terminal"] = "none"
    setup: ACPRunnerSetup | None = None


@dataclass
class _Job:
    owner: int
    locale: str
    view: ACPSetupJob
    task: asyncio.Task[None] | None = None
    process: asyncio.subprocess.Process | None = None
    master_fd: int | None = None
    parse_buffer: str = ""
    secrets: list[str] = field(default_factory=list)
    expected_configuration: dict[str, Any] | None = None
    login_deadline: float | None = None


def _safe_url(value: str, runner: str) -> str | None:
    value = value.rstrip(".,;)")
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        if (
            parsed.scheme != "https"
            or parsed.username
            or parsed.password
            or parsed.port not in {None, 443}
            or not any(
                host == allowed or host.endswith(f".{allowed}")
                for allowed in _AUTH_HOSTS.get(runner, ())
            )
            or _SENSITIVE_QUERY.intersection(key.lower() for key in parse_qs(parsed.query))
        ):
            return None
    except ValueError:
        return None
    return value


class ACPSetupJobs:
    def __init__(
        self, service: ACPSetupService, on_complete: Callable[[int], None] | None = None
    ) -> None:
        self._service = service
        self._on_complete = on_complete
        self._jobs: dict[str, _Job] = {}
        self._closed = False

    def _owned(self, user_id: int, job_id: str) -> _Job:
        job = self._jobs.get(job_id)
        if job is None or job.owner != user_id:
            raise OctopError(ErrorCode.NOT_FOUND, "ACP setup job not found")
        return job

    def get(self, user_id: int, job_id: str) -> ACPSetupJob:
        return self._owned(user_id, job_id).view.model_copy(deep=True)

    def active(self, user_id: int) -> dict[str, ACPSetupJob]:
        return {
            job.view.runner: job.view.model_copy(deep=True)
            for job in self._jobs.values()
            if job.owner == user_id and job.view.status in _ACTIVE
        }

    async def start(self, user_id: int, runner: str, kind: JobKind, locale: str) -> ACPSetupJob:
        if self._closed:
            raise OctopError(ErrorCode.SLASH_BAD_ARGS, tr("acp_setup.job_closed", locale))
        if kind not in {"install", "login", "connect"} or (kind == "connect" and runner != "codex"):
            raise OctopError(ErrorCode.SLASH_BAD_ARGS, "Invalid ACP setup operation")
        config = await asyncio.get_running_loop().run_in_executor(
            None, self._service._load, user_id, runner
        )
        if self._closed:
            raise OctopError(ErrorCode.SLASH_BAD_ARGS, tr("acp_setup.job_closed", locale))
        for current in self._jobs.values():
            if current.view.runner == runner and current.view.status in _ACTIVE:
                if current.owner == user_id and current.view.kind == kind:
                    return current.view.model_copy(deep=True)
                raise OctopError(ErrorCode.SLASH_BAD_ARGS, tr("acp_setup.busy", locale))
        # Keep completed receipts bounded; active jobs are never discarded.
        finished = [key for key, job in self._jobs.items() if job.view.status not in _ACTIVE]
        for key in finished[:-32]:
            self._jobs.pop(key, None)
        view = ACPSetupJob(
            job_id=uuid.uuid4().hex,
            runner=runner,
            kind=kind,
            message=tr("acp_setup.job_running", locale),
        )
        job = _Job(user_id, locale, view)
        if kind == "connect":
            job.expected_configuration = config
        self._jobs[view.job_id] = job
        job.task = asyncio.create_task(self._execute(job), name=f"acp-setup-{kind}-{runner}")
        return view.model_copy(deep=True)

    def _append(self, job: _Job, text: str) -> None:
        clean = _ANSI.sub("", text).replace("\r", "\n")
        job.parse_buffer = _ANSI.sub("", job.parse_buffer + clean)[-8192:]
        for found in _URL.finditer(job.parse_buffer):
            url = _safe_url(found.group(), job.view.runner)
            if url and job.view.kind in {"login", "connect"}:
                authorization = job.view.authorization or ACPSetupAuthorization(
                    expires_at=int(
                        (
                            job.login_deadline
                            or (
                                time.time()
                                + (
                                    _CODEX_LOGIN_TIMEOUT
                                    if job.view.runner == "codex"
                                    else _LOGIN_TIMEOUT
                                )
                            )
                        )
                        * 1000
                    )
                )
                authorization.url = url
                query = parse_qs(urlsplit(url).query)
                codes = query.get("user_code") or query.get("userCode")
                authorization.user_code = codes[0] if codes else None
                job.view.authorization = authorization
                job.view.status, job.view.phase = "waiting_for_user", "waiting_for_user"
        found_code = _USER_CODE.search(job.parse_buffer)
        if found_code and job.view.authorization is not None:
            job.view.authorization.user_code = found_code.group(1)
        # Raw URLs and bearer credentials never enter the retained transcript.
        clean = _URL.sub("[authorization link]", job.view.output + clean)
        clean = _AUTH_HEADER.sub("[redacted]", clean)
        clean = _SECRET.sub(lambda match: f"{match.group(1)}{match.group(2)}[redacted]", clean)
        clean = _TOKEN.sub("[redacted]", clean)
        for secret in job.secrets:
            clean = clean.replace(secret, "[redacted]")
        clean = "".join(char for char in clean if char in "\n\t" or ord(char) >= 32)
        job.view.output = clean[-_OUTPUT_LIMIT:]

    def _progress(self, job: _Job, text: str) -> None:
        if job.view.status not in _ACTIVE:
            return
        if text in _INSTALL_PHASES:
            job.view.phase = text
        else:
            self._append(job, text)

    async def _execute(self, job: _Job) -> None:
        try:
            if job.view.kind == "install":
                job.view.setup = await self._install(job)
                success = (
                    job.view.setup.installation == "installed"
                    and job.view.setup.connection != "error"
                )
                job.view.status = "succeeded" if success else "failed"
            elif job.view.kind == "connect":
                await self._connect(job)
            else:
                await self._login(job)
            job.view.phase = "completed" if job.view.status == "succeeded" else "failed"
            if job.view.setup and (
                job.view.status == "succeeded"
                or job.view.message == tr("acp_setup.job_running", job.locale)
            ):
                job.view.message = job.view.setup.message
            elif job.view.message == tr("acp_setup.job_running", job.locale):
                job.view.message = tr(f"acp_setup.job_{job.view.status}", job.locale)
            if job.view.status == "succeeded" and self._on_complete and not self._closed:
                with contextlib.suppress(Exception):
                    self._on_complete(job.owner)
        except asyncio.CancelledError:
            job.view.status, job.view.phase = "cancelled", "cancelled"
            job.view.message = tr("acp_setup.job_cancelled", job.locale)
            raise
        except TimeoutError:
            job.view.status, job.view.phase = "failed", "failed"
            job.view.message = tr(
                "acp_setup.login_timeout"
                if job.view.kind in {"login", "connect"}
                else "acp_setup.install_timeout",
                job.locale,
            )
        except Exception:
            job.view.status, job.view.phase = "failed", "failed"
            job.view.message = tr("acp_setup.job_failed", job.locale)
        finally:
            job.view.input_mode = "none"
            job.view.authorization = None
            job.parse_buffer = ""
            job.secrets.clear()

    async def _install(self, job: _Job) -> ACPRunnerSetup:
        job.view.phase = "installing_runner"
        loop = asyncio.get_running_loop()
        active = True

        def deliver(text: str) -> None:
            if active:
                self._progress(job, text)

        def progress(text: str) -> None:
            loop.call_soon_threadsafe(deliver, text)

        try:
            async with asyncio.timeout(_LOGIN_TIMEOUT):
                if job.view.kind == "connect":
                    return await self._service.install(
                        job.owner,
                        job.view.runner,
                        job.locale,
                        on_progress=progress,
                        expected_configuration=job.expected_configuration,
                    )
                return await self._service.install(
                    job.owner, job.view.runner, job.locale, on_progress=progress
                )
        finally:
            active = False

    async def _connect(self, job: _Job) -> None:
        loop = asyncio.get_running_loop()
        config = job.expected_configuration
        assert config is not None
        setup = await loop.run_in_executor(
            None, self._service._status, job.owner, job.view.runner, config, job.locale
        )
        if setup.installation != "installed":
            setup = await self._install(job)
            job.view.setup = setup
            if setup.installation != "installed" or setup.connection == "error":
                job.view.status = "failed"
                return
            config = setup._configuration
            assert config is not None
            job.expected_configuration = config
        elif not await loop.run_in_executor(
            None, self._service._is_standard, RUNNER_SETUP_SPECS["codex"], config
        ):
            job.view.status = "failed"
            job.view.message = tr("acp_setup.login_unavailable", job.locale)
            return
        job.view.phase = "checking_connection"
        setup = await self._service.check(
            job.owner, job.view.runner, job.locale, expected_configuration=config
        )
        job.view.setup = setup
        if setup.connection == "auth_required":
            job.expected_configuration = setup._configuration
            await self._login(job)
            setup = job.view.setup or setup
        if setup.connection != "ready" or job.view.status == "failed":
            job.view.status = "failed"
            return
        config = setup._configuration
        assert config is not None
        job.view.phase, job.view.status = "enabling", "running"
        commit = asyncio.create_task(
            self._service.enable_checked(job.owner, job.view.runner, job.locale, config)
        )
        try:
            setup = await asyncio.shield(commit)
        except asyncio.CancelledError:
            setup = await asyncio.shield(commit)
        job.view.setup = setup
        job.view.status = "succeeded" if setup.connection == "ready" and setup.enabled else "failed"

    def _login_launch(self, job: _Job) -> tuple[list[str], dict[str, str]] | None:
        spec = RUNNER_SETUP_SPECS.get(job.view.runner)
        if spec is None or job.view.runner not in _LOGIN_RUNNERS or spec.login_args is None:
            return None
        config = self._service._load(job.owner, job.view.runner)
        if job.expected_configuration is not None and config != job.expected_configuration:
            job.view.message = tr("acp_setup.config_changed", job.locale)
            return None
        if not self._service._is_standard(spec, config):
            return None
        launch = self._service._launch(job.view.runner, config)
        if launch is None:
            return None
        base = launch[: -len(spec.default_args)] if spec.default_args else launch
        env = self._service.runner_environment(job.owner, job.view.runner, config, ensure=True)
        env.update({"NO_OPEN_BROWSER": "1", "BROWSER": "none", "TERM": "xterm-256color"})
        return [*base, *spec.login_args], env

    async def _read_pty(self, descriptor: int) -> bytes:
        loop = asyncio.get_running_loop()
        ready: asyncio.Future[bytes] = loop.create_future()

        def read_ready() -> None:
            if ready.done():
                return
            try:
                ready.set_result(os.read(descriptor, 4096))
            except BlockingIOError:
                pass
            except OSError as exc:
                if exc.errno == errno.EIO:
                    ready.set_result(b"")
                else:
                    ready.set_exception(exc)

        loop.add_reader(descriptor, read_ready)
        try:
            return await ready
        finally:
            loop.remove_reader(descriptor)

    async def _login(self, job: _Job) -> None:
        job.view.phase = "starting_login"
        if job.view.runner not in _LOGIN_RUNNERS:
            job.view.status = "failed"
            job.view.message = tr("acp_setup.login_not_supported", job.locale)
            return
        loop = asyncio.get_running_loop()
        prepared = await loop.run_in_executor(None, self._login_launch, job)
        if prepared is None:
            job.view.status = "failed"
            if job.view.message != tr("acp_setup.config_changed", job.locale):
                job.view.message = tr("acp_setup.login_unavailable", job.locale)
            return
        if os.name != "posix" and job.view.runner in {"opencode", "kimi_code"}:
            job.view.status = "failed"
            job.view.message = tr("acp_setup.login_platform_unsupported", job.locale)
            return
        command, env = prepared
        job.secrets = [
            value
            for key, value in env.items()
            if value and len(value) >= 8 and re.search(r"(?i)token|secret|key|password", key)
        ]
        directory = await loop.run_in_executor(
            None, partial(tempfile.mkdtemp, prefix="octop-acp-login-")
        )
        slave: int | None = None
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        try:
            timeout = _CODEX_LOGIN_TIMEOUT if job.view.runner == "codex" else _LOGIN_TIMEOUT
            job.login_deadline = time.time() + timeout
            async with asyncio.timeout(timeout):
                if os.name == "posix":
                    job.master_fd, slave = posix_compat.openpty()
                    posix_compat.set_nonblock(job.master_fd)
                    job.process = await asyncio.create_subprocess_exec(
                        *command,
                        cwd=directory,
                        env=env,
                        stdin=slave,
                        stdout=slave,
                        stderr=slave,
                        start_new_session=True,
                    )
                    os.close(slave)
                    slave = None
                else:
                    job.process = await asyncio.create_subprocess_exec(
                        *command,
                        cwd=directory,
                        env=env,
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT,
                    )
                job.view.input_mode = "terminal"
                job.view.status, job.view.phase = "waiting_for_user", "waiting_for_user"
                while True:
                    if job.master_fd is not None:
                        chunk = await self._read_pty(job.master_fd)
                    else:
                        assert job.process.stdout is not None
                        chunk = await job.process.stdout.read(4096)
                    if not chunk:
                        break
                    self._append(job, decoder.decode(chunk))
                self._append(job, decoder.decode(b"", final=True))
                if await job.process.wait() != 0:
                    job.view.status = "failed"
                    job.view.message = tr("acp_setup.login_failed", job.locale)
                    return
                job.view.input_mode = "none"
                job.view.phase = "checking_connection"
                if job.view.kind == "connect":
                    job.view.setup = await self._service.check(
                        job.owner,
                        job.view.runner,
                        job.locale,
                        expected_configuration=job.expected_configuration,
                    )
                    job.view.status = (
                        "running" if job.view.setup.connection == "ready" else "failed"
                    )
                else:
                    job.view.setup = await self._service.check(
                        job.owner, job.view.runner, job.locale
                    )
                    job.view.status = (
                        "succeeded" if job.view.setup.connection == "ready" else "failed"
                    )
        finally:
            if job.process is not None:
                await _stop_process(job.process)
                job.process = None
            for descriptor in (slave, job.master_fd):
                if descriptor is not None:
                    with contextlib.suppress(OSError):
                        os.close(descriptor)
            job.master_fd = None
            await loop.run_in_executor(None, partial(shutil.rmtree, directory, ignore_errors=True))

    async def cancel(self, user_id: int, job_id: str) -> ACPSetupJob:
        job = self._owned(user_id, job_id)
        if job.task is not None and not job.task.done():
            if job.view.phase != "enabling":
                job.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.shield(job.task)
        if job.view.status in _ACTIVE:
            job.view.status, job.view.phase = "cancelled", "cancelled"
            job.view.message = tr("acp_setup.job_cancelled", job.locale)
            job.view.input_mode, job.view.authorization = "none", None
        return job.view.model_copy(deep=True)

    async def input(self, user_id: int, job_id: str, data: str) -> ACPSetupJob:
        job = self._owned(user_id, job_id)
        if (
            job.view.status != "waiting_for_user"
            or job.view.input_mode != "terminal"
            or job.process is None
        ):
            raise OctopError(ErrorCode.SLASH_BAD_ARGS, "ACP login is not waiting for input")
        if not data or len(data.encode()) > 4096 or "\x00" in data:
            raise OctopError(ErrorCode.SLASH_BAD_ARGS, "Invalid ACP login input")
        if len(data.strip()) >= 6:
            job.secrets.append(data.strip())
            job.secrets = job.secrets[-32:]
        if job.master_fd is not None:
            await asyncio.get_running_loop().run_in_executor(
                None, os.write, job.master_fd, data.encode()
            )
        elif job.process.stdin is not None:
            job.process.stdin.write(data.encode())
            await job.process.stdin.drain()
        return job.view.model_copy(deep=True)

    async def close(self) -> None:
        self._closed = True
        tasks = [job.task for job in self._jobs.values() if job.task and not job.task.done()]
        for task in tasks:
            job = next(job for job in self._jobs.values() if job.task is task)
            if job.view.phase != "enabling":
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for job in self._jobs.values():
            if job.view.status in _ACTIVE:
                job.view.status, job.view.phase = "cancelled", "cancelled"
                job.view.message = tr("acp_setup.job_cancelled", job.locale)
                job.view.input_mode, job.view.authorization = "none", None
