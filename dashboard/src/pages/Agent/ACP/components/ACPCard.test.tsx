import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type {
  ACPRunnerConfig,
  ACPRunnerSetup,
} from "../../../../api/types/acp";
import { ACPCard } from "./ACPCard";

const config: ACPRunnerConfig = {
  command: "npx",
  args: ["custom-runner"],
  env: {},
  enabled: false,
  trusted: true,
  tool_parse_mode: "update_detail",
  stdio_buffer_limit_bytes: 50 * 1024 * 1024,
};
const setup: ACPRunnerSetup = {
  runner: "custom",
  installation: "manual",
  connection: "unchecked",
  enabled: false,
  can_install: false,
  can_login: false,
  credential_options: [],
  install_command: null,
  login_command: null,
  docs_url: null,
  message: null,
  checked_at: null,
};

function mount(
  overrides: {
    runnerKey?: string;
    config?: Partial<ACPRunnerConfig>;
    setup?: Partial<ACPRunnerSetup>;
    interactionDisabled?: boolean;
  } = {},
) {
  const events = {
    onClick: vi.fn(),
    onMouseEnter: vi.fn(),
    onMouseLeave: vi.fn(),
    onToggleEnabled: vi.fn(),
    onInstall: vi.fn(),
    onConnect: vi.fn(),
    onCheck: vi.fn(),
    onGuide: vi.fn(),
  };
  render(
    <ACPCard
      runnerKey={overrides.runnerKey ?? "custom"}
      config={{ ...config, ...overrides.config }}
      setup={{ ...setup, ...overrides.setup }}
      timeZone="Asia/Shanghai"
      interactionDisabled={overrides.interactionDisabled}
      isHover={false}
      {...events}
    />,
  );
  return events;
}

describe("ACPCard setup actions", () => {
  it("replaces manual sign-in instructions for idle Codex", () => {
    mount({
      runnerKey: "codex",
      setup: {
        connection: "auth_required",
        message: "请在此网页完成登录授权或配置 API Key，然后重新检测连接",
      },
    });
    expect(screen.getByText("acp.codexConnectHint")).toBeInTheDocument();
    expect(screen.queryByText(/然后重新检测连接/)).not.toBeInTheDocument();
  });

  it("retains the real Codex connection error", () => {
    mount({
      runnerKey: "codex",
      setup: {
        connection: "error",
        message: "Provider connection was refused",
      },
    });
    expect(
      screen.getByText("Provider connection was refused"),
    ).toBeInTheDocument();
  });

  it.each([
    {
      installation: "not_installed",
      connection: "unchecked",
      can_install: true,
    },
    { installation: "installed", connection: "auth_required", can_login: true },
    { installation: "installed", connection: "ready" },
  ] as const)("offers only Connect Codex at setup state %j", async (state) => {
    const events = mount({ runnerKey: "codex", setup: state });
    expect(screen.getByRole("switch")).toBeDisabled();
    expect(screen.getAllByRole("button")).toHaveLength(2);
    expect(
      screen.getByRole("button", { name: "acp.advancedConfig" }),
    ).toBeEnabled();
    await userEvent.click(
      screen.getByRole("button", { name: "acp.connectCodex" }),
    );
    expect(events.onConnect).toHaveBeenCalledWith("codex");
    expect(events.onInstall).not.toHaveBeenCalled();
    expect(events.onCheck).not.toHaveBeenCalled();
    expect(events.onToggleEnabled).not.toHaveBeenCalled();
  });

  it("keeps the Codex disconnect switch and a single reconnect entry", async () => {
    const events = mount({
      runnerKey: "codex",
      config: { enabled: true },
      setup: { installation: "installed", connection: "ready", enabled: true },
    });
    await userEvent.click(
      screen.getByRole("button", { name: "acp.reconnectCodex" }),
    );
    expect(events.onConnect).toHaveBeenCalledWith("codex");
    await userEvent.click(screen.getByRole("switch"));
    expect(events.onToggleEnabled).toHaveBeenCalledWith("codex", false);
  });

  it("offers manual setup for custom launchers without claiming they are installed", async () => {
    const events = mount();
    expect(screen.getByText("acp.installation_manual")).toBeInTheDocument();
    expect(
      screen.queryByText("acp.installation_installed"),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("switch")).toBeDisabled();
    await userEvent.click(
      screen.getByRole("button", { name: "acp.advancedConfig" }),
    );
    expect(events.onClick).toHaveBeenCalled();
    expect(events.onInstall).not.toHaveBeenCalled();
    expect(events.onToggleEnabled).not.toHaveBeenCalled();
  });

  it("requires an explicit enable action after the ACP connection is ready", async () => {
    const events = mount({
      setup: { installation: "installed", connection: "ready" },
    });
    expect(screen.getByRole("switch")).not.toBeChecked();
    expect(events.onToggleEnabled).not.toHaveBeenCalled();
    await userEvent.click(
      screen.getByRole("button", { name: "acp.enableRunner" }),
    );
    expect(events.onToggleEnabled).toHaveBeenCalledWith("custom", true);
    expect(events.onInstall).not.toHaveBeenCalled();
  });

  it("allows disabling a previously enabled runner even in a sandbox and awaiting login", async () => {
    const events = mount({
      config: { enabled: true },
      setup: {
        installation: "installed",
        connection: "auth_required",
        can_login: true,
      },
      interactionDisabled: true,
    });
    expect(screen.getByRole("switch")).toBeEnabled();
    expect(screen.getByRole("button", { name: "acp.login" })).toBeDisabled();
    await userEvent.click(screen.getByRole("switch"));
    expect(events.onToggleEnabled).toHaveBeenCalledWith("custom", false);
  });
});
