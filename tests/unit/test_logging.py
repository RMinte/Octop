"""tests/unit/test_logging.py

Covers the daily-rotation + retention logging strategy in ``octop.infra.server``:
the log path lives under ``~/.octop/logs`` and stale rotated files are purged.
"""

from __future__ import annotations

import gzip
import logging
import os
import sys
import time
from collections.abc import Iterator
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

import pytest

from octop.infra.server import (
    DEFAULT_LOG_MAX_BYTES,
    OctopServer,
    SizeTimedRotatingFileHandler,
    _attach_log_handler,
    _build_log_handler,
    _parse_log_compress,
    _parse_log_max_bytes,
    _purge_stale_logs,
    delaycompress_rotated_logs,
    gzip_rotated_log,
)


@pytest.fixture(autouse=True)
def _isolate_root_logger() -> Iterator[None]:
    """Restore the global root/uvicorn loggers so tests never leak handlers/levels."""
    root = logging.getLogger()
    saved_root_handlers = list(root.handlers)
    saved_root_level = root.level
    saved_child = {
        name: (
            list(target.handlers),
            list(target.filters),
            target.level,
            target.propagate,
            target.disabled,
        )
        for name in ("uvicorn", "uvicorn.access", "uvicorn.error", "uvicorn.asgi")
        for target in (logging.getLogger(name),)
    }
    yield
    original_handlers = set(saved_root_handlers)
    original_handlers.update(h for saved in saved_child.values() for h in saved[0])
    current_handlers = set(root.handlers)
    current_handlers.update(h for name in saved_child for h in logging.getLogger(name).handlers)
    for handler in current_handlers - original_handlers:
        handler.close()
    root.handlers = saved_root_handlers
    root.setLevel(saved_root_level)
    for name, (handlers, filters, level, propagate, disabled) in saved_child.items():
        target = logging.getLogger(name)
        target.handlers = handlers
        target.filters = filters
        target.setLevel(level)
        target.propagate = propagate
        target.disabled = disabled


def _stale_path(log_dir: Path, name: str, age_days: int) -> Path:
    """Create a rotated log file with an mtime shifted ``age_days`` into the past."""
    path = log_dir / name
    path.write_text("old\n", encoding="utf-8")
    past = time.time() - age_days * 86400
    os.utime(path, (past, past))
    return path


def test_build_log_handler_uses_daily_rotation_and_retention(tmp_path: Path):
    handler = _build_log_handler(tmp_path / "octop.log", retention_days=7, max_bytes=4096)
    try:
        assert isinstance(handler, SizeTimedRotatingFileHandler)
        assert handler.when == "MIDNIGHT"
        assert handler.backupCount == 7
        assert handler.max_bytes == 4096
        # Date-only suffix keeps filenames valid on Windows (no ':' like %H:%M:%S).
        assert handler.suffix == "%Y-%m-%d"
        assert ":" not in handler.suffix
    finally:
        handler.close()


def test_purge_stale_logs_removes_only_old_files(tmp_path: Path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    old = _stale_path(log_dir, "octop.log.2020-01-01", age_days=100)
    new = _stale_path(log_dir, "octop.log.2026-07-16", age_days=1)

    _purge_stale_logs(log_dir, retention_days=14)

    assert not old.exists()
    assert new.exists()


def test_purge_stale_logs_skipped_when_retention_non_positive(tmp_path: Path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    old = _stale_path(log_dir, "octop.log.2020-01-01", age_days=100)

    _purge_stale_logs(log_dir, retention_days=0)

    assert old.exists()


def test_attach_log_handler_is_idempotent(tmp_path: Path):
    root = logging.getLogger()
    handler = _build_log_handler(tmp_path / "octop.log", retention_days=7)
    try:
        _attach_log_handler(root, handler)
        _attach_log_handler(root, handler)
        matching = [
            h
            for h in root.handlers
            if isinstance(h, TimedRotatingFileHandler) and h.baseFilename == handler.baseFilename
        ]
        assert len(matching) == 1
    finally:
        handler.close()


def test_setup_logging_creates_logs_dir_and_file(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("OCTOP_LOG_LEVEL", raising=False)
    monkeypatch.delenv("OCTOP_LOG_RETENTION_DAYS", raising=False)
    server = OctopServer(home=tmp_path / ".octop")

    server._setup_logging()

    assert server.paths.logs_dir.is_dir()
    root = logging.getLogger()
    handler = next(
        h
        for h in root.handlers
        if isinstance(h, TimedRotatingFileHandler) and h.baseFilename == str(server.paths.log)
    )
    # Emit a record and confirm it lands in the logs directory file.
    root.info("logging smoke test")
    handler.flush()
    assert server.paths.log.exists()
    assert "logging smoke test" in server.paths.log.read_text(encoding="utf-8")
    assert root.level == logging.INFO


def test_setup_logging_migrates_legacy_log(tmp_path: Path):
    home = tmp_path / ".octop"
    legacy = home / "octop.log"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("legacy content", encoding="utf-8")
    server = OctopServer(home=home)

    server._setup_logging()

    assert not legacy.exists()
    assert server.paths.log.exists()
    assert server.paths.log.read_text(encoding="utf-8") == "legacy content"


def test_setup_logging_respects_retention_env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OCTOP_LOG_RETENTION_DAYS", "3")
    server = OctopServer(home=tmp_path / ".octop")

    server._setup_logging()

    root = logging.getLogger()
    handler = next(
        h
        for h in root.handlers
        if isinstance(h, TimedRotatingFileHandler) and h.baseFilename == str(server.paths.log)
    )
    assert handler.backupCount == 3


def test_setup_logging_respects_level_env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OCTOP_LOG_LEVEL", "debug")
    server = OctopServer(home=tmp_path / ".octop")

    server._setup_logging()

    assert logging.getLogger().level == logging.DEBUG


def test_setup_logging_falls_back_on_invalid_level_env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OCTOP_LOG_LEVEL", "not-a-level")
    server = OctopServer(home=tmp_path / ".octop")

    server._setup_logging()

    assert logging.getLogger().level == logging.INFO


def test_setup_logging_falls_back_on_invalid_retention_env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OCTOP_LOG_RETENTION_DAYS", "oops")
    server = OctopServer(home=tmp_path / ".octop")

    server._setup_logging()

    root = logging.getLogger()
    handler = next(
        h
        for h in root.handlers
        if isinstance(h, TimedRotatingFileHandler) and h.baseFilename == str(server.paths.log)
    )
    # Default retention is 14 days.
    assert handler.backupCount == 14


def test_parse_log_max_bytes_defaults_and_env(monkeypatch):
    monkeypatch.delenv("OCTOP_LOG_MAX_BYTES", raising=False)
    assert _parse_log_max_bytes() == DEFAULT_LOG_MAX_BYTES
    monkeypatch.setenv("OCTOP_LOG_MAX_BYTES", "2048")
    assert _parse_log_max_bytes() == 2048
    monkeypatch.setenv("OCTOP_LOG_MAX_BYTES", "bad")
    assert _parse_log_max_bytes() == DEFAULT_LOG_MAX_BYTES


def test_log_handler_rotates_when_max_bytes_exceeded(tmp_path: Path):
    log_path = tmp_path / "octop.log"
    handler = _build_log_handler(log_path, retention_days=3, max_bytes=256)
    test_logger = logging.getLogger("test.octop.log.size_roll")
    test_logger.handlers = []
    test_logger.propagate = False
    test_logger.setLevel(logging.INFO)
    test_logger.addHandler(handler)
    try:
        for _ in range(40):
            test_logger.info("x" * 40)
        handler.flush()
        backups = [p for p in tmp_path.glob("octop.log*") if p != log_path]
        assert len(backups) >= 1
        # delaycompress: newest rotated file stays plain; older ones may be .gz
        plains = [p for p in backups if not p.name.endswith(".gz")]
        gzips = [p for p in backups if p.name.endswith(".gz")]
        assert len(plains) == 1
        if len(backups) > 1:
            assert len(gzips) >= 1
        assert log_path.exists()
        assert log_path.stat().st_size < 512
    finally:
        test_logger.handlers = []
        handler.close()


def test_gzip_rotated_log_replaces_plain_file(tmp_path: Path):
    plain = tmp_path / "octop.log.2026-09-02"
    # Binary write keeps LF on Windows (text mode would expand to CRLF).
    plain.write_bytes(b"hello\nworld\n")
    gzip_rotated_log(plain)
    gz = tmp_path / "octop.log.2026-09-02.gz"
    assert not plain.exists()
    assert gz.exists()
    assert gzip.decompress(gz.read_bytes()) == b"hello\nworld\n"


def test_delaycompress_keeps_newest_plain_on_startup(tmp_path: Path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    older = log_dir / "octop.log.2026-08-28"
    newer = log_dir / "octop.log.2026-08-29"
    older.write_text("older\n", encoding="utf-8")
    newer.write_text("newer\n", encoding="utf-8")
    past = time.time() - 86400
    os.utime(older, (past, past))
    delaycompress_rotated_logs(log_dir, enabled=True)
    assert not older.exists()
    assert (log_dir / "octop.log.2026-08-28.gz").exists()
    assert newer.exists()
    assert not (log_dir / "octop.log.2026-08-29.gz").exists()


def test_delaycompress_single_plain_stays_uncompressed(tmp_path: Path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    plain = log_dir / "octop.log.2026-08-29"
    plain.write_text("only\n", encoding="utf-8")
    delaycompress_rotated_logs(log_dir, enabled=True)
    assert plain.exists()
    assert not (log_dir / "octop.log.2026-08-29.gz").exists()


def test_parse_log_compress_env(monkeypatch):
    monkeypatch.delenv("OCTOP_LOG_COMPRESS", raising=False)
    assert _parse_log_compress() is True
    monkeypatch.setenv("OCTOP_LOG_COMPRESS", "0")
    assert _parse_log_compress() is False


def test_purge_stale_logs_removes_old_gz_files(tmp_path: Path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    old = _stale_path(log_dir, "octop.log.2020-01-01.gz", age_days=100)
    new = _stale_path(log_dir, "octop.log.2026-07-16.gz", age_days=1)
    _purge_stale_logs(log_dir, retention_days=14)
    assert not old.exists()
    assert new.exists()


def test_setup_logging_respects_max_bytes_env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OCTOP_LOG_MAX_BYTES", "8192")
    server = OctopServer(home=tmp_path / ".octop")
    server._setup_logging()
    handler = next(
        h
        for h in logging.getLogger().handlers
        if isinstance(h, SizeTimedRotatingFileHandler) and h.baseFilename == str(server.paths.log)
    )
    assert handler.max_bytes == 8192


@pytest.mark.parametrize("listener_count", [1, 2])
@pytest.mark.parametrize("log_level", ["info", "debug", "trace"])
def test_uvicorn_config_preserves_safe_file_logging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    listener_count: int,
    log_level: str,
) -> None:
    """Real Config logging setup must retain the shared file for every listener."""
    import uvicorn

    logging.getLogger().handlers = []
    monkeypatch.setenv("OCTOP_LOG_LEVEL", "debug")
    server = OctopServer(home=tmp_path / ".octop")
    server._setup_logging()
    root = logging.getLogger()
    file_handler = next(
        h
        for h in root.handlers
        if isinstance(h, SizeTimedRotatingFileHandler) and h.baseFilename == str(server.paths.log)
    )
    for _ in range(listener_count):
        uvicorn.Config("fixture:app", log_config=None, log_level=log_level)
    logging.getLogger("octop.api.task_websocket").info("task lifecycle persisted")
    access = logging.getLogger("uvicorn.access")
    protocol = logging.getLogger("uvicorn.error")
    access.info(
        '%s - "%s %s HTTP/%s" %d',
        "127.0.0.1:1234",
        "GET",
        "/api/project-tasks/ws?token=query-secret&other=private",
        "1.1",
        101,
    )
    protocol.info(
        '%s - "WebSocket %s" [accepted]',
        "127.0.0.1:1234",
        "/api/project-tasks/ws?token=ws-secret",
    )
    protocol.debug("< Authorization: Bearer header-secret")
    protocol.debug("< TEXT %s", "session-body-secret")
    logging.getLogger("uvicorn.asgi").log(
        5, "Started scope=%s", {"query_string": b"token=scope-secret"}
    )
    file_handler.flush()

    assert not file_handler._closed
    content = server.paths.log.read_text(encoding="utf-8")
    assert content.count("task lifecycle persisted") == 1
    assert content.count('"GET /api/project-tasks/ws HTTP/1.1" 101') == 1
    assert content.count('"WebSocket /api/project-tasks/ws" [accepted]') == 1
    console_content = capsys.readouterr().err
    assert console_content.count('"GET /api/project-tasks/ws HTTP/1.1" 101') == 1
    assert console_content.count('"WebSocket /api/project-tasks/ws" [accepted]') == 1
    for secret in (
        "query-secret",
        "private",
        "ws-secret",
        "header-secret",
        "session-body-secret",
        "scope-secret",
    ):
        assert secret not in content
        assert secret not in console_content


@pytest.mark.parametrize(
    "logger_name", ["uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi"]
)
def test_uvicorn_error_logs_keep_only_safe_exception_details(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], logger_name: str
) -> None:
    logging.getLogger().handlers = []
    server = OctopServer(home=tmp_path / ".octop")
    server._setup_logging()
    protocol = logging.getLogger(logger_name)

    try:
        try:
            raise ValueError("Bearer cause-credential-secret")
        except ValueError as exc:
            raise RuntimeError("exception-session-body-secret") from exc
    except RuntimeError:
        protocol.exception("connection handler failed", stack_info=True)
        record = protocol.makeRecord(
            logger_name,
            logging.ERROR,
            __file__,
            1,
            "closing handshake failed",
            (),
            sys.exc_info(),
        )
        record.exc_text = "cached-traceback-secret"
        record.stack_info = "stack-source-line-secret"
        protocol.handle(record)
        assert record.exc_info is None and record.exc_text is None and record.stack_info is None
    protocol.error("ASGI callable should return None, but returned '%s'.", "return-payload-secret")
    protocol.warning("skipped broadcast: failed to write message: %s", "broadcast-error-secret")
    for handler in logging.getLogger().handlers:
        handler.flush()

    content = server.paths.log.read_text(encoding="utf-8")
    console_content = capsys.readouterr().err
    for output in (content, console_content):
        assert (
            "connection handler failed exception_type=RuntimeError stack=test_logging.py:" in output
        )
        assert (
            "closing handshake failed exception_type=RuntimeError stack=test_logging.py:" in output
        )
        assert "ASGI callable returned a non-None result" in output
        assert "skipped broadcast: failed to write message" in output
        for secret in (
            "cause-credential-secret",
            "exception-session-body-secret",
            "cached-traceback-secret",
            "stack-source-line-secret",
            "return-payload-secret",
            "broadcast-error-secret",
        ):
            assert secret not in output


@pytest.mark.parametrize("dual_listeners", [False, True])
async def test_foreground_keeps_logging_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dual_listeners: bool
) -> None:
    """Exercise real foreground Config creation without listening or booting services."""
    from octop import launch
    from octop.config import OctopConfig
    from octop.infra.setup.tls.listeners import ListenPlan

    server = OctopServer(home=tmp_path / ".octop")
    config = OctopConfig()
    server.config = config

    async def start() -> None:
        server._setup_logging()

    async def stop() -> None:
        pass

    class MemoryControl:
        def __init__(self, _server: object) -> None:
            pass

        async def start(self) -> None:
            pass

        async def close(self) -> None:
            pass

    listeners = []

    async def serve(instance) -> None:
        listeners.append(instance.config)
        assert instance.config.log_config is None
        logging.getLogger("uvicorn.error").info("listener started %s", instance.config.port)

    plan = ListenPlan(
        bind_host="127.0.0.1",
        https_port=8443 if dual_listeners else None,
        http_port=8080,
        ssl_certfile="fixture-cert" if dual_listeners else None,
        ssl_keyfile="fixture-key" if dual_listeners else None,
    )
    monkeypatch.setattr(launch, "OctopServer", lambda **_kwargs: server)
    monkeypatch.setattr(server, "start", start)
    monkeypatch.setattr(server, "stop", stop)
    monkeypatch.setattr(launch, "_schedule_linux_bubblewrap_ensure", lambda: None)
    monkeypatch.setattr(launch, "_serve", serve)
    monkeypatch.setattr("octop.api.app.build_app", lambda _server: object())
    monkeypatch.setattr("octop.infra.setup.tls.listeners.build_listen_plan", lambda *_a, **_k: plan)
    monkeypatch.setattr("octop.infra.agents.memory.slim_control.MemorySlimControl", MemoryControl)

    await launch.run_foreground(
        host="127.0.0.1",
        port=8080,
        reload=False,
        workers=1,
        log_level="info",
        ssl_certfile=None,
        ssl_keyfile=None,
    )
    for handler in logging.getLogger().handlers:
        handler.flush()
    content = server.paths.log.read_text(encoding="utf-8")
    assert len(listeners) == (2 if dual_listeners else 1)
    assert content.count("listener started 8080") == 1
    assert content.count("listener started 8443") == (1 if dual_listeners else 0)
