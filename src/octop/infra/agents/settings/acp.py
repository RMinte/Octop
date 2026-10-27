"""User-scoped global ACP runner configuration (settings table)."""

from __future__ import annotations

import json
import logging
from typing import Any

from octop_harness.acp.models import ACPConfig, default_acp_runners

from octop.infra.db.repos.agents import AgentRepo, AgentRow
from octop.infra.db.repos.settings import SettingsRepo

logger = logging.getLogger(__name__)

_SETTINGS_PREFIX = "acp_runners:user:"
_HIDDEN_RUNNERS = frozenset({"qwen_code"})
# Until octop-harness ships these builtins, merge Octop-side defaults.
_OCTOP_BUILTIN_RUNNERS: dict[str, dict[str, Any]] = {
    "kimi_code": {
        "command": "kimi",
        "args": ["acp"],
        "trusted": True,
        "tool_parse_mode": "update_detail",
    },
    "cursor_cli": {
        "command": "agent",
        "args": ["acp"],
        "trusted": True,
        "tool_parse_mode": "update_detail",
    },
    "pi": {
        "command": "npx",
        "args": ["-y", "pi-acp"],
        "trusted": True,
        "tool_parse_mode": "update_detail",
    },
}


def _settings_key(user_id: int) -> str:
    return f"{_SETTINGS_PREFIX}{user_id}"


class ACPSettingsStore:
    """Persist ACP runner definitions once per user; agents only store ``tool_enabled``."""

    def __init__(self, *, settings_repo: SettingsRepo, agents_repo: AgentRepo) -> None:
        self._settings = settings_repo
        self._agents = agents_repo

    def load_runners(self, user_id: int, *, migrate_legacy: bool = True) -> dict[str, Any]:
        """Return merged runner dict (built-ins + saved overrides)."""
        raw = self._settings.get(_settings_key(user_id))
        if raw:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("invalid acp_runners JSON for user %s; using defaults", user_id)
                data = {}
            if isinstance(data, dict):
                runners_raw = data.get("runners")
                if isinstance(runners_raw, dict):
                    return _runners_response(runners_raw)
                return _runners_response(data)

        migrated = self._migrate_legacy_from_agents(user_id) if migrate_legacy else None
        if migrated is not None:
            self._save_raw_runners(user_id, migrated)
            return _runners_response(migrated)

        return _runners_response({})

    def save_runners(self, user_id: int, runners: dict[str, Any]) -> dict[str, Any]:
        filtered = {k: v for k, v in runners.items() if k not in _HIDDEN_RUNNERS}
        self._save_raw_runners(user_id, filtered)
        return self.load_runners(user_id)

    def load_setup_status(self, user_id: int, runner: str) -> dict[str, Any]:
        """Read a connection observation separately from runner configuration."""
        raw = self._settings.get(f"acp_setup:user:{user_id}:{runner}")
        return _parse_config_json(raw)

    def save_setup_status(self, user_id: int, runner: str, status: dict[str, Any]) -> None:
        """Persist only the observation, never CLI output or credentials."""
        self._settings.set(
            f"acp_setup:user:{user_id}:{runner}", json.dumps(status, ensure_ascii=False)
        )

    def update_runner_if_matches(
        self,
        user_id: int,
        runner: str,
        expected: dict[str, Any],
        changes: dict[str, Any],
        *,
        expected_status: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Merge one runner atomically, preserving concurrently changed peers."""
        key = _settings_key(user_id)
        for _ in range(8):
            raw = self._settings.get(key)
            document = _parse_config_json(raw)
            stored = document.get("runners", document)
            runners = _runners_response(stored if isinstance(stored, dict) else {})
            if runners.get(runner) != expected:
                return None
            updated = {**expected, **changes}
            runners[runner] = updated
            checks: dict[str, str | None] = {}
            if expected_status is not None:
                status_key = f"acp_setup:user:{user_id}:{runner}"
                status_raw = self._settings.get(status_key)
                if _parse_config_json(status_raw) != expected_status:
                    return None
                checks[status_key] = status_raw
            payload = json.dumps({"runners": runners}, ensure_ascii=False)
            if self._settings.compare_and_set(key, raw, payload, checks=checks):
                return dict(_runners_response(runners)[runner])
        return None

    def _save_raw_runners(self, user_id: int, runners: dict[str, Any]) -> None:
        self._settings.set(
            _settings_key(user_id), json.dumps({"runners": runners}, ensure_ascii=False)
        )

    def _migrate_legacy_from_agents(self, user_id: int) -> dict[str, Any] | None:
        best: dict[str, Any] | None = None
        best_score = -1
        touched: list[AgentRow] = []
        for row in self._agents.list_by_user(user_id):
            cfg = _parse_config_json(row.config_json)
            acp = cfg.get("acp")
            if not isinstance(acp, dict):
                continue
            legacy = acp.get("runners")
            if not isinstance(legacy, dict) or not legacy:
                continue
            touched.append(row)
            score = sum(1 for v in legacy.values() if isinstance(v, dict) and v.get("enabled"))
            if score > best_score or (score == best_score and len(legacy) > len(best or {})):
                best = legacy
                best_score = score
        if best is None:
            return None
        logger.info("migrated legacy per-agent ACP runners to global settings for user %s", user_id)
        self._strip_legacy_runners(touched)
        return best

    def _strip_legacy_runners(self, rows: list[AgentRow]) -> None:
        for row in rows:
            cfg = _parse_config_json(row.config_json)
            acp = cfg.get("acp")
            if not isinstance(acp, dict) or "runners" not in acp:
                continue
            slim = {k: v for k, v in acp.items() if k != "runners"}
            if slim:
                cfg["acp"] = slim
            else:
                cfg.pop("acp", None)
            self._agents.update_config(
                row.agent_id, config_json=json.dumps(cfg, ensure_ascii=False)
            )


def _runners_response(raw_runners: dict[str, Any]) -> dict[str, Any]:
    merged_raw = dict(raw_runners)
    harness_names = set(default_acp_runners())
    for name, spec in _OCTOP_BUILTIN_RUNNERS.items():
        if name not in harness_names and name not in merged_raw:
            merged_raw[name] = spec
    merged = ACPConfig.from_dict({"runners": merged_raw})
    return {
        name: runner.to_dict()
        for name, runner in merged.runners.items()
        if name not in _HIDDEN_RUNNERS
    }


def _parse_config_json(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}
