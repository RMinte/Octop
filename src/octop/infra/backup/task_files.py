"""Archive Octop-owned task files without following project or client paths."""

from __future__ import annotations

import os
import shutil
import tarfile
from collections.abc import Iterator
from pathlib import Path

from octop.infra.backup.snapshot import snapshot_sqlite_file

TASK_FILES_DIR = "project-tasks"
EXPERT_MEMORY_DIR = "expert-memory-users"
_SQLITE_HEADER = b"SQLite format 3\0"


def _workspace_path(rel: Path) -> bool:
    return len(rel.parts) >= 2 and rel.parts[1] == "workspace"


def _walk_error(error: OSError) -> None:
    raise error


def _files(root: Path, *, include_workspaces: bool = True) -> Iterator[Path]:
    if root.is_symlink() or not root.is_dir():
        return
    for directory, directories, filenames in os.walk(root, followlinks=False, onerror=_walk_error):
        parent = Path(directory)
        directories[:] = sorted(
            name
            for name in directories
            if not (parent / name).is_symlink()
            and (include_workspaces or not _workspace_path((parent / name).relative_to(root)))
        )
        for name in sorted(filenames):
            path = parent / name
            if path.is_symlink() or not path.is_file():
                continue
            if include_workspaces or not _workspace_path(path.relative_to(root)):
                yield path


def _is_sqlite(path: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    with path.open("rb") as file:
        return file.read(len(_SQLITE_HEADER)) == _SQLITE_HEADER


def add_task_files(
    tf: tarfile.TarFile,
    home: Path,
    staging: Path,
    *,
    include_workspaces: bool,
) -> None:
    """Snapshot SQLite files, including committed WAL data, and pack regular files.

    Call after snapshotting the main registry, whose checkpoint references are
    written after the corresponding checkpoint database commits. A single
    point-in-time snapshot across all files still requires paused task writers.
    """
    for prefix in (TASK_FILES_DIR, EXPERT_MEMORY_DIR):
        root = home / prefix
        for path in _files(root, include_workspaces=include_workspaces):
            if path.name.endswith(("-wal", "-shm", "-journal")) and _is_sqlite(
                path.with_name(path.name.rsplit("-", 1)[0])
            ):
                continue
            archive_path = Path(prefix) / path.relative_to(root)
            source = path
            if _is_sqlite(path):
                source = staging / archive_path
                snapshot_sqlite_file(path, source)
            tf.add(source, arcname=archive_path.as_posix(), recursive=False)


def _remove(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    else:
        shutil.rmtree(path)


def _clear_task_files(root: Path, *, include_workspaces: bool) -> None:
    if root.is_symlink():
        root.unlink()
    elif root.exists():
        if include_workspaces:
            _remove(root)
        else:
            for task in root.iterdir():
                if task.is_symlink() or not task.is_dir():
                    _remove(task)
                    continue
                for child in task.iterdir():
                    if child.name != "workspace":
                        _remove(child)
    root.mkdir(parents=True, exist_ok=True)


def restore_task_files(
    extracted: Path,
    home: Path,
    *,
    include_workspaces: bool,
) -> tuple[int, int, int]:
    """Replace task state and selected default workspaces; task writers must stop.

    Clearing the selected state first also removes stale SQLite WAL/SHM files.
    Omitted default workspaces remain intact, including tasks absent from the DB.
    """
    task_root = home / TASK_FILES_DIR
    _clear_task_files(task_root, include_workspaces=include_workspaces)
    state_files = workspace_files = expert_memory_files = 0
    for source in _files(extracted / TASK_FILES_DIR, include_workspaces=include_workspaces):
        rel = source.relative_to(extracted / TASK_FILES_DIR)
        target = task_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        if _workspace_path(rel):
            workspace_files += 1
        else:
            state_files += 1
    expert_root = home / EXPERT_MEMORY_DIR
    if expert_root.exists() or expert_root.is_symlink():
        _remove(expert_root)
    expert_root.mkdir(parents=True, exist_ok=True)
    for source in _files(extracted / EXPERT_MEMORY_DIR):
        target = expert_root / source.relative_to(extracted / EXPERT_MEMORY_DIR)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        expert_memory_files += 1
    return state_files, workspace_files, expert_memory_files
