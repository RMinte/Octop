"""An existing v56 database receives new access and Bridge columns exactly once."""

from pathlib import Path

import pytest

from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.skill_packages import SkillPackageRepo


@pytest.mark.parametrize("partial", [False, True])
def test_existing56_upgrade_preserves_packages_and_adds_bridge_identity(
    tmp_path: Path, partial: bool
):
    pool = SqlitePool(tmp_path / "upgrade.sqlite")
    try:
        run_migrations(pool)
        repo = SkillPackageRepo(pool)
        repo.create(id="keep-package", name="Keep", created_by="1", copy_policy="lock")
        with pool.transaction() as conn:
            conn.execute("ALTER TABLE skill_packages DROP COLUMN shared")
            if not partial:
                conn.execute("ALTER TABLE skill_packages DROP COLUMN allow_download")
            conn.execute("ALTER TABLE bridge_connections DROP COLUMN peer_connection_id")
            conn.execute("UPDATE _schema_version SET version=56")
        run_migrations(pool)
        run_migrations(pool)
        row = repo.get("keep-package")
        assert row is not None and row.name == "Keep" and row.copy_policy == "lock"
        assert row.allow_download is True and row.shared is True
        repo.update("keep-package", shared=False)
        run_migrations(pool)
        assert repo.get("keep-package").shared is False
        repo.create(id="new-private", name="New", created_by="1")
        assert repo.get("new-private").shared is False
        with pool.connect() as conn:
            assert conn.execute("SELECT version FROM _schema_version").fetchone()[0] == 60
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(bridge_connections)")}
            assert "peer_connection_id" in columns
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        pool.close()


def test_sqlite_and_postgresql_access_ddl_have_equivalent_defaults():
    root = Path(__file__).resolve().parents[3] / "src/octop/infra/db/migrations"
    sqlite = (root / "057_skill_package_access.sql").read_text()
    postgres = (root / "057_skill_package_access.pg.sql").read_text()
    assert sqlite == postgres
    assert "allow_download INTEGER NOT NULL DEFAULT 1" in sqlite
    assert "shared INTEGER NOT NULL DEFAULT 0" in sqlite
