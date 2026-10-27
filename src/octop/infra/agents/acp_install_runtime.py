"""Install fixed upstream runtimes into Octop's private ACP directory."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import platform
import posixpath
import re
import shutil
import signal
import tarfile
import tempfile
import threading
import urllib.request
from collections.abc import Callable
from functools import partial
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit
from weakref import WeakKeyDictionary

_NODE_VERSION = "22.23.4"
_NODE_MINIMUM = (22, 19, 0)
# https://nodejs.org/en/blog/release/v22.23.4 (signed SHASUMS)
_NODE_SHA256 = {
    "x64": "1805bcd35e6815559bd2eb97db438d3fdb0eb3a5e6a977b7c1badca76337e9ce",
    "arm64": "6287aa46b6c375d816e63ae66d024fbbfd70404825aabdfdf898a970871d2f4b",
}
# Fixed package URL and layout from https://cursor.com/install, reviewed 2026-10-26.
_CURSOR_VERSION = "2026.10.22-27dcf5f"
_MARKER = ".octop-install-complete"
_MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024
_MAX_EXTRACT_BYTES = 2 * 1024 * 1024 * 1024
_MAX_MEMBERS = 50_000
_DOWNLOAD_TIMEOUT = 180.0
_READ_TIMEOUT = 15.0
_LOCKS: WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Lock]] = WeakKeyDictionary()

Progress = Callable[[str], None]


def _install_lock(destination: Path) -> asyncio.Lock:
    locks = _LOCKS.setdefault(asyncio.get_running_loop(), {})
    return locks.setdefault(str(destination.absolute()), asyncio.Lock())


def _linux_architecture() -> str:
    if platform.system() != "Linux":
        raise RuntimeError("unsupported_runtime_platform")
    machine = platform.machine().lower()
    if machine in {"x86_64", "amd64"}:
        return "x64"
    if machine in {"aarch64", "arm64"}:
        return "arm64"
    raise RuntimeError("unsupported_runtime_architecture")


def _prepend(env: dict[str, str], directory: Path) -> dict[str, str]:
    result = dict(env)
    location = str(directory)
    paths = env.get("PATH", "").split(os.pathsep)
    result["PATH"] = os.pathsep.join([location, *(path for path in paths if path != location)])
    return result


def _complete(directory: Path, kind: str, version: str, required: tuple[str, ...]) -> bool:
    try:
        marker = json.loads((directory / _MARKER).read_text(encoding="utf-8"))
        if marker != {"kind": kind, "version": version}:
            return False
        return all((directory / name).is_file() for name in required)
    except (OSError, ValueError):
        return False


def runtime_environment(acp_root: Path, env: dict[str, str]) -> dict[str, str]:
    """Passively expose a complete managed Node installation; never launch or download."""
    directory = acp_root / "runtime" / "node"
    if _complete(directory, "node", _NODE_VERSION, ("bin/node", "bin/npm")) and os.access(
        directory / "bin" / "node", os.X_OK
    ):
        return _prepend(env, directory / "bin")
    return dict(env)


def cursor_launch(acp_root: Path) -> list[str] | None:
    """Return the fixed native entry point only after a complete managed installation."""
    directory = acp_root / "runners" / "cursor_cli"
    executable = directory / "cursor-agent"
    if _complete(directory, "cursor", _CURSOR_VERSION, ("cursor-agent",)) and os.access(
        executable, os.X_OK
    ):
        return [str(executable), "acp"]
    return None


async def _node_is_usable(env: dict[str, str]) -> bool:
    node, npm = await asyncio.get_running_loop().run_in_executor(
        None,
        lambda: (
            shutil.which("node", path=env.get("PATH", "")),
            shutil.which("npm", path=env.get("PATH", "")),
        ),
    )
    if node is None or npm is None:
        return False
    process: asyncio.subprocess.Process | None = None
    try:
        process = await asyncio.create_subprocess_exec(
            node,
            "--version",
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=os.name == "posix",
        )
        assert process.stdout is not None
        async with asyncio.timeout(10):
            output = await process.stdout.read(1025)
            if len(output) > 1024:
                return False
            code = await process.wait()
        match = re.fullmatch(rb"v(\d+)\.(\d+)\.(\d+)\s*", output)
        return code == 0 and match is not None and tuple(map(int, match.groups())) >= _NODE_MINIMUM
    except (OSError, TimeoutError):
        return False
    finally:
        if process is not None and process.returncode is None:
            if os.name == "posix":
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(process.pid, signal.SIGKILL)
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()


def _check_cancelled(cancelled: threading.Event) -> None:
    if cancelled.is_set():
        raise RuntimeError("runtime_install_cancelled")


def _download(url: str, archive: Path, expected: str | None, cancelled: threading.Event) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "Octop-ACP-Setup"})
    digest = hashlib.sha256()
    size = 0
    with urllib.request.urlopen(request, timeout=_READ_TIMEOUT) as response:
        final = urlsplit(response.geturl())
        if final.scheme != "https" or final.hostname != urlsplit(url).hostname:
            raise RuntimeError("runtime_download_redirect")
        content_length = response.headers.get("Content-Length")
        if content_length is not None and int(content_length) > _MAX_DOWNLOAD_BYTES:
            raise RuntimeError("runtime_download_too_large")
        with archive.open("xb") as target:
            while True:
                _check_cancelled(cancelled)
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > _MAX_DOWNLOAD_BYTES:
                    raise RuntimeError("runtime_download_too_large")
                target.write(chunk)
                digest.update(chunk)
    if expected is not None and digest.hexdigest() != expected:
        raise RuntimeError("runtime_checksum_mismatch")


def _archive_path(name: str) -> tuple[str, ...]:
    path = PurePosixPath(name)
    if path.is_absolute() or "\\" in name or any(part in {"..", ""} for part in path.parts):
        raise RuntimeError("runtime_unsafe_archive")
    if not path.parts or ":" in path.parts[0]:
        raise RuntimeError("runtime_unsafe_archive")
    return path.parts


def _extract_archive(archive: Path, destination: Path, cancelled: threading.Event) -> None:
    """Strip one common archive root and reject traversal, devices, and escaping links."""
    total = 0
    root: str | None = None
    links: list[tuple[Path, Path, str, bool]] = []
    with tarfile.open(archive, "r:*") as package:
        for index, member in enumerate(package):
            _check_cancelled(cancelled)
            if index >= _MAX_MEMBERS:
                raise RuntimeError("runtime_archive_too_large")
            parts = _archive_path(member.name)
            if root is None:
                root = parts[0]
            if parts[0] != root:
                raise RuntimeError("runtime_unsafe_archive")
            if len(parts) == 1:
                if not member.isdir():
                    raise RuntimeError("runtime_unsafe_archive")
                continue
            relative = Path(*parts[1:])
            target = destination / relative
            if not target.resolve().is_relative_to(destination.resolve()):
                raise RuntimeError("runtime_unsafe_archive")
            target.parent.mkdir(parents=True, exist_ok=True)
            if member.isdir():
                target.mkdir(exist_ok=True)
            elif member.isfile():
                total += member.size
                if member.size < 0 or total > _MAX_EXTRACT_BYTES:
                    raise RuntimeError("runtime_archive_too_large")
                source = package.extractfile(member)
                if source is None:
                    raise RuntimeError("runtime_invalid_archive")
                with source, target.open("xb") as output:
                    while chunk := source.read(64 * 1024):
                        _check_cancelled(cancelled)
                        output.write(chunk)
                target.chmod(member.mode & 0o777)
            elif member.issym() or member.islnk():
                link_parts = _archive_path(member.linkname) if member.islnk() else ()
                if member.islnk():
                    if link_parts[0] != root or len(link_parts) == 1:
                        raise RuntimeError("runtime_unsafe_archive")
                    link_target = destination / Path(*link_parts[1:])
                else:
                    if member.linkname.startswith("/") or "\\" in member.linkname:
                        raise RuntimeError("runtime_unsafe_archive")
                    normalized = posixpath.normpath(
                        posixpath.join(relative.parent.as_posix(), member.linkname)
                    )
                    if normalized == ".." or normalized.startswith("../"):
                        raise RuntimeError("runtime_unsafe_archive")
                    link_target = destination / normalized
                links.append((target, link_target, member.linkname, member.issym()))
            else:
                raise RuntimeError("runtime_unsafe_archive")
    for target, link_target, link_name, symbolic in links:
        _check_cancelled(cancelled)
        if not link_target.resolve().is_relative_to(destination.resolve()):
            raise RuntimeError("runtime_unsafe_archive")
        if symbolic:
            target.symlink_to(link_name)
        elif link_target.is_file() and not link_target.is_symlink():
            os.link(link_target, target)
        else:
            raise RuntimeError("runtime_unsafe_archive")


def _install_archive(
    destination: Path,
    url: str,
    expected: str | None,
    kind: str,
    version: str,
    required: tuple[str, ...],
    cancelled: threading.Event,
    progress: Progress,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}-install-", dir=destination.parent))
    try:
        archive = staging / "package.tar"
        progress("downloading_runtime" if kind == "node" else "downloading_runner")
        _download(url, archive, expected, cancelled)
        progress("installing_runtime" if kind == "node" else "installing_runner")
        extracted = staging / "extracted"
        extracted.mkdir()
        _extract_archive(archive, extracted, cancelled)
        if not all((extracted / name).is_file() for name in required):
            raise RuntimeError("runtime_invalid_layout")
        for name in required:
            if not os.access(extracted / name, os.X_OK):
                raise RuntimeError("runtime_invalid_permissions")
        (extracted / _MARKER).write_text(
            json.dumps({"kind": kind, "version": version}), encoding="utf-8"
        )
        _check_cancelled(cancelled)
        previous = staging / "previous"
        if destination.exists():
            destination.rename(previous)
        try:
            extracted.rename(destination)
        except OSError:
            if previous.exists():
                previous.rename(destination)
            raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)


async def _install_in_executor(
    destination: Path,
    url: str,
    expected: str | None,
    kind: str,
    version: str,
    required: tuple[str, ...],
    progress: Progress | None,
) -> None:
    loop = asyncio.get_running_loop()
    cancelled = threading.Event()

    def report(key: str) -> None:
        if progress is not None:
            loop.call_soon_threadsafe(progress, key)

    future = loop.run_in_executor(
        None,
        partial(
            _install_archive,
            destination,
            url,
            expected,
            kind,
            version,
            required,
            cancelled,
            report,
        ),
    )
    try:
        await asyncio.wait_for(asyncio.shield(future), _DOWNLOAD_TIMEOUT)
    except asyncio.CancelledError:
        cancelled.set()
        with contextlib.suppress(Exception):
            await asyncio.shield(future)
        raise
    except TimeoutError as exc:
        cancelled.set()
        with contextlib.suppress(Exception):
            await asyncio.shield(future)
        raise RuntimeError("runtime_install_timeout") from exc
    except Exception as exc:
        raise RuntimeError("runtime_install_failed") from exc


async def ensure_node(
    acp_root: Path, env: dict[str, str], progress: Progress | None = None
) -> dict[str, str]:
    """Reuse a suitable Node/npm or install a verified private Linux runtime."""
    if progress is not None:
        progress("preparing")
    if await _node_is_usable(env):
        return dict(env)
    loop = asyncio.get_running_loop()
    destination = acp_root / "runtime" / "node"
    async with _install_lock(destination):
        managed = await loop.run_in_executor(None, runtime_environment, acp_root, env)
        if managed != env and await _node_is_usable(managed):
            return managed
        architecture = _linux_architecture()
        await _install_in_executor(
            destination,
            f"https://nodejs.org/dist/v{_NODE_VERSION}/node-v{_NODE_VERSION}-linux-{architecture}.tar.xz",
            _NODE_SHA256[architecture],
            "node",
            _NODE_VERSION,
            ("bin/node", "bin/npm"),
            progress,
        )
        managed = await loop.run_in_executor(None, runtime_environment, acp_root, env)
        if not await _node_is_usable(managed):
            await loop.run_in_executor(
                None, partial((destination / _MARKER).unlink, missing_ok=True)
            )
            raise RuntimeError("runtime_not_executable")
        return managed


async def install_cursor(
    acp_root: Path, env: dict[str, str], progress: Progress | None = None
) -> list[str]:
    """Install Cursor's fixed native package without running its home-mutating installer."""
    destination = acp_root / "runners" / "cursor_cli"
    loop = asyncio.get_running_loop()
    async with _install_lock(destination):
        installed = await loop.run_in_executor(None, cursor_launch, acp_root)
        if installed is not None:
            return installed
        architecture = _linux_architecture()
        await _install_in_executor(
            destination,
            f"https://downloads.cursor.com/lab/{_CURSOR_VERSION}/linux/{architecture}/agent-cli-package.tar.gz",
            None,
            "cursor",
            _CURSOR_VERSION,
            ("cursor-agent",),
            progress,
        )
        installed = await loop.run_in_executor(None, cursor_launch, acp_root)
        if installed is None:
            raise RuntimeError("runtime_invalid_layout")
        return installed
