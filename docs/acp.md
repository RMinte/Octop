# ACP (Agent Client Protocol)

Octop integrates [ACP](https://agentclientprotocol.com/) in two directions:

| Direction | ACP server | ACP client | Typical use |
|-----------|------------|------------|-------------|
| **Inbound** | Octop (`octop acp`) | Zed, OpenCode, … | External IDE drives your Octop agent |
| **Outbound** | OpenCode, CodeBuddy, … | Octop (`acp_runner` tool) | Octop agent delegates coding tasks |

Both use **stdio** JSON-RPC today. Octop does not expose an HTTP ACP endpoint.

---

## Dashboard: outbound runners

Open **ACP** in the sidebar (`/acp`).

### Install and authorize on the box

The cards distinguish installation, connection readiness, and enablement. Loading this
page does not download packages or start an agent. Built-in runners are installed by
background jobs on the **box running Octop**; users do not need SSH or host terminal commands.

**Connect Codex** is a single operation: Octop installs missing components, checks for an
existing authorization, displays the browser authorization step when needed, then checks
the connection and enables Codex automatically. Users do not install Codex on their own
computer or separately click sign in, check, and enable. Existing valid authorization is
reused. If ChatGPT has device code sign-in disabled, the user must enable it in ChatGPT's
security settings before retrying; Octop cannot accept provider authorization on their behalf.
Progress can be reopened after closing or refreshing the page. Cancellation, failed checks,
or a concurrent launch configuration change do not automatically enable the runner.

The following individual installation/login/check actions remain available for other runners.

| Status | Meaning | Next action |
|--------|---------|-------------|
| Not installed | The runner or required component is missing | Install on the box |
| Runtime missing | Node.js/npm needs preparation | Install; the job prepares the runtime automatically |
| Not checked | Installed, but the current configuration has not passed a connection check | Authorize/configure a model, then check |
| Login required | An explicit authentication-required response was received | Authorize in the web page, then check again |
| Connection ready | ACP initialization and session creation succeeded | Enable the runner |
| Enabled | Delegation is configured; readiness remains visible separately | Enable `acp_runner` for the expert that should use it |
| Connection error | The ACP session could not be established | Review the error, adjust configuration, and retry |

**Install** runs a background operation with visible stages, cancellation, and retry. All
seven built-in profiles have a managed installation path. The six npm profiles reuse a
supported Node.js/npm or install a fixed official Node.js runtime into Octop's private
ACP directory. Cursor uses its official distribution archive in that same managed tree.
Automatic runtime and Cursor bootstrap support Linux x64/arm64 boxes; unsupported hosts
report an error. Node archives are checked against pinned official SHA256 values. Files
are safely unpacked, and incomplete downloads do not count as installed. System folders
and the service account's `HOME` are not changed. Custom launch profiles are preserved.

An individual **Install** operation leaves the runner disabled until connection checking
and explicit enablement. **Connect Codex** includes the subsequent check and enablement in
the same job. Installation does not accept provider terms or complete OAuth authorization.

**Sign in** starts the runner's fixed login-specific process on the box. The page displays
its approved authorization URL, device code, or interactive authentication response
field. Users complete the provider authorization in their own browser. No general host
shell is opened. Pi and CodeBuddy use a web provider/API-key form because their interactive
entrypoints launch the full agent rather than a login-only command; Cursor also provides
an API-key alternative. Secrets are not echoed in setup responses or retained job output.
Credentials stay in the current Octop user's private runner HOME on the execution box.

Closing the drawer leaves the operation running; reopening or refreshing recovers active
jobs. Explicit cancellation or Octop shutdown terminates the operation and its child
processes. Jobs are in memory: after a service restart, completed installations and CLI
credentials remain, while unfinished operations must be started again.

Connection checks start an installed executable in a temporary empty directory, establish
an ACP session without a prompt, and close the process. They never invoke `npx` to fetch
missing code or send a paid model message. Success verifies the ACP connection, not model
quota or every provider's credentials. The page shows the time of the latest check.
Changing the launch command, arguments, or environment invalidates that check. Enabled
runners with a readiness problem can always be disabled.

Installation and authorization are local host operations. Manage a remote expert's setup
in its peer Octop; its delegation toggle remains available separately.

### What is global vs per-agent

| Setting | Scope | Stored in |
|---------|--------|-----------|
| Runner cards (command, args, enabled, …) | **Per user** — shared by all your agents | `settings` table (`acp_runners:user:{id}`) |
| **Enable acp_runner tool** toggle | **Per agent** | Agent `config_json.acp.tool_enabled` |

Legacy per-agent `config_json.acp.runners` is migrated to the user-global store on first load.

### Built-in runners

| ID | Command | Args |
|----|---------|------|
| `opencode` | `opencode` | `acp` |
| `codebuddy` | `codebuddy` | `--acp` |
| `claude_code` | `npx` | `-y`, `@zed-industries/claude-agent-acp` |
| `codex` | `npx` | `-y`, `@zed-industries/codex-acp` |
| `kimi_code` | `kimi` | `acp` |
| `cursor_cli` | `agent` | `acp` |
| `pi` | `npx` | `-y`, `pi-acp` |

Use **Install** on a card to prepare a built-in runner on the host running Octop.
Existing host installations can also be detected. Custom runners use an existing
executable on `PATH` or an absolute launch command in advanced configuration.
Built-in runners cannot be deleted; custom runners can be added from **Add runner**.

The legacy `npx` entries above are launch presets, not installation checks. Managed
installation uses the current supported adapter packages and a direct local executable.

### Enable delegation in chat

1. Use **Connect Codex** on `/acp` and confirm any browser authorization. For other runners, install, authorize/configure, check, then enable.
2. Switch agent in the top bar and turn on **Enable acp_runner tool** for that agent.
3. In chat, ask the agent to use `acp_runner`, or let it delegate when appropriate.

### `acp_runner` tool workflow

```text
action=list              → enabled runners and session state
action=start             → new session: runner + message (+ optional cwd)
action=message           → continue session
action=respond           → answer [permission_required] with exact option id
action=status            → session open / waiting for permission
action=close             → end session
```

Example user message:

```text
Use acp_runner: action=start, runner=opencode, message=find the README in the workspace and summarize it.
```

Permission prompts from the external agent appear in chat; pick an option or instruct the agent to call `action=respond` with the option id.

After changing global runners, Octop reloads your agents automatically. Changing only `tool_enabled` reloads that agent.

---

## CLI: inbound — expose Octop as an ACP server

Let external clients (e.g. Zed) use an Octop agent as their coding agent:

```bash
octop acp --agent main
```

| Option | Description |
|--------|-------------|
| `--agent ID` | Agent to expose (default: CLI `default_agent` or first agent) |
| `--debug` | Log to stderr |

This starts a **standalone** `OctopServer` (reads `~/.octop`), boots the agent, and speaks ACP on stdin/stdout. It does **not** require `octop run` to be running; it is a separate process.

Pin the default agent for convenience:

```bash
octop user login --username you
# set default_agent in ~/.octop/cli_state.json, or:
octop --agent main acp
```

### Zed example

`~/.config/zed/settings.json`:

```json
{
  "agent_servers": {
    "Octop": {
      "command": "octop",
      "args": ["acp", "--agent", "main"],
      "env": {}
    }
  }
}
```

From a dev checkout:

```json
{
  "agent_servers": {
    "Octop": {
      "command": "uv",
      "args": ["run", "octop", "acp", "--agent", "main"],
      "env": {}
    }
  }
}
```

Create an agent thread in Zed and prompt as usual. Sessions map to `thread_id`; the agent workspace remains under `~/.octop/agents/<agent_id>/`.

---

## HTTP API

Interactive docs: `/api/docs` (tag **agents**).

### Global runners (current user)

| Method | Path | Body |
|--------|------|------|
| `GET` | `/api/acp` | — |
| `PUT` | `/api/acp` | `{ "runners": { "<id>": { … } } }` |
| `GET` | `/api/acp/setup` | passive host installation status and last connection checks |
| `POST` | `/api/acp/codex/connect` | start the complete install → browser authorization if needed → check → enable job (`202`) |
| `POST` | `/api/acp/{runner_name}/install` | start a managed background installation (`202`) |
| `POST` | `/api/acp/{runner_name}/login` | start a login-specific background authorization (`202`) |
| `GET` | `/api/acp/jobs/{job_id}` | owner-only progress and authorization state |
| `POST` | `/api/acp/jobs/{job_id}/cancel` | cancel and clean up the operation |
| `POST` | `/api/acp/jobs/{job_id}/input` | `{ "data": "…" }` to the login-only process |
| `POST` | `/api/acp/{runner_name}/credentials` | provider option ID and API key; response never echoes the key |
| `POST` | `/api/acp/{runner_name}/check` | bounded ACP session check; no prompt |
| `GET` | `/api/acp/{runner_name}` | — |
| `PUT` | `/api/acp/{runner_name}` | runner object |
| `DELETE` | `/api/acp/{runner_name}` | custom runners only |

Setup and job operations require the existing administrator permission and remain scoped
to the current account and execution host. The Codex connect response is an `ACPSetupJob`
with `kind: "connect"`. Poll its owner-only job URL, or recover active jobs from
`GET /api/acp/setup`; the server continues each stage without additional client requests.
`succeeded` means Codex passed the final connection check and is enabled. Connection checks
do not send a model prompt or verify model quota. These HTTP operations can also be used
by the APP: open `authorization.url` in the system browser and display/copy `user_code`;
do not invoke the local computer's Codex or copy its credentials to the box.

### Per-agent tool toggle

| Method | Path | Body |
|--------|------|------|
| `GET` | `/api/agents/{agent_id}/acp` | returns global `runners` + agent `tool_enabled` |
| `PUT` | `/api/agents/{agent_id}/acp` | `{ "tool_enabled": bool, "runners": …? }` — `runners` optional, updates global |
| `PUT` | `/api/agents/{agent_id}/acp/tool` | `{ "tool_enabled": bool }` only |

Runner shape:

```json
{
  "enabled": true,
  "command": "opencode",
  "args": ["acp"],
  "env": {},
  "trusted": true,
  "tool_parse_mode": "update_detail",
  "stdio_buffer_limit_bytes": 52428800
}
```

---

## Prerequisites

- **ACP dependencies** — Octop includes the octop-harness `[acp]` extra and requires `agent-client-protocol>=0.11.0`; Task engines use its public notification queue and dispatcher hooks.
- **Outbound:** prepare the runner with the card's managed installation, or use an existing CLI on the Octop host.
- **Inbound:** agent must start successfully (provider + model configured).

---

## Troubleshooting

| Symptom | Likely cause |
|---------|----------------|
| Runner missing on some agents | Upgrade to user-global runners; open `/acp` once or `GET /api/acp` to trigger migration |
| `acp_runner` lists runner but start fails with “Unknown runner” | Restart `octop run` after adding a custom runner (ACP service cache is process-wide) |
| External runner returns no text | Check CLI auth/quota (e.g. `codebuddy auth status`) |
| `octop acp` fails immediately | Agent not running or invalid `--agent`; create/start agent in dashboard first |
| Command not found | Use absolute path in runner `command` if `PATH` for the server process differs from your shell |
| Kimi Code fails to start | Use managed installation, then authorize Kimi from the runner card on the execution box |
| Cursor CLI fails to start | Use managed installation and the card's web authorization or account-specific API key form |
| Pi runner fails to start | Retry managed installation and inspect its progress; both the Pi runtime and ACP adapter are required |

---

## Related

- [Task execution engines](task-execution-engines.md): expert/engine separation, same-task switching, box-side authorization and recovery boundaries.

- Dashboard: `/acp`
- CLI: `octop acp --help`
- OpenAPI: `/api/docs`
- Harness implementation: `octop-harness` → `octop_harness.acp` (server + `acp_runner` tool)
