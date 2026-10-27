"""Host-side installation and explicit, message-free ACP connection checks."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import hashlib
import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, Field, PrivateAttr

from octop.i18n import tr
from octop.infra.agents.acp_install_runtime import (
    cursor_launch,
    ensure_node,
    install_cursor,
    runtime_environment,
)
from octop.infra.agents.acp_setup_catalog import RUNNER_SETUP_SPECS, RunnerSetupSpec
from octop.infra.agents.settings.acp import ACPSettingsStore
from octop.infra.errors import ErrorCode, OctopError

Installation = Literal["not_installed", "installed", "missing_dependency", "manual"]
Connection = Literal["unchecked", "ready", "auth_required", "error"]
_CHECK_TIMEOUT = 25.0
_INSTALL_TIMEOUT = 240.0
AUTH_SCOPE_VERSION = 1
_RUNTIME_ENV_KEYS = frozenset(
    {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "SHELL",
        "OS",
        "PROCESSOR_ARCHITECTURE",
        "PROCESSOR_ARCHITEW6432",
        "NUMBER_OF_PROCESSORS",
        "TEMP",
        "TMP",
        "TMPDIR",
        "LANG",
        "LANGUAGE",
        "TERM",
        "COLORTERM",
        "TZ",
        "NO_COLOR",
        "FORCE_COLOR",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "NODE_EXTRA_CA_CERTS",
    }
)

_CREDENTIALS = {
    "pi": {
        "anthropic": ("Anthropic", "ANTHROPIC_API_KEY"),
        "openai": ("OpenAI", "OPENAI_API_KEY"),
        "google": ("Google Gemini", "GEMINI_API_KEY"),
        "openrouter": ("OpenRouter", "OPENROUTER_API_KEY"),
    },
    "codebuddy": {
        "codebuddy_cn": ("CodeBuddy", "CODEBUDDY_API_KEY"),
        "codebuddy_global": ("CodeBuddy", "CODEBUDDY_API_KEY"),
    },
    "cursor_cli": {"cursor": ("Cursor", "CURSOR_API_KEY")},
}


class ACPCredentialOption(BaseModel):
    id: str
    label: str


class ACPRunnerSetup(BaseModel):
    _configuration: dict[str, Any] | None = PrivateAttr(default=None)
    runner: str
    installation: Installation = Field(
        description="Passive installation state on the execution host"
    )
    connection: Connection = Field(
        default="unchecked",
        description="Latest explicit ACP session check; ready does not verify model entitlement",
    )
    enabled: bool = False
    can_install: bool = Field(
        default=False,
        description="Whether this official profile can be installed into the instance-managed directory",
    )
    can_login: bool = Field(default=False, description="A login-only host process is supported")
    credential_options: list[ACPCredentialOption] = Field(default_factory=list)
    install_command: str | None = None
    login_command: str | None = None
    docs_url: str | None = None
    message: str | None = None
    checked_at: int | None = Field(default=None, description="Unix timestamp in milliseconds")


class ACPSetupView(BaseModel):
    host: str = Field(description="Host running Octop and the external Runner programs")
    runners: dict[str, ACPRunnerSetup]


def _fingerprint(config: dict[str, Any]) -> str:
    launch = {key: config.get(key) for key in ("command", "args", "env")}
    return hashlib.sha256(json.dumps(launch, sort_keys=True).encode()).hexdigest()


def _environment(config: dict[str, Any]) -> dict[str, str]:
    env = dict(os.environ)
    env.update({str(key): str(value) for key, value in config.get("env", {}).items()})
    return env


def _find_command(command: str, env: dict[str, str]) -> str | None:
    # Relative launch paths depend on a task directory and cannot be validated here.
    if not command or (not Path(command).is_absolute() and ("/" in command or "\\" in command)):
        return None
    return shutil.which(command, path=env.get("PATH", ""))


def _display_command(parts: list[str]) -> str:
    return subprocess.list2cmdline(parts) if os.name == "nt" else shlex.join(parts)


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    if os.name == "posix":
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
    elif process.returncode is None:
        # The ACP runtime already depends on psutil; Windows needs explicit tree cleanup.
        def kill_tree() -> None:
            import psutil

            with contextlib.suppress(psutil.NoSuchProcess):
                parent = psutil.Process(process.pid)
                for child in parent.children(recursive=True):
                    with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                        child.kill()
                with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                    parent.kill()

        await asyncio.get_running_loop().run_in_executor(None, kill_tree)
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(process.wait(), timeout=3.0)


class _CheckClient:
    async def request_permission(self, **_: Any) -> dict[str, Any]:
        return {"outcome": {"outcome": "cancelled"}}

    async def session_update(self, **_: Any) -> None:
        pass

    async def read_text_file(self, **_: Any) -> None:
        from acp import RequestError

        raise RequestError.method_not_found("fs/read_text_file")

    async def write_text_file(self, **_: Any) -> None:
        from acp import RequestError

        raise RequestError.method_not_found("fs/write_text_file")


class ACPSetupService:
    def __init__(self, settings: ACPSettingsStore, root: Path) -> None:
        self._settings = settings
        self._root = root / "acp" / "runners"
        self._install_locks: dict[str, asyncio.Lock] = {}

    def _env(self, config: dict[str, Any]) -> dict[str, str]:
        return runtime_environment(self._root.parent, _environment(config))

    def runner_environment(
        self, user_id: int, runner: str, config: dict[str, Any], *, ensure: bool = False
    ) -> dict[str, str]:
        """Give login, inspection and execution the same owner/runner credential scope.

        Executables remain shared. Existing host CLI tokens and provider environment
        credentials are never adopted into this account's private login directory.
        """
        if user_id < 1:
            raise ValueError("ACP requires an account owner")
        token = hashlib.sha256(runner.encode()).hexdigest()
        home = self._root.parent / "users" / str(user_id) / token / "home"
        identity = {
            "HOME": home,
            "USERPROFILE": home,
            "APPDATA": home / "AppData" / "Roaming",
            "LOCALAPPDATA": home / "AppData" / "Local",
            "XDG_CONFIG_HOME": home / ".config",
            "XDG_DATA_HOME": home / ".local" / "share",
            "XDG_CACHE_HOME": home / ".cache",
            "XDG_STATE_HOME": home / ".local" / "state",
            "XDG_RUNTIME_DIR": home / ".run",
            "CODEX_HOME": home / ".codex",
            "CLAUDE_CONFIG_DIR": home / ".claude",
            "PI_CODING_AGENT_DIR": home / ".pi" / "agent",
            "OPENCODE_CONFIG_DIR": home / ".config" / "opencode",
        }
        reserved = {*identity, "HOMEDRIVE", "HOMEPATH", "OPENCODE_CONFIG", "KIMI_CONFIG_FILE"}
        env = {
            key: value
            for key, value in os.environ.items()
            if key.upper() in _RUNTIME_ENV_KEYS or key.upper().startswith("LC_")
        }
        env.update(
            {
                str(key): str(value)
                for key, value in config.get("env", {}).items()
                if str(key).upper() not in reserved
            }
        )
        env.update({key: str(path) for key, path in identity.items()})
        if os.name == "nt":
            env["HOMEDRIVE"], env["HOMEPATH"] = home.drive, str(home)[len(home.drive) :]
        if ensure:
            for path in set(identity.values()):
                path.mkdir(parents=True, mode=0o700, exist_ok=True)
        return self._managed_environment(runner, env)

    def legacy_runner_environment(
        self,
        user_id: int,
        runner: str,
        config: dict[str, Any],
        *,
        workspace_env: dict[str, str] | None = None,
        ensure: bool = False,
    ) -> dict[str, str]:
        """Close the legacy Harness parent-env overlay without persisting credentials.

        Harness applies runner.env after its cwd .env. Owner workspace values are
        captured with the same protected-key rules; explicit runner values win.
        """
        from octop_harness.runtime_env import overlay_env

        explicit = {**overlay_env({}, workspace_env or {}, protect=True), **config.get("env", {})}
        scoped = self.runner_environment(
            user_id, runner, {**config, "env": explicit}, ensure=ensure
        )
        cleared = {
            key: ""
            for key in os.environ
            if key.upper() not in _RUNTIME_ENV_KEYS and not key.upper().startswith("LC_")
        }
        return {**cleared, **scoped}

    async def view(self, user_id: int, locale: str) -> ACPSetupView:
        return await asyncio.get_running_loop().run_in_executor(
            None, partial(self._view, user_id, locale)
        )

    def _view(self, user_id: int, locale: str) -> ACPSetupView:
        return ACPSetupView(
            host=socket.gethostname(),
            runners={
                name: self._status(user_id, name, config, locale)
                for name, config in self._settings.load_runners(user_id).items()
            },
        )

    def _load(self, user_id: int, runner: str) -> dict[str, Any]:
        config = self._settings.load_runners(user_id).get(runner)
        if not isinstance(config, dict):
            raise OctopError(ErrorCode.NOT_FOUND, "ACP runner not found")
        return config

    def _save_setup_status(self, user_id: int, runner: str, status: dict[str, Any]) -> None:
        previous = self._settings.load_setup_status(user_id, runner)
        # Keep the installer-generated launch receipt even when a check is invalidated.
        # It identifies a repairable managed profile after its installation is damaged.
        managed = previous.get("managed_launch")
        preserved = {"managed_launch": managed} if isinstance(managed, list) else {}
        self._settings.save_setup_status(user_id, runner, {**preserved, **status})

    def _npm_command(self, env: dict[str, str]) -> list[str] | None:
        node = _find_command("node", env)
        npm = _find_command("npm", env)
        if node is None or npm is None:
            return None
        if os.name == "nt":
            entry = Path(npm).parent / "node_modules" / "npm" / "bin" / "npm-cli.js"
            return [node, str(entry)] if entry.is_file() else None
        return [npm]

    def _managed_environment(self, runner: str, env: dict[str, str]) -> dict[str, str]:
        env = runtime_environment(self._root.parent, env)
        private_bin = str(self._root / runner / "node_modules" / ".bin")
        paths = env.get("PATH", "").split(os.pathsep)
        env["PATH"] = os.pathsep.join(
            [private_bin, *(path for path in paths if path != private_bin)]
        )
        return env

    def _managed_launch(
        self, spec: RunnerSetupSpec, env: dict[str, str], require_complete: bool = True
    ) -> list[str] | None:
        if spec.runner_id == "cursor_cli":
            return cursor_launch(self._root.parent)
        prefix = self._root / spec.runner_id
        if require_complete and not (prefix / ".octop-install-complete").is_file():
            return None
        env = self._managed_environment(spec.runner_id, env)
        if any(_find_command(extra, env) is None for extra in spec.extra_binaries):
            return None
        for package in spec.npm_packages:
            # Catalog versions are pinned where verified; package.json lives under its name.
            name = package.rsplit("@", 1)[0] if "@" in package[1:] else package
            package_dir = prefix / "node_modules" / name
            try:
                metadata = json.loads((package_dir / "package.json").read_text(encoding="utf-8"))
                bins = metadata.get("bin", {})
                relative = bins.get(spec.binary) if isinstance(bins, dict) else bins
                if not isinstance(relative, str):
                    continue
                entry = (package_dir / relative).resolve()
                if not entry.is_relative_to(prefix.resolve()) or not entry.is_file():
                    continue
                if entry.suffix.lower() == ".exe":
                    return [str(entry), *spec.default_args]
                with entry.open("rb") as source:
                    first_line = source.readline(256)
                if entry.suffix in {".js", ".cjs", ".mjs"} or b"node" in first_line:
                    node = _find_command("node", env)
                    if node:
                        return [node, str(entry), *spec.default_args]
                elif os.name == "posix" and os.access(entry, os.X_OK):
                    return [str(entry), *spec.default_args]
            except (OSError, ValueError, AttributeError):
                continue
        return None

    def _is_standard(self, spec: RunnerSetupSpec, config: dict[str, Any]) -> bool:
        command, args = str(config.get("command", "")), tuple(config.get("args", []))
        accepted = ((spec.binary, spec.default_args), *spec.accepted_launches)
        if (command, args) in accepted:
            return True
        installed = _find_command(spec.binary, self._env(config))
        if installed and command == installed and args == spec.default_args:
            return True
        managed = self._managed_launch(spec, self._env(config))
        return managed is not None and [command, *args] == managed

    def _launch(self, runner: str, config: dict[str, Any]) -> list[str] | None:
        env = self._env(config)
        command = str(config.get("command", ""))
        args = list(config.get("args", []))
        if Path(command).stem.lower() in {"npx", "npm", "pnpm", "yarn", "bun", "uv", "uvx"}:
            # Package launchers may download on execution. Resolve only known local profiles.
            spec = RUNNER_SETUP_SPECS.get(runner)
            if spec is None or not self._is_standard(spec, config):
                return None
            managed = self._managed_launch(spec, env)
            if managed is not None:
                return managed
            if any(_find_command(extra, env) is None for extra in spec.extra_binaries):
                return None
            binary = _find_command(spec.binary, env)
            if binary and Path(binary).suffix.lower() in {".cmd", ".bat"}:
                return None
            return [binary, *spec.default_args] if binary else None
        binary = _find_command(command, env)
        if binary and Path(binary).suffix.lower() in {".cmd", ".bat"}:
            return None
        spec = RUNNER_SETUP_SPECS.get(runner)
        prefix = self._root / runner
        if spec and any(
            Path(part).is_absolute() and Path(part).is_relative_to(prefix)
            for part in [command, *args]
        ):
            return [binary, *args] if binary and self._managed_launch(spec, env) else None
        if spec and any(_find_command(extra, env) is None for extra in spec.extra_binaries):
            return None
        return [binary, *args] if binary else None

    def _status(
        self, user_id: int, runner: str, config: dict[str, Any], locale: str
    ) -> ACPRunnerSetup:
        spec = RUNNER_SETUP_SPECS.get(runner)
        env = self._env(config)
        launch = self._launch(runner, config)
        observation = self._settings.load_setup_status(user_id, runner)
        known_managed = observation.get("managed_launch") == [
            str(config.get("command", "")),
            *config.get("args", []),
        ]
        standard = spec is not None and (self._is_standard(spec, config) or known_managed)
        supported = standard and bool(spec and (spec.npm_packages or runner == "cursor_cli"))
        npm = self._npm_command(env) if supported else None
        installation: Installation = "installed" if launch else "not_installed"
        if not launch and not supported:
            installation = "manual"
        elif not launch and npm is None and spec and spec.npm_packages:
            installation = "missing_dependency"
        command = _find_command(str(config.get("command", "")), env)
        if command and Path(command).suffix.lower() in {".cmd", ".bat"}:
            installation = "manual"
        result = ACPRunnerSetup(
            runner=runner,
            installation=installation,
            enabled=bool(config.get("enabled", False)),
            can_install=bool(supported),
            can_login=bool(launch and standard and runner not in {"pi", "codebuddy"}),
            credential_options=[
                ACPCredentialOption(
                    id=key,
                    label=tr(f"acp_setup.credential_{key}", locale)
                    if runner == "codebuddy"
                    else value[0],
                )
                for key, value in _CREDENTIALS.get(runner, {}).items()
            ],
            docs_url=spec.docs_url if spec else None,
        )
        result._configuration = copy.deepcopy(config)
        if spec:
            if spec.login_args is not None:
                login = launch or [spec.binary, *spec.default_args]
                if spec.default_args:
                    login = login[: -len(spec.default_args)]
                result.login_command = _display_command([*login, *spec.login_args])
            if supported and spec.npm_packages:
                result.install_command = _display_command(
                    [
                        "npm",
                        "install",
                        "--prefix",
                        str(self._root / runner),
                        "--no-audit",
                        "--no-fund",
                        *spec.npm_packages,
                    ]
                )
        if (
            launch
            and observation.get("fingerprint") == _fingerprint(config)
            and observation.get("auth_scope_version") == AUTH_SCOPE_VERSION
        ):
            value = observation.get("connection")
            if value in {"unchecked", "ready", "auth_required", "error"}:
                result.connection = cast(Connection, value)
                checked_at = observation.get("checked_at")
                result.checked_at = checked_at if isinstance(checked_at, int) else None
        message_key: str = result.connection if result.connection != "unchecked" else installation
        if message_key == "installed":
            message_key = "unchecked"
        if message_key == "error":
            message_key = "check_failed"
        result.message = tr(f"acp_setup.{message_key}", locale)
        return result

    async def save_credentials(
        self, user_id: int, runner: str, provider: str, api_key: str, locale: str
    ) -> ACPRunnerSetup:
        option = _CREDENTIALS.get(runner, {}).get(provider)
        if (
            option is None
            or not api_key.strip()
            or len(api_key) > 8192
            or any(ch in api_key for ch in "\r\n\x00")
        ):
            raise OctopError(ErrorCode.SLASH_BAD_ARGS, "Invalid ACP credential configuration")

        def save() -> ACPRunnerSetup:
            runners = self._settings.load_runners(user_id)
            config = self._load(user_id, runner)
            env = dict(config.get("env", {}))
            if runner == "pi":
                for _, name in _CREDENTIALS[runner].values():
                    env[name] = ""
            env[option[1]] = api_key.strip()
            if runner == "codebuddy":
                env["CODEBUDDY_INTERNET_ENVIRONMENT"] = (
                    "internal" if provider == "codebuddy_cn" else "public"
                )
            runners[runner] = {**config, "env": env, "enabled": False}
            self._settings.save_runners(user_id, runners)
            self._save_setup_status(user_id, runner, {})
            result = self._status(user_id, runner, runners[runner], locale)
            result.message = tr("acp_setup.credentials_saved", locale)
            return result

        return await asyncio.get_running_loop().run_in_executor(None, save)

    async def check(
        self,
        user_id: int,
        runner: str,
        locale: str,
        *,
        expected_configuration: dict[str, Any] | None = None,
    ) -> ACPRunnerSetup:
        loop = asyncio.get_running_loop()
        config = await loop.run_in_executor(None, self._load, user_id, runner)
        if expected_configuration is not None and config != expected_configuration:
            result = await self._configuration_changed(user_id, runner, locale)
            result.connection = "error"
            return result
        result = await loop.run_in_executor(None, self._status, user_id, runner, config, locale)
        launch = await loop.run_in_executor(None, self._launch, runner, config)
        if launch is None:
            return result
        if Path(str(config.get("command", ""))).stem.lower() == "npx":
            # An explicit check adopts the exact local executable being tested. Subsequent
            # delegated tasks must not return to a package launcher that can download code.
            try:
                spec = RUNNER_SETUP_SPECS.get(runner)
                managed = spec is not None and launch == await loop.run_in_executor(
                    None, self._managed_launch, spec, self._env(config)
                )
                config = await loop.run_in_executor(
                    None, self._save_launch, user_id, runner, config, launch, managed
                )
            except RuntimeError:
                return await self._configuration_changed(user_id, runner, locale)
            result = await loop.run_in_executor(None, self._status, user_id, runner, config, locale)
        message_key = "ready"
        connection: Connection = "ready"
        try:
            env = await loop.run_in_executor(
                None, partial(self.runner_environment, user_id, runner, config, ensure=True)
            )
            spec = RUNNER_SETUP_SPECS.get(runner)
            if (
                spec
                and spec.auth_status_args
                and runner in {"claude_code", "codex"}
                and await loop.run_in_executor(None, self._is_standard, spec, config)
            ):
                await self._check_auth(spec, launch, env)
            await asyncio.wait_for(self._probe(launch, env), _CHECK_TIMEOUT)
        except TimeoutError:
            connection, message_key = "error", "check_timeout"
        except Exception as exc:
            from acp import RequestError

            connection, message_key = (
                ("auth_required", "auth_required")
                if isinstance(exc, RequestError) and exc.code == -32000
                else ("error", "check_failed")
            )
        latest = await loop.run_in_executor(None, self._load, user_id, runner)
        if (
            latest != config
            if expected_configuration is not None
            else _fingerprint(latest) != _fingerprint(config)
        ):
            result = await loop.run_in_executor(None, self._status, user_id, runner, latest, locale)
            result.message = tr("acp_setup.config_changed", locale)
            if expected_configuration is not None:
                result.connection = "error"
            return result
        checked_at = time.time_ns() // 1_000_000
        await loop.run_in_executor(
            None,
            self._save_setup_status,
            user_id,
            runner,
            {
                "fingerprint": _fingerprint(config),
                "connection": connection,
                "auth_scope_version": AUTH_SCOPE_VERSION,
                "checked_at": checked_at,
            },
        )
        result.connection, result.checked_at = connection, checked_at
        result.message = tr(f"acp_setup.{message_key}", locale)
        return result

    async def _configuration_changed(
        self, user_id: int, runner: str, locale: str
    ) -> ACPRunnerSetup:
        loop = asyncio.get_running_loop()
        latest = await loop.run_in_executor(None, self._load, user_id, runner)
        result = await loop.run_in_executor(None, self._status, user_id, runner, latest, locale)
        result.message = tr("acp_setup.config_changed", locale)
        return result

    def _save_launch(
        self,
        user_id: int,
        runner: str,
        expected: dict[str, Any],
        launch: list[str],
        managed: bool,
        disable: bool = False,
    ) -> dict[str, Any]:
        runners = self._settings.load_runners(user_id)
        latest = runners.get(runner, {})
        if _fingerprint(latest) != _fingerprint(expected):
            raise RuntimeError("ACP configuration changed")
        updated = {**latest, "command": launch[0], "args": launch[1:]}
        if disable:
            updated["enabled"] = False
        if managed:
            env = dict(latest.get("env", {}))
            env["PATH"] = self._managed_environment(runner, self._env(latest))["PATH"]
            updated["env"] = env
        saved = self._settings.update_runner_if_matches(user_id, runner, expected, updated)
        if saved is None:
            raise RuntimeError("ACP configuration changed")
        if managed:
            self._save_setup_status(user_id, runner, {"managed_launch": launch})
        return saved

    async def enable_checked(
        self, user_id: int, runner: str, locale: str, expected_configuration: dict[str, Any]
    ) -> ACPRunnerSetup:
        def commit() -> ACPRunnerSetup:
            config = self._load(user_id, runner)
            result = self._status(user_id, runner, config, locale)
            observation = self._settings.load_setup_status(user_id, runner)
            if (
                config != expected_configuration
                or result.connection != "ready"
                or observation.get("connection") != "ready"
                or observation.get("fingerprint") != _fingerprint(config)
                or observation.get("auth_scope_version") != AUTH_SCOPE_VERSION
            ):
                result.connection = "error"
                result.message = tr("acp_setup.config_changed", locale)
                return result
            saved = self._settings.update_runner_if_matches(
                user_id, runner, config, {"enabled": True}, expected_status=observation
            )
            if saved is None:
                result.connection = "error"
                result.message = tr("acp_setup.config_changed", locale)
                return result
            result.enabled = True
            result._configuration = saved
            result.message = tr("acp_setup.connected", locale)
            return result

        return await asyncio.get_running_loop().run_in_executor(None, commit)

    async def _capture(self, command: list[str], env: dict[str, str]) -> tuple[int, bytes]:
        """Read bounded local CLI status output; it is never included in the API response."""
        process = await asyncio.create_subprocess_exec(
            *command,
            env=env,
            cwd=env.get("HOME"),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=os.name == "posix",
            limit=64 * 1024,
        )
        try:
            assert process.stdout is not None
            async with asyncio.timeout(10.0):
                data = b""
                while chunk := await process.stdout.read(4096):
                    data += chunk
                    if len(data) > 64 * 1024:
                        raise RuntimeError("ACP local status output exceeded limit")
                code = await process.wait()
                return code, data
        finally:
            await _stop_process(process)

    async def _check_auth(
        self, spec: RunnerSetupSpec, launch: list[str], env: dict[str, str]
    ) -> None:
        from acp import RequestError

        base = launch[: -len(spec.default_args)] if spec.default_args else launch
        code, raw = await self._capture([*base, *(spec.auth_status_args or ())], env)
        if spec.runner_id == "claude_code":
            try:
                status = json.loads(raw)
            except ValueError as exc:
                raise RuntimeError("Unrecognized ACP CLI authentication status") from exc
            if code == 1 and isinstance(status, dict) and status.get("loggedIn") is False:
                raise RequestError.auth_required()
            if code != 0 or not isinstance(status, dict) or status.get("loggedIn") is not True:
                raise RuntimeError("Unrecognized ACP CLI authentication status")
        elif code == 1 and raw.strip() == b"Not logged in":
            raise RequestError.auth_required()
        elif code != 0:
            raise RuntimeError("ACP CLI authentication status failed")

    async def _probe(self, launch: list[str], env: dict[str, str]) -> None:
        from acp import PROTOCOL_VERSION, Client, connect_to_agent
        from acp.schema import ClientCapabilities, Implementation

        loop = asyncio.get_running_loop()
        directory = await loop.run_in_executor(
            None, partial(tempfile.mkdtemp, prefix="octop-acp-check-")
        )
        process: asyncio.subprocess.Process | None = None
        connection: Any = None
        try:
            process = await asyncio.create_subprocess_exec(
                *launch,
                cwd=directory,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=os.name == "posix",
                limit=1024 * 1024,
            )
            connection = connect_to_agent(
                cast(Client, _CheckClient()), process.stdin, process.stdout
            )
            initialized = await connection.initialize(
                protocol_version=PROTOCOL_VERSION,
                client_capabilities=ClientCapabilities(),
                client_info=Implementation(name="octop-setup-check", version="1"),
            )
            if initialized.protocol_version != PROTOCOL_VERSION:
                raise ValueError("Unsupported ACP protocol version")
            session = await connection.new_session(cwd=directory, mcp_servers=[])
            capabilities = initialized.agent_capabilities
            sessions = getattr(capabilities, "session_capabilities", None)
            if getattr(sessions, "close", None) is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(
                        connection.close_session(session_id=session.session_id), 2.0
                    )
        finally:
            if process is not None:
                await _stop_process(process)
            if connection is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(connection.close(), 2.0)
            await loop.run_in_executor(None, partial(shutil.rmtree, directory, ignore_errors=True))

    async def install(
        self,
        user_id: int,
        runner: str,
        locale: str,
        on_progress: Callable[[str], None] | None = None,
        *,
        expected_configuration: dict[str, Any] | None = None,
    ) -> ACPRunnerSetup:
        progress = on_progress or (lambda _stage: None)
        lock = self._install_locks.setdefault(runner, asyncio.Lock())
        if lock.locked():
            result = (await self.view(user_id, locale)).runners.get(runner)
            if result is None:
                raise OctopError(ErrorCode.NOT_FOUND, "ACP runner not found")
            result.connection = "error"
            result.message = tr("acp_setup.busy", locale)
            return result
        async with lock:
            loop = asyncio.get_running_loop()
            config = await loop.run_in_executor(None, self._load, user_id, runner)
            result = await loop.run_in_executor(None, self._status, user_id, runner, config, locale)
            if expected_configuration is not None and config != expected_configuration:
                result.connection = "error"
                result.message = tr("acp_setup.config_changed", locale)
                return result
            if not result.can_install:
                result.connection = "error"
                result.message = tr("acp_setup.unsupported_install", locale)
                return result
            spec = RUNNER_SETUP_SPECS[runner]
            progress("preparing")
            prefix = self._root / runner
            marker = prefix / ".octop-install-complete"
            launch: list[str] | None
            try:
                env = await loop.run_in_executor(None, self._env, config)
                # Installations are shared by the host. A second user's setup must
                # not mutate or cancel an already complete runner used by others.
                launch = await loop.run_in_executor(None, self._managed_launch, spec, env)
                if launch is None and runner == "cursor_cli":
                    launch = await install_cursor(self._root.parent, env, progress)
                elif launch is None:
                    env = await ensure_node(self._root.parent, env, progress)
                    npm = await loop.run_in_executor(None, self._npm_command, env)
                    if npm is None:
                        raise RuntimeError("Managed npm is unavailable")
                    await loop.run_in_executor(
                        None, partial(prefix.mkdir, parents=True, exist_ok=True)
                    )
                    await loop.run_in_executor(None, partial(marker.unlink, missing_ok=True))
                    progress("installing_runner")
                    await self._run_install(
                        [
                            *npm,
                            "install",
                            "--prefix",
                            str(prefix),
                            "--no-audit",
                            "--no-fund",
                            *spec.npm_packages,
                        ],
                        env,
                        on_output=progress,
                    )
                    launch = await loop.run_in_executor(
                        None, self._managed_launch, spec, env, False
                    )
                    if launch is None:
                        raise RuntimeError("Runner executable is missing after installation")
                    await loop.run_in_executor(None, marker.touch)
            except (OSError, RuntimeError, TimeoutError) as exc:
                key = "install_timeout" if isinstance(exc, TimeoutError) else "install_failed"
                cause: BaseException | None = exc
                while cause is not None:
                    reason = str(cause)
                    if reason in {
                        "unsupported_runtime_platform",
                        "unsupported_runtime_architecture",
                        "runtime_checksum_mismatch",
                        "runtime_not_executable",
                        "runtime_unsafe_archive",
                    }:
                        key = reason
                        break
                    cause = cause.__cause__
                result.connection = "error"
                result.message = tr(f"acp_setup.{key}", locale)
                return result
            progress("saving_config")
            latest = await loop.run_in_executor(None, self._load, user_id, runner)
            if _fingerprint(latest) != _fingerprint(config):
                result = await self._configuration_changed(user_id, runner, locale)
                result.connection = "error"
                return result
            try:
                saved = await loop.run_in_executor(
                    None, self._save_launch, user_id, runner, config, launch, True, True
                )
            except RuntimeError:
                result = await self._configuration_changed(user_id, runner, locale)
                result.connection = "error"
                return result
            result = await loop.run_in_executor(None, self._status, user_id, runner, saved, locale)
            result.connection, result.checked_at = "unchecked", None
            await loop.run_in_executor(None, self._save_setup_status, user_id, runner, {})
            result.message = tr("acp_setup.installed", locale)
            return result

    async def _run_install(
        self,
        command: list[str],
        env: dict[str, str],
        on_output: Callable[[str], None] | None = None,
    ) -> None:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=command[command.index("--prefix") + 1],
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=os.name == "posix",
        )
        try:
            async with asyncio.timeout(_INSTALL_TIMEOUT):
                assert process.stdout is not None
                while chunk := await process.stdout.read(4096):
                    if on_output is not None:
                        on_output(chunk.decode("utf-8", errors="replace"))
                if await process.wait() != 0:
                    raise RuntimeError("ACP package installation failed")
        finally:
            await _stop_process(process)
