"""Path policy for Bridge HTTP tunnel requests against the local ASGI app.

Keep the allowlist aligned with ``dashboard/src/utils/remoteExpert.ts``
``SURFACES``: chat / history / tasks / workspace / memory / mbti / subagents
are fully tunneled; tools / plugins / channels are limited writes (the
Personalization UI still PATCHes tool-settings and may POST reload);
skill packages, global ACP, connector admin, and knowledge-base admin stay
peer-only in the UI even when a matching path would otherwise match.
Unified projects and tasks use a separate explicit operation allowlist.
Management / auth / bridge control planes stay local-only.
"""

from __future__ import annotations

import re

# Product surfaces run as the connection owner. Management / auth / bridge
# control planes stay local-only.
_AGENT_COLLECTION = re.compile(r"^/api/agents/?$")
_AGENT_RESOURCE = re.compile(
    r"^/api/agents/"
    r"(?:bridge:[^/]+|[^/]+)"
    r"(?:/(?:threads|history|uploads|upload|avatar|icon|chat|messages|files|workspace"
    r"|attachments|media|turns|memory|skills|tools|tool-settings|mbti|persona"
    r"|channels|cron|config|state|status|welcome|members|subagents|reload|acp"
    r")(?:/.*)?)?$"
)

# Composer read-only surfaces for remote chat (models + knowledge pickers).
# Write / document / admin provider routes stay denied.
_COMPOSER_READONLY = re.compile(
    r"^/api/(?:"
    r"providers/resolved|"
    r"providers/active-model|"
    r"knowledge-bases|"
    r"knowledge-bases/capability"
    r")$"
)

# Chat dock browser viewer (peer harness). Install / record-replay stay denied.
_BROWSER_VIEWER_GET = re.compile(r"^/api/browser/(?:env-status|harness-sessions)$")
_BROWSER_HANDOFF = re.compile(r"^/api/browser/sessions/[^/]+/handoff$")

# Experts surfaces that are not under /api/agents/{id} but still agent-scoped
# on the peer (header or /plugins/agents/{id} path).
_PLUGIN_AGENT = re.compile(r"^/api/plugins/agents/(?:bridge:[^/]+|[^/]+)(?:/tools)?$")
_MBTI = re.compile(r"^/api/mbti(?:/.*)?$")
_SUBAGENT_CATALOG = re.compile(r"^/api/subagent-catalog(?:/.*)?$")
_ACP_GLOBAL = re.compile(r"^/api/acp(?:/[^/]+)?$")
_CRON_SETTINGS = re.compile(r"^/api/cron/settings$")
_CONNECTOR_INSTANCES_LIST = re.compile(r"^/api/connector-instances$")

_RESOURCE_ID = r"[A-Za-z0-9_-]+"


def is_task_tunnel_path_allowed(method: str, path: str) -> bool:
    """Only unified product operations, never arbitrary management or URLs."""
    verb = method.upper()
    if "?" in path or "%" in path or ".." in path or "\\" in path:
        return False
    if path in {"/api/projects", "/api/project-tasks"}:
        return verb in {"GET", "POST"}
    if path == "/api/project-tasks/approval-policy":
        return verb in {"GET", "PUT"}
    if path in {
        "/api/projects/resources",
        "/api/projects/sync",
        "/api/project-tasks/resources",
        "/api/project-tasks/settings",
        "/api/project-tasks/capabilities",
        "/api/project-tasks/sync",
        "/api/project-tasks/artifacts",
        "/api/settings/timezone",
    }:
        return verb == "GET"
    if path == "/api/project-tasks/polish":
        return verb == "POST"
    if path in {"/api/knowledge-bases", "/api/knowledge-bases/capability"} or re.fullmatch(
        rf"/api/knowledge-bases/{_RESOURCE_ID}/documents(?:/{_RESOURCE_ID}(?:/(?:preview|text|file|content))?)?",
        path,
    ):
        return verb == "GET"
    if path == "/api/project-tasks/attachments":
        return verb == "POST"
    if re.fullmatch(rf"/api/project-tasks/attachments/{_RESOURCE_ID}", path):
        return verb == "GET"
    if re.fullmatch(
        rf"/api/project-tasks/artifacts/{_RESOURCE_ID}",
        path,
    ):
        return verb == "GET"
    if re.fullmatch(rf"/api/projects/{_RESOURCE_ID}", path):
        return verb in {"GET", "PATCH", "PUT", "DELETE"}
    if re.fullmatch(rf"/api/projects/{_RESOURCE_ID}/archive", path):
        return verb == "PATCH"
    if re.fullmatch(rf"/api/projects/{_RESOURCE_ID}/tasks", path):
        return verb in {"GET", "POST"}
    if re.fullmatch(rf"/api/projects/{_RESOURCE_ID}/tasks/sync", path):
        return verb == "GET"
    if re.fullmatch(rf"/api/projects/{_RESOURCE_ID}/workspace/tree-children", path):
        return verb == "GET"
    if re.fullmatch(rf"/api/project-tasks/{_RESOURCE_ID}", path):
        return verb in {"GET", "PATCH", "DELETE"}
    tail = path.removeprefix("/api/project-tasks/")
    if tail == path or "/" not in tail:
        return False
    task_id, operation = tail.split("/", 1)
    if not re.fullmatch(_RESOURCE_ID, task_id):
        return False
    if operation in {
        "export",
        "context-usage",
        "events",
        "workbench",
        "workspace/tree-children",
        "workspace/media",
        "workspace/download-file",
        "browser/sessions",
        "browser/record-replay/status",
        "browser/record-replay/skill-content",
    }:
        return verb == "GET"
    if operation in {"memory", "expert-memory"}:
        return (
            verb in {"GET", "POST", "DELETE"} if operation == "memory" else verb in {"GET", "POST"}
        )
    if operation == "approval-policy":
        return verb == "PUT"
    if operation == "expert-memory/policy":
        return verb in {"GET", "PATCH"}
    if operation in {
        "messages",
        "forks",
        "cancel",
        "pause",
        "resume",
        "workspace/mkdir",
        "workspace/move",
        "workspace/upload",
        "browser/handoff",
        "browser/record-replay/start",
        "browser/record-replay/stop-and-generate-skill",
    }:
        return verb == "POST"
    if operation == "workspace/file":
        return verb in {"GET", "PUT", "DELETE"}
    if re.fullmatch(
        rf"turns/{_RESOURCE_ID}/retry|turns/{_RESOURCE_ID}/interactions/{_RESOURCE_ID}/respond|recordings/{_RESOURCE_ID}/apply|tool-ui/{_RESOURCE_ID}/{_RESOURCE_ID}/request",
        operation,
    ):
        return verb == "POST"
    if re.fullmatch(rf"artifacts/{_RESOURCE_ID}/download", operation):
        return verb == "GET"
    if re.fullmatch(rf"terminal/sessions/{_RESOURCE_ID}", operation):
        return verb == "DELETE"
    return False


def is_tunnel_path_allowed(method: str, path: str) -> bool:
    """Return True when ``method`` + ``path`` may run via an inbound tunnel."""
    verb = (method or "GET").upper()
    raw = (path or "").split("?", 1)[0].strip() or "/"
    if not raw.startswith("/"):
        raw = f"/{raw}"
    # Normalize trailing slash except root.
    if len(raw) > 1:
        raw = raw.rstrip("/")

    if is_task_tunnel_path_allowed(verb, raw):
        return True

    if _AGENT_COLLECTION.fullmatch(raw):
        return verb == "GET"

    if _COMPOSER_READONLY.fullmatch(raw):
        return verb == "GET"

    if _BROWSER_VIEWER_GET.fullmatch(raw):
        return verb == "GET"

    if _BROWSER_HANDOFF.fullmatch(raw):
        return verb == "POST"

    if _AGENT_RESOURCE.fullmatch(raw):
        return verb in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}

    if _PLUGIN_AGENT.fullmatch(raw):
        return verb in {"GET", "PATCH"}

    if _MBTI.fullmatch(raw):
        return verb in {"GET", "POST"}

    if _SUBAGENT_CATALOG.fullmatch(raw):
        return verb == "GET"

    if _ACP_GLOBAL.fullmatch(raw):
        return verb in {"GET", "PUT", "DELETE"}

    if _CRON_SETTINGS.fullmatch(raw):
        return verb == "GET"

    if _CONNECTOR_INSTANCES_LIST.fullmatch(raw):
        return verb == "GET"

    return False
