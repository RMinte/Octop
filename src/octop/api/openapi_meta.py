"""OpenAPI metadata for Scalar API docs."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

from octop.api.deps import is_jwt_exempt_path

API_DESCRIPTION = """\
**Octop** is a smarter, self-hosted AI assistant for multiple users and agents. All routes are served under `/api`.

## Authentication

Most endpoints require a JWT bearer token:

1. Complete initial setup via `/api/setup/*` (first install only).
2. `GET /api/auth/captcha` then `POST /api/auth/login` with `username` and `password`
   (and `captcha_token` when a strong provider is active), or complete SSO with
   `/api/auth/oidc/*` or `/api/auth/oauth/*` when a provider is enabled. When a
   directory is configured (`/api/auth/ldap/config`), `POST /api/auth/login` also
   accepts LDAP credentials and provisions the account on first login.
3. Send `Authorization: Bearer <access_token>` on subsequent requests.

Access tokens use sliding renewal: when less than one-third of
`access_token_ttl_seconds` remains, authenticated responses may include a fresh
token in the `X-Octop-Access-Token` header. Clients should replace the stored
token when present.

Public endpoints (no token): `/api/docs`, `/api/openapi.json`, `/api/health`,
`/api/setup/*`, `/api/auth/login`, `/api/auth/captcha`, `/api/auth/oidc/status`, `/api/auth/oidc/start`,
`/api/auth/oidc/callback`, `/api/auth/oidc/exchange`, `/api/auth/oauth/status`,
`/api/auth/oauth/start`, `/api/auth/oauth/callback`, `/api/auth/oauth/exchange`,
`/api/auth/ldap/status`,
`/api/auth/invite/validate`,
`/api/auth/invite/redeem`, `/api/connectors/oauth/callback`,
and `/api/internal/mcp/*`.

## Agent scope

Many agent-scoped routes use the agent ID in the URL path (`/api/agents/{agent_id}/…`).
The caller must own the agent unless they are an admin.

## Streaming chat

Unified project/task conversations use `/api/project-tasks` and **WebSocket** at
`/api/project-tasks/ws` with first-frame authentication. Tasks may select an owned
expert or use the general assistant; projects determine the working directory.
Configuration changes apply to future turns, while accepted turns retain snapshots.

Legacy and advanced dashboard turns use **WebSocket** at `/api/agents/{agent_id}/chat/ws` (pass JWT as
`?token=` query param). Send `{"type":"user_turn", ...}` frames; the server replies with JSON
chunks matching the harness stream format, ending with `{"type":"done"}` or
`{"type":"error","message":"..."}`.

Text-type dashboard pushes (cron reminders, proactive care) also emit
`{"type":"dashboard_push", ...}` on `/api/notifications/ws` (same `?token=` auth) so the
SPA can show a toast even when the chat socket is not subscribed.

HITL resume still uses `POST /api/agents/{agent_id}/chat/hitl/resume` (SSE).
"""

OPENAPI_TAGS: list[dict[str, str]] = [
    {
        "name": "projects",
        "description": "独立项目和任务：管理项目自己的目录、Skill、MCP、插件、子代理角色、长期记忆、团队协作与行动指南；发送首条消息创建任务，在同一任务内继续多轮对话。支持持久工具上下文、任务独立记忆、增量回复和停止。独立保存并实时传送模型返回的思考内容，本轮可选择模型支持的思考模式和强度。专家可选，支持本人团队和共享普通专家；团队使用任务内角色或固定专家成员同步协作，成员身份分别保存。高级工作台文件、浏览器、终端、录制技能及工具UI均使用Task范围；专家记忆支持独立范围、持久自动抽取与有来源约束的通用偏好晋升。任务实时通道为 `/api/project-tasks/ws`，支持多任务共用连接；浏览器首帧 authenticate 鉴权，命令包括 task.create、task.open、task.message、task.subscribe、task.cancel、task.retry。task.open 表示进入任务详情，按需补生成标题；普通订阅和断线恢复不触发标题重试。支持按轮声明客户端工具，通过 client.attach、tool.claim、tool.result 完成客户端执行和模型续跑。create_artifact、publish_workspace_file 由服务端提供；每轮 artifacts 返回已发布附件元数据，内容通过鉴权 HTTP 下载。成果资料库为 `/api/project-tasks/artifacts`，跨任务按文件发布时间倒序分页，支持文件名、项目、任务、详细格式、资料分类与归档筛选。资料库筛选为全部、文本和PDF、幻灯片、表格、图片、其他；business_category 按最终发布文件名后缀自动保存，取值 document/presentation/spreadsheet/image/other，默认查询 all 包含所有成果。历史成果按相同规则回填；file_kind 保留原详细格式。WebSocket 帧协议见仓库 docs/project-task-websocket.md；REST 保留配置、列表、历史和快照。",
    },
    {
        "name": "setup",
        "description": "First-run wizard: create the admin user and optional LLM provider.",
    },
    {"name": "auth", "description": "Sign in, sign out, and manage the current user profile."},
    {"name": "health", "description": "Liveness probe for load balancers and monitoring."},
    {"name": "users", "description": "Admin user management (create, disable, reset passwords)."},
    {
        "name": "agents",
        "description": "Create and configure AI agents; start, stop, and inspect runtime status.",
    },
    {
        "name": "chat",
        "description": "Dashboard chat: WebSocket streaming, threads, conversation history, trajectory ledger, and text-push toasts.",
    },
    {
        "name": "slash",
        "description": "Slash command metadata for composer menus and inline help.",
    },
    {
        "name": "connectors",
        "description": "Third-party integrations (Notion, Figma, …) exposed as MCP servers.",
    },
    {
        "name": "bridge",
        "description": "Link this Octop to remote Octop instances (HTTP tunnel + remote chat).",
    },
    {
        "name": "knowledge",
        "description": "Private, shareable document knowledge bases and their indexing capability.",
    },
    {
        "name": "internal-mcp",
        "description": "Internal MCP gateway used by harness agents (no dashboard auth).",
    },
    {
        "name": "channels",
        "description": "Instant-messaging bridges (WeCom, Feishu, Telegram, …) per agent.",
    },
    {
        "name": "cron",
        "description": "Scheduled prompts that run against an agent on a cron trigger.",
    },
    {
        "name": "settings",
        "description": "Process-level settings (timezone, upload size limit, HITL switch, login captcha).",
    },
    {
        "name": "envs",
        "description": "Global environment variables (~/.octop/env) inherited by every agent.",
    },
    {
        "name": "search",
        "description": "Web-search provider API key probes (Tavily, Brave, Google, Kimi).",
    },
    {"name": "providers", "description": "LLM provider configuration and active model selection."},
    {"name": "voice", "description": "Speech-to-text and text-to-speech provider configuration."},
    {
        "name": "observability",
        "description": "LLM observability integrations (Langfuse tracing).",
    },
    {
        "name": "tls",
        "description": "Let's Encrypt HTTPS certificate issuance and status.",
    },
    {
        "name": "security",
        "description": "Agent security policy: tool approval (HITL), command guard rules, filesystem rules, PII redaction.",
    },
    {"name": "admin", "description": "Server-wide admin operations (audit log, storage, usage)."},
    {"name": "storage-backends", "description": "User-visible remote storage backend connections."},
    {
        "name": "filesystem",
        "description": "Host filesystem directory browsing for dashboard forms.",
    },
    {"name": "mbti", "description": "MBTI persona presets applied to agent personality."},
    {"name": "experts", "description": "Bundled expert templates for creating specialized agents."},
    {
        "name": "teams",
        "description": "Expert teams: roster, create/edit, and member dispatch.",
    },
    {"name": "workspace", "description": "Agent workspace file tree: list, read, write, upload."},
    {"name": "agent_files", "description": "Agent-owned configuration files (SOUL.md, skills, …)."},
    {"name": "usage", "description": "Token usage summaries for billing and dashboards."},
    {
        "name": "skill-packages",
        "description": "Instance-global skill package catalog and package skill content.",
    },
    {"name": "skills", "description": "Per-agent skills and Skill Hub marketplace search."},
    {
        "name": "subagents",
        "description": "Per-agent subagent definitions (agents/*.md) and bundled catalog install.",
    },
    {"name": "terminal", "description": "AI-assisted remote terminal sessions."},
    {"name": "update", "description": "In-place server update checks and apply flow."},
    {
        "name": "browser",
        "description": "Remote browser: env/install, harness sessions, live stream, record/replay.",
    },
    {
        "name": "mobile",
        "description": "Remote Phone stream and adb control (mobile permission).",
    },
    {
        "name": "desktop",
        "description": "Remote OS desktop stream and input injection (admin only).",
    },
    {"name": "ollama", "description": "Local Ollama model discovery and download management."},
    {
        "name": "onnx",
        "description": "Local ONNX / fastembed embedding service: enable, catalog, and download.",
    },
]

_BEARER_SCHEME = {
    "type": "http",
    "scheme": "bearer",
    "bearerFormat": "JWT",
    "description": "JWT from `POST /api/auth/login` (`access_token` field).",
}


def configure_openapi(app: FastAPI) -> None:
    """Attach tag descriptions, API intro, and Bearer security to the OpenAPI schema."""

    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema

        schema = get_openapi(
            title=app.title,
            version=app.version,
            description=API_DESCRIPTION,
            routes=app.routes,
            tags=OPENAPI_TAGS,
        )
        components = schema.setdefault("components", {})
        components.setdefault("securitySchemes", {})["BearerAuth"] = _BEARER_SCHEME

        for path, path_item in schema.get("paths", {}).items():
            if not path.startswith("/api/") or is_jwt_exempt_path(path):
                continue
            for operation in path_item.values():
                if isinstance(operation, dict) and "security" not in operation:
                    operation["security"] = [{"BearerAuth": []}]

        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi  # type: ignore[method-assign]
