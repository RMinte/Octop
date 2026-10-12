"""OS ownership persists while the preparing process is alive, not by a TTL."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from octop.infra.agentcomputer.commands import StorageDataError
from octop.infra.agentcomputer.preparation_lock import preparation_lock


def test_second_process_cannot_steal_live_lock(tmp_path: Path) -> None:
    path = tmp_path / "control" / "project.lock"
    with preparation_lock(path) as acquired:
        assert acquired

        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; import sys; from octop.infra.agentcomputer.preparation_lock import preparation_lock\nwith preparation_lock(Path(sys.argv[1])) as owned: print(owned)",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        assert result.stdout.strip() == "False"
    with preparation_lock(path) as acquired:
        assert acquired


def test_process_exit_releases_lock_without_a_lease(tmp_path: Path) -> None:
    path = tmp_path / "control" / "project.lock"
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import os,sys; from pathlib import Path; from octop.infra.agentcomputer.preparation_lock import preparation_lock\nwith preparation_lock(Path(sys.argv[1])) as owned:\n assert owned\n os._exit(0)",
            str(path),
        ],
        check=True,
        timeout=15,
    )
    with preparation_lock(path) as acquired:
        assert acquired


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink and permission semantics")
def test_lock_refuses_symlink_without_touching_target(tmp_path: Path) -> None:
    target = tmp_path / "unrelated"
    target.write_text("preserve")
    lock = tmp_path / "project.lock"
    lock.symlink_to(target)
    with pytest.raises(StorageDataError), preparation_lock(lock):
        pass
    assert target.read_text() == "preserve"
