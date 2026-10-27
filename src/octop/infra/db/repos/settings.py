"""Settings KV store — lightweight key/value pairs backed by SQLite."""

from __future__ import annotations

from octop.infra.db.pool import DatabasePool


class SettingsRepo:
    _KEY_ACTIVE_MODEL = "active_model"

    def __init__(self, db: DatabasePool):
        self._db = db

    def get(self, key: str) -> str | None:
        with self._db.connect() as conn:
            r = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return r["value"] if r else None

    def set(self, key: str, value: str) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def delete(self, key: str) -> None:
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM settings WHERE key = ?", (key,))

    def compare_and_set(
        self,
        key: str,
        expected: str | None,
        value: str,
        *,
        checks: dict[str, str | None] | None = None,
    ) -> bool:
        """Replace one value only while it and optional related values still match."""
        with self._db.transaction() as conn:
            conditions = {**(checks or {}), key: expected}
            suffix = " FOR UPDATE" if self._db.dialect == "postgresql" else ""
            for checked_key in sorted(conditions):
                row = conn.execute(
                    "SELECT value FROM settings WHERE key=?" + suffix, (checked_key,)
                ).fetchone()
                if (str(row["value"]) if row else None) != conditions[checked_key]:
                    return False
            if expected is None:
                return bool(
                    conn.execute(
                        "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO NOTHING",
                        (key, value),
                    ).rowcount
                )
            return bool(
                conn.execute(
                    "UPDATE settings SET value=? WHERE key=? AND value=?", (value, key, expected)
                ).rowcount
            )

    def get_active_model(self) -> tuple[str, str]:
        """Return (provider_name, model_id); empty strings if not set."""
        raw = self.get(self._KEY_ACTIVE_MODEL) or ""
        name, _, model = raw.partition("/")
        return name, model

    def set_active_model(self, provider_name: str, model_id: str) -> None:
        self.set(self._KEY_ACTIVE_MODEL, f"{provider_name}/{model_id}")
