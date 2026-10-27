"""Reviewed installation and authentication metadata for built-in ACP runners.

Package names and launch commands come from the linked upstream documentation.
Only runners with an npm installation path have ``npm_packages``. Authentication
probes describe local credential state; they do not verify model entitlement.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RunnerSetupSpec:
    """Fixed installation inputs; never supplied by an HTTP request."""

    runner_id: str
    name: str
    npm_packages: tuple[str, ...]
    binary: str
    default_args: tuple[str, ...]
    login_args: tuple[str, ...] | None
    auth_status_args: tuple[str, ...] | None
    docs_url: str
    node_min_version: tuple[int, int, int] = (22, 19, 0)
    extra_binaries: tuple[str, ...] = ()
    accepted_launches: tuple[tuple[str, tuple[str, ...]], ...] = ()


# Keep the existing OpenCode runner on V1. V2 uses @opencode/cli and exports the
# same binary, so selecting V2 must be an explicit migration.
# Claude and Codex adapters bundle their respective runtimes. Their CLI forwarding
# arguments are present in the pinned tags; no second global CLI is required.
# Cursor documents native installation scripts, which are deliberately not run by
# this catalog. Pi's adapter starts a separate pi process, so install both packages.
RUNNER_SETUP_SPECS: dict[str, RunnerSetupSpec] = {
    "opencode": RunnerSetupSpec(
        runner_id="opencode",
        name="OpenCode",
        npm_packages=("opencode-ai@1.19.4",),
        binary="opencode",
        default_args=("acp",),
        login_args=("auth", "login"),
        auth_status_args=None,
        docs_url="https://opencode.ai/docs/",
        accepted_launches=(("opencode", ("acp",)),),
    ),
    "codebuddy": RunnerSetupSpec(
        runner_id="codebuddy",
        name="CodeBuddy",
        npm_packages=("@tencent-ai/codebuddy-code",),
        binary="codebuddy",
        default_args=("--acp",),
        login_args=(),
        auth_status_args=None,
        docs_url="https://www.codebuddy.ai/docs/cli/quickstart",
        accepted_launches=(("codebuddy", ("--acp",)),),
    ),
    "claude_code": RunnerSetupSpec(
        runner_id="claude_code",
        name="Claude Code",
        npm_packages=("@agentclientprotocol/claude-agent-acp@0.95.0",),
        binary="claude-agent-acp",
        default_args=(),
        login_args=("--cli", "auth", "login"),
        auth_status_args=("--cli", "auth", "status"),
        docs_url="https://github.com/agentclientprotocol/claude-agent-acp",
        accepted_launches=(
            ("npx", ("-y", "@zed-industries/claude-agent-acp")),
            ("npx", ("-y", "@agentclientprotocol/claude-agent-acp")),
            ("claude-agent-acp", ()),
        ),
    ),
    "codex": RunnerSetupSpec(
        runner_id="codex",
        name="Codex",
        npm_packages=("@agentclientprotocol/codex-acp@2.2.2",),
        binary="codex-acp",
        default_args=(),
        login_args=("cli", "login", "--device-auth"),
        auth_status_args=("cli", "login", "status"),
        docs_url="https://github.com/agentclientprotocol/codex-acp",
        accepted_launches=(
            ("npx", ("-y", "@zed-industries/codex-acp")),
            ("npx", ("-y", "@agentclientprotocol/codex-acp")),
            ("codex-acp", ()),
        ),
    ),
    "kimi_code": RunnerSetupSpec(
        runner_id="kimi_code",
        name="Kimi Code",
        npm_packages=("@moonshot-ai/kimi-code",),
        binary="kimi",
        default_args=("acp",),
        login_args=("login",),
        auth_status_args=None,
        docs_url="https://www.kimi.com/code/docs/en/kimi-code-cli/guides/getting-started",
        accepted_launches=(("kimi", ("acp",)),),
    ),
    "cursor_cli": RunnerSetupSpec(
        runner_id="cursor_cli",
        name="Cursor CLI",
        npm_packages=(),
        binary="agent",
        default_args=("acp",),
        login_args=("login",),
        auth_status_args=("status",),
        docs_url="https://cursor.com/docs/cli/installation",
        accepted_launches=(("agent", ("acp",)),),
    ),
    "pi": RunnerSetupSpec(
        runner_id="pi",
        name="Pi",
        npm_packages=("pi-acp@0.0.34", "@earendil-works/pi-coding-agent@1.4.0"),
        binary="pi-acp",
        default_args=(),
        login_args=("--terminal-login",),
        auth_status_args=None,
        docs_url="https://github.com/svkozak/pi-acp",
        extra_binaries=("pi",),
        accepted_launches=(("npx", ("-y", "pi-acp")), ("pi-acp", ())),
    ),
}
