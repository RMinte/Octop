"""Immutable, content-checked skill snapshots owned by one task turn."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
from pathlib import Path, PureWindowsPath
from typing import Any

from octop.infra.skills.skill_packages import normalize_skill_files, validate_skill_slug

_FILENAME = "skills.json"
_VERSION = 1


class SnapshotError(ValueError):
    """A turn's saved skills are invalid or do not match its resource selection."""


def _encoded(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _configuration_digest(configuration_json: str) -> str:
    configuration = json.loads(configuration_json)
    if not isinstance(configuration, dict):
        raise SnapshotError("invalid snapshot configuration")
    # Approval decisions and live credentials are deliberately outside the snapshot.
    selection = {key: configuration.get(key, []) for key in ("skills", "plugins")}
    return hashlib.sha256(_encoded(selection)).hexdigest()


def _validated(
    files: list[tuple[str, list[tuple[str, bytes]]]],
) -> list[tuple[str, list[tuple[str, bytes]]]]:
    result = []
    seen = set()
    for slug, entries in files:
        if validate_skill_slug(slug) != slug or PureWindowsPath(slug).drive or slug in seen:
            raise SnapshotError("invalid or duplicate snapshot skill")
        seen.add(slug)
        if any(PureWindowsPath(name).drive for name, _ in entries):
            raise SnapshotError("invalid snapshot path")
        result.append((slug, list(normalize_skill_files(entries))))
    return result


def load_snapshot(
    root: Path, configuration_json: str
) -> list[tuple[str, list[tuple[str, bytes]]]] | None:
    """Read a complete snapshot; damaged snapshots must never trigger a fresh download."""
    try:
        payload = (root / _FILENAME).read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SnapshotError("snapshot cannot be read") from exc
    try:
        document = json.loads(payload)
        if not isinstance(document, dict):
            raise SnapshotError("invalid snapshot document")
        digest = document.pop("sha256")
        if (
            document.get("version") != _VERSION
            or document.get("configuration_sha256") != _configuration_digest(configuration_json)
            or digest != hashlib.sha256(_encoded(document)).hexdigest()
            or not isinstance(document.get("skills"), list)
        ):
            raise SnapshotError("snapshot integrity check failed")
        files = []
        for skill in document["skills"]:
            if not isinstance(skill, dict) or not isinstance(skill.get("slug"), str):
                raise SnapshotError("invalid snapshot skill")
            if not isinstance(skill.get("files"), list):
                raise SnapshotError("invalid snapshot files")
            entries = []
            for entry in skill["files"]:
                if (
                    not isinstance(entry, dict)
                    or not isinstance(entry.get("path"), str)
                    or not isinstance(entry.get("base64"), str)
                ):
                    raise SnapshotError("invalid snapshot file")
                entries.append((entry["path"], base64.b64decode(entry["base64"], validate=True)))
            files.append((skill["slug"], entries))
        return _validated(files)
    except (ValueError, TypeError, KeyError, UnicodeError) as exc:
        raise SnapshotError("snapshot integrity check failed") from exc


def save_snapshot(
    root: Path, configuration_json: str, files: list[tuple[str, list[tuple[str, bytes]]]]
) -> None:
    """Atomically publish all validated skills without storing runtime credentials."""
    try:
        document: dict[str, Any] = {
            "version": _VERSION,
            "configuration_sha256": _configuration_digest(configuration_json),
            "skills": [
                {
                    "slug": slug,
                    "files": [
                        {"path": name, "base64": base64.b64encode(data).decode("ascii")}
                        for name, data in entries
                    ],
                }
                for slug, entries in _validated(files)
            ],
        }
        document["sha256"] = hashlib.sha256(_encoded(document)).hexdigest()
        payload = _encoded(document)
    except (ValueError, TypeError, UnicodeError) as exc:
        raise SnapshotError("invalid snapshot resources") from exc
    temporary: Path | None = None
    try:
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=root, prefix=".skills-", delete=False) as target:
            temporary = Path(target.name)
            target.write(payload)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, root / _FILENAME)
    except OSError as exc:
        raise SnapshotError("snapshot cannot be saved") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
