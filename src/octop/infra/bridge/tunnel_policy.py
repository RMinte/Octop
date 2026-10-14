"""Path policy for Bridge HTTP tunnel requests against the local ASGI app.

Keep the allowlist aligned with ``dashboard/src/utils/remoteExpert.ts``
``SURFACES``: chat / history / tasks / workspace / memory / mbti / subagents
are fully tunneled; tools / plugins / channels are limited writes (the
Personalization UI still PATCHes tool-settings and may POST reload);
skill packages, global ACP, connector admin, and knowledge-base admin stay
peer-only in the UI even when a matching path would otherwise match.
The separate AgentComputer project/Task protocol has its own narrow route set.
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
    r"(?:/(?:threads|history|uploads|avatar|icon|chat|messages|files|workspace"
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

# AgentComputer exposes a separate, owner-scoped project/Task protocol. Only
# the endpoints needed by the remote project and conversation pages may cross
# a Bridge. Keep write methods and resource shapes explicit: this is not an
# escape hatch into the rest of the peer's control plane.
_AC_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_AGENTCOMPUTER_READ = re.compile(
    rf"^/api/agentcomputer/v1/(?:"
    rf"capabilities|projects|projects/{_AC_UUID}|projects/{_AC_UUID}/documents|"
    rf"projects/{_AC_UUID}/tasks|"
    rf"tasks/{_AC_UUID}|tasks/{_AC_UUID}/turns|"
    rf"runs/{_AC_UUID}|runs/{_AC_UUID}/result|runs/{_AC_UUID}/output|"
    rf"runs/{_AC_UUID}/tool-activity|commands/{_AC_UUID}"
    rf")$"
)
_AGENTCOMPUTER_WRITE = re.compile(
    rf"^/api/agentcomputer/v1/(?:projects|projects/{_AC_UUID}/tasks|tasks/{_AC_UUID}/turns)$"
)
_AGENTCOMPUTER_DOCUMENT = re.compile(
    rf"^/api/agentcomputer/v1/projects/{_AC_UUID}/documents/(?P<filename>[^/]+)$"
)


def _allowed_document_filename(filename: str) -> bool:
    # Mirror the peer's single-file text-document contract. Never let a
    # general workspace path or percent-encoded separator enter the tunnel.
    return (
        1 <= len(filename) <= 128
        and filename == filename.strip()
        and not filename.startswith(".")
        and filename.endswith((".md", ".txt"))
        and all(character.isalnum() or character in "._-" for character in filename)
    )


def is_tunnel_path_allowed(method: str, path: str) -> bool:
    """Return True when ``method`` + ``path`` may run via an inbound tunnel."""
    verb = (method or "GET").upper()
    raw = (path or "").split("?", 1)[0].strip() or "/"
    if not raw.startswith("/"):
        raw = f"/{raw}"
    # Normalize trailing slash except root.
    if len(raw) > 1:
        raw = raw.rstrip("/")

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

    if _AGENTCOMPUTER_READ.fullmatch(raw) and verb == "GET":
        return True

    document = _AGENTCOMPUTER_DOCUMENT.fullmatch(raw)
    if document is not None and verb in {"GET", "PUT"}:
        return _allowed_document_filename(document.group("filename"))

    return bool(_AGENTCOMPUTER_WRITE.fullmatch(raw) and verb == "POST")
