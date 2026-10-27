"""Private runtime installation is bounded, verified, atomic, and entirely mockable."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import shutil
import tarfile
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from octop.infra.agents import acp_install_runtime as runtime

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX file modes and symlinks")


def archive_file(tmp_path: Path, files: dict[str, bytes], *, mode: int = 0o755) -> Path:
    target = tmp_path / "fixture.tar.gz"
    with tarfile.open(target, "w:gz") as archive:
        for name, data in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            member.mode = mode
            archive.addfile(member, io.BytesIO(data))
    return target


@pytest.fixture
def linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime.platform, "system", lambda: "Linux")
    monkeypatch.setattr(runtime.platform, "machine", lambda: "x86_64")


@pytest.mark.parametrize(
    ("machine", "expected"),
    [("x86_64", "x64"), ("amd64", "x64"), ("aarch64", "arm64"), ("arm64", "arm64")],
)
def test_supported_linux_architectures(
    linux: None, monkeypatch: pytest.MonkeyPatch, machine: str, expected: str
) -> None:
    monkeypatch.setattr(runtime.platform, "machine", lambda: machine)
    assert runtime._linux_architecture() == expected


def test_unsupported_platform_has_no_download_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime.platform, "system", lambda: "Darwin")
    with pytest.raises(RuntimeError, match="unsupported_runtime_platform"):
        runtime._linux_architecture()
    monkeypatch.setattr(runtime.platform, "system", lambda: "Linux")
    monkeypatch.setattr(runtime.platform, "machine", lambda: "armv7l")
    with pytest.raises(RuntimeError, match="unsupported_runtime_architecture"):
        runtime._linux_architecture()


@pytest.mark.asyncio
async def test_existing_suitable_node_is_reused_without_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime, "_node_is_usable", AsyncMock(return_value=True))
    install = AsyncMock(side_effect=AssertionError("must not download"))
    monkeypatch.setattr(runtime, "_install_in_executor", install)
    env = {"PATH": "original-path", "HOME": "original-home"}
    assert await runtime.ensure_node(tmp_path, env) == env
    assert env == {"PATH": "original-path", "HOME": "original-home"}
    install.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("output", "exit_code", "expected"),
    [
        (b"v22.19.0\n", 0, True),
        (b"v24.0.0\n", 0, True),
        (b"v22.18.9\n", 0, False),
        (b"v22.23.4\n", 1, False),
        (b"unexpected version", 0, False),
    ],
)
async def test_node_reuse_checks_actual_version_and_exit_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    output: bytes,
    exit_code: int,
    expected: bool,
) -> None:
    monkeypatch.setattr(runtime.shutil, "which", lambda name, path: str(tmp_path / name))
    process = SimpleNamespace(
        stdout=SimpleNamespace(read=AsyncMock(return_value=output)),
        returncode=exit_code,
        wait=AsyncMock(return_value=exit_code),
    )
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    assert await runtime._node_is_usable({"PATH": "bin"}) is expected
    assert spawn.await_args.args == (str(tmp_path / "node"), "--version")


@pytest.mark.asyncio
async def test_node_install_uses_fixed_hash_and_is_shared_across_runners(
    linux: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = archive_file(
        tmp_path,
        {
            "node-package/bin/node": b"node fixture",
            "node-package/bin/npm": b"npm fixture",
            "node-package/lib/node_modules/npm/bin/npm-cli.js": b"npm fixture",
        },
    )
    root = tmp_path / "acp"
    calls: list[tuple[str, str | None]] = []

    def download(url: str, target: Path, expected: str | None, cancel: threading.Event) -> None:
        calls.append((url, expected))
        shutil.copyfile(fixture, target)

    async def usable(env: dict[str, str]) -> bool:
        return env.get("PATH", "").split(os.pathsep)[0] == str(root / "runtime/node/bin")

    monkeypatch.setattr(runtime, "_download", download)
    monkeypatch.setattr(runtime, "_node_is_usable", usable)
    progress: list[str] = []
    env = {"PATH": "system-bin", "HOME": "unchanged-home"}
    first, second = await asyncio.gather(
        runtime.ensure_node(root, env, progress.append), runtime.ensure_node(root, env)
    )
    assert first == second
    assert first["PATH"].split(os.pathsep) == [str(root / "runtime/node/bin"), "system-bin"]
    assert first["HOME"] == env["HOME"] and env["PATH"] == "system-bin"
    assert calls == [
        (
            "https://nodejs.org/dist/v22.23.4/node-v22.23.4-linux-x64.tar.xz",
            runtime._NODE_SHA256["x64"],
        )
    ]
    assert "downloading_runtime" in progress and "installing_runtime" in progress
    assert (root / "runtime/node/.octop-install-complete").is_file()
    assert not list((root / "runtime").glob(".node-install-*"))


class DownloadResponse(io.BytesIO):
    headers: dict[str, str] = {}

    def geturl(self) -> str:
        return "https://nodejs.org/test-fixture"


def test_download_rejects_bad_hash_and_oversized_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        runtime.urllib.request, "urlopen", lambda *args, **kwargs: DownloadResponse(b"archive")
    )
    with pytest.raises(RuntimeError, match="runtime_checksum_mismatch"):
        runtime._download(
            "https://nodejs.org/test-fixture", tmp_path / "bad", "0" * 64, threading.Event()
        )
    expected = hashlib.sha256(b"archive").hexdigest()
    runtime._download(
        "https://nodejs.org/test-fixture", tmp_path / "good", expected, threading.Event()
    )
    monkeypatch.setattr(runtime, "_MAX_DOWNLOAD_BYTES", 2)
    with pytest.raises(RuntimeError, match="runtime_download_too_large"):
        runtime._download(
            "https://nodejs.org/test-fixture", tmp_path / "large", None, threading.Event()
        )


@pytest.mark.parametrize("name", ["../outside", "root/../../outside", "/root/absolute"])
def test_archive_rejects_path_traversal(tmp_path: Path, name: str) -> None:
    fixture = archive_file(tmp_path, {name: b"unsafe"})
    directory = tmp_path / "extracted"
    directory.mkdir()
    with pytest.raises(RuntimeError, match="runtime_unsafe_archive"):
        runtime._extract_archive(fixture, directory, threading.Event())
    assert not (tmp_path / "outside").exists()


@posix_only
def test_node_style_relative_symlinks_work_but_escaping_links_are_rejected(tmp_path: Path) -> None:
    fixture = tmp_path / "links.tar.gz"
    with tarfile.open(fixture, "w:gz") as archive:
        data = b"npm fixture"
        regular = tarfile.TarInfo("root/lib/npm.js")
        regular.size, regular.mode = len(data), 0o755
        archive.addfile(regular, io.BytesIO(data))
        link = tarfile.TarInfo("root/bin/npm")
        link.type, link.linkname = tarfile.SYMTYPE, "../lib/npm.js"
        archive.addfile(link)
    directory = tmp_path / "extracted"
    directory.mkdir()
    runtime._extract_archive(fixture, directory, threading.Event())
    assert (directory / "bin/npm").read_bytes() == b"npm fixture"

    with tarfile.open(fixture, "w:gz") as archive:
        link.linkname = "../../outside"
        archive.addfile(link)
    with pytest.raises(RuntimeError, match="runtime_unsafe_archive"):
        runtime._extract_archive(fixture, directory, threading.Event())


@posix_only
def test_archive_preserves_execute_permission_without_setuid(tmp_path: Path) -> None:
    fixture = archive_file(tmp_path, {"root/bin/node": b"node"}, mode=0o4755)
    directory = tmp_path / "extracted"
    directory.mkdir()
    runtime._extract_archive(fixture, directory, threading.Event())
    assert (directory / "bin/node").stat().st_mode & 0o7777 == 0o755


def test_passive_helpers_require_complete_marker_and_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        runtime.urllib.request, "urlopen", lambda *args, **kwargs: pytest.fail("passive download")
    )
    directory = tmp_path / "runtime/node/bin"
    directory.mkdir(parents=True)
    for name in ("node", "npm"):
        (directory / name).write_bytes(b"fixture")
        (directory / name).chmod(0o755)
    env = {"PATH": "unchanged"}
    assert runtime.runtime_environment(tmp_path, env) == env
    (directory.parent / runtime._MARKER).write_text(
        json.dumps({"kind": "node", "version": runtime._NODE_VERSION}), encoding="utf-8"
    )
    assert runtime.runtime_environment(tmp_path, env)["PATH"].startswith(str(directory))
    (directory / "npm").unlink()
    assert runtime.runtime_environment(tmp_path, env) == env
    assert runtime.cursor_launch(tmp_path) is None


@pytest.mark.asyncio
async def test_cursor_installs_official_fixed_arm64_archive_and_reuses_complete_install(
    linux: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime.platform, "machine", lambda: "aarch64")
    fixture = archive_file(tmp_path, {"agent-cli-package/cursor-agent": b"native cursor"})
    calls: list[str] = []

    def download(url: str, target: Path, expected: str | None, cancel: threading.Event) -> None:
        calls.append(url)
        assert expected is None
        shutil.copyfile(fixture, target)

    monkeypatch.setattr(runtime, "_download", download)
    root = tmp_path / "acp"
    progress: list[str] = []
    launch = await runtime.install_cursor(root, {}, progress.append)
    assert launch == [str(root / "runners/cursor_cli/cursor-agent"), "acp"]
    assert await runtime.install_cursor(root, {}) == launch
    assert calls == [
        "https://downloads.cursor.com/lab/2026.10.22-27dcf5f/linux/arm64/agent-cli-package.tar.gz"
    ]
    assert progress == ["downloading_runner", "installing_runner"]
    assert runtime.cursor_launch(root) == launch


@pytest.mark.asyncio
async def test_failed_install_does_not_publish_marker_or_leave_staging_files(
    linux: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = archive_file(tmp_path, {"root/unrelated": b"missing entry point"})

    def download(url: str, target: Path, expected: str | None, cancel: threading.Event) -> None:
        shutil.copyfile(fixture, target)

    monkeypatch.setattr(runtime, "_download", download)
    root = tmp_path / "acp"
    with pytest.raises(RuntimeError, match="runtime_install_failed"):
        await runtime.install_cursor(root, {})
    assert runtime.cursor_launch(root) is None
    assert not list((root / "runners").iterdir())


@pytest.mark.asyncio
async def test_cancellation_stops_worker_and_removes_partial_download(
    linux: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = threading.Event()

    def download(url: str, target: Path, expected: str | None, cancel: threading.Event) -> None:
        target.write_bytes(b"partial")
        started.set()
        cancel.wait(5)
        runtime._check_cancelled(cancel)

    monkeypatch.setattr(runtime, "_download", download)
    root = tmp_path / "acp"
    task = asyncio.create_task(runtime.install_cursor(root, {}))
    assert await asyncio.get_running_loop().run_in_executor(None, started.wait, 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not list((root / "runners").iterdir())
    assert runtime.cursor_launch(root) is None


@pytest.mark.asyncio
async def test_timeout_is_runtime_error_and_cleans_up(
    linux: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime, "_DOWNLOAD_TIMEOUT", 0.01)

    def download(url: str, target: Path, expected: str | None, cancel: threading.Event) -> None:
        target.write_bytes(b"partial")
        cancel.wait(5)
        runtime._check_cancelled(cancel)

    monkeypatch.setattr(runtime, "_download", download)
    root = tmp_path / "acp"
    with pytest.raises(RuntimeError, match="runtime_install_timeout"):
        await runtime.install_cursor(root, {})
    assert not list((root / "runners").iterdir())
