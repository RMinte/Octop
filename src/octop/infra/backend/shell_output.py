"""Decode host-shell output without trusting the locale (issue #1959).

``HarnessLocalShellBackend`` and ``BubbledLocalShellBackend`` run commands with
``text=True`` and no ``encoding``. Python then decodes the pipe with
``locale.getpreferredencoding(False)`` — cp936 on a Chinese Windows host.
Tools such as Python, git, and ripgrep emit UTF-8. On Windows the mismatch
raises inside ``subprocess``'s reader thread, the exception is discarded, and
``execute`` reports ``<no output>`` with exit code 0.

``cmd`` builtins emit the console code page, so the bytes are not forced to
UTF-8. Try strict UTF-8, then the locale code page, then GBK, and only then
replacement characters.

This is a process-start stand-in for shell implementations that do not yet
ship ``decode_child_output``. Native decoding is checked per module: a newer
harness must not leave the upstream deepagents shell using the host code page.
"""

from __future__ import annotations

import importlib
import locale
import logging
import subprocess
from typing import Any

logger = logging.getLogger(__name__)

_SHELL_MODULES = (
    "deepagents.backends.local_shell",
    "octop_harness.backends.local_shell",
    "octop_harness.backends.bwrap_shell",
)
_INSTALLED = False


def decode_child_output(data: bytes | str | None) -> str:
    """Decode one captured stream. Never raises."""
    if not data:
        return ""
    if isinstance(data, str):
        return data
    seen: set[str] = set()
    for encoding in ("utf-8", locale.getpreferredencoding(False), "gbk"):
        if not encoding or encoding.lower() in seen:
            continue
        seen.add(encoding.lower())
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


class _ShellSubprocess:
    """``subprocess`` stand-in for shell backends without explicit decoding."""

    __slots__ = ("_real",)

    def __init__(self, real: Any) -> None:
        self._real = real

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    def run(self, *args: Any, **kwargs: Any) -> Any:
        text_mode = bool(kwargs.get("text") or kwargs.get("universal_newlines"))
        if not text_mode or kwargs.get("encoding"):
            return self._real.run(*args, **kwargs)
        forwarded = dict(kwargs)
        forwarded.pop("text", None)
        forwarded.pop("universal_newlines", None)
        result = self._real.run(*args, **forwarded)
        stdout = result.stdout
        stderr = result.stderr
        if not isinstance(stdout, bytes) and not isinstance(stderr, bytes):
            return result
        return subprocess.CompletedProcess(
            result.args,
            result.returncode,
            decode_child_output(stdout) if isinstance(stdout, bytes) else stdout,
            decode_child_output(stderr) if isinstance(stderr, bytes) else stderr,
        )


def install_shell_output_decoding() -> None:
    """Patch each legacy shell independently, leaving native decoders in place."""
    global _INSTALLED
    if _INSTALLED:
        return
    for module_name in _SHELL_MODULES:
        module = importlib.import_module(module_name)
        if callable(getattr(module, "decode_child_output", None)):
            continue
        current = module.subprocess
        if not isinstance(current, _ShellSubprocess):
            module.subprocess = _ShellSubprocess(current)  # type: ignore[attr-defined]
    _INSTALLED = True
    logger.info("installed shell output decoding (utf-8, locale, gbk)")
