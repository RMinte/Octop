import { App } from "antd";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {
  afterAll,
  beforeAll,
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from "vitest";
import type {
  ACPRunnerConfig,
  ACPRunnerSetup,
  ACPSetupJob,
} from "../../../api/types/acp";

const { t, agentContext, api } = vi.hoisted(() => ({
  t: (key: string) => key,
  agentContext: {
    activeAgentId: "local",
    agents: [{ agent_id: "local", bridge: false, config: {} }],
  },
  api: {
    getGlobalRunners: vi.fn(),
    updateGlobalRunners: vi.fn(),
    getConfig: vi.fn(),
    updateToolEnabled: vi.fn(),
    getSetup: vi.fn(),
    installRunner: vi.fn(),
    loginRunner: vi.fn(),
    checkRunner: vi.fn(),
    getJob: vi.fn(),
    cancelJob: vi.fn(),
    inputJob: vi.fn(),
    saveCredentials: vi.fn(),
  },
}));
vi.mock("react-i18next", () => ({ useTranslation: () => ({ t }) }));
vi.mock("../../../context/AgentContext", () => ({
  useAgent: () => agentContext,
}));
vi.mock("../../../hooks/useServerTimezone", () => ({
  useServerTimezone: () => "Asia/Shanghai",
}));
vi.mock("../../../api/modules/acp", () => ({ acpApi: api }));
vi.mock("../../Experts/components/agentBackendForm", () => ({
  blocksAcpOutboundFromConfig: () => false,
}));
vi.mock("../../../layouts/PageShell", () => ({
  default: ({ children }: { children: React.ReactNode }) => children,
}));
import { ACPPanel } from "./index";

const config: ACPRunnerConfig = {
  command: "opencode",
  args: ["acp"],
  env: {},
  enabled: false,
  trusted: true,
  tool_parse_mode: "update_detail",
  stdio_buffer_limit_bytes: 50 * 1024 * 1024,
};
const initialSetup: ACPRunnerSetup = {
  runner: "opencode",
  installation: "not_installed",
  connection: "unchecked",
  enabled: false,
  can_install: true,
  can_login: true,
  credential_options: [],
  install_command: "private-installer",
  login_command: "opencode auth login",
  docs_url: "https://opencode.ai/docs/",
  message: null,
  checked_at: null,
};
const initialJob: ACPSetupJob = {
  job_id: "install-1",
  runner: "opencode",
  kind: "install",
  status: "running",
  phase: "downloading_runtime",
  output: "Downloading runtime…",
  message: null,
  authorization: null,
  input_mode: "none",
  setup: null,
};
let runners: Record<string, ACPRunnerConfig>;
let setup: ACPRunnerSetup;
let activeJobs: Record<string, ACPSetupJob>;
let currentJob: ACPSetupJob;

beforeAll(() => {
  const getComputedStyle = window.getComputedStyle.bind(window);
  vi.spyOn(window, "getComputedStyle").mockImplementation((element) =>
    getComputedStyle(element),
  );
});
afterAll(() => vi.restoreAllMocks());
beforeEach(() => {
  vi.clearAllMocks();
  agentContext.activeAgentId = "local";
  agentContext.agents = [{ agent_id: "local", bridge: false, config: {} }];
  runners = { opencode: { ...config } };
  setup = { ...initialSetup };
  activeJobs = {};
  currentJob = { ...initialJob };
  api.getGlobalRunners.mockImplementation(async () => ({ runners }));
  api.getSetup.mockImplementation(async () => ({
    host: "service@octop-box",
    runners: { opencode: setup },
    active_jobs: activeJobs,
  }));
  api.getConfig.mockResolvedValue({ tool_enabled: false, runners: {} });
  api.updateGlobalRunners.mockImplementation(async (next) => ({
    runners: (runners = next),
  }));
  api.installRunner.mockImplementation(async () => {
    runners = { opencode: { ...config, command: "/managed/bin/opencode" } };
    setup = { ...initialSetup, installation: "installed", can_install: false };
    return (currentJob = {
      ...initialJob,
      status: "succeeded",
      phase: "completed",
      setup,
    });
  });
  api.loginRunner.mockImplementation(async () => {
    currentJob = {
      ...initialJob,
      job_id: "login-1",
      kind: "login",
      phase: "waiting_for_user",
      status: "waiting_for_user",
      authorization: {
        url: "https://opencode.ai/authorize",
        user_code: "ABCD-1234",
        expires_at: 1792972800000,
      },
    };
    activeJobs = { opencode: currentJob };
    return currentJob;
  });
  api.getJob.mockImplementation(async () => currentJob);
  api.cancelJob.mockImplementation(async () => {
    activeJobs = {};
    return (currentJob = {
      ...currentJob,
      status: "cancelled",
      phase: "cancelled",
    });
  });
  api.inputJob.mockImplementation(async () => {
    activeJobs = {};
    return (currentJob = {
      ...currentJob,
      status: "succeeded",
      phase: "completed",
      setup,
    });
  });
  api.saveCredentials.mockImplementation(
    async () => (setup = { ...setup, connection: "unchecked" }),
  );
  api.checkRunner.mockImplementation(
    async () =>
      (setup = {
        ...setup,
        installation: "installed",
        connection: "ready",
        checked_at: 1792972800000,
      }),
  );
});
function mount(agentId?: string) {
  return render(
    <App>
      <ACPPanel {...(agentId === undefined ? {} : { agentId })} />
    </App>,
  );
}
async function getCard() {
  return within(
    await screen.findByRole("group", { name: "acp.runner_opencode" }),
  );
}
function getSetupPanel() {
  return within(screen.getByRole("dialog"));
}

describe("ACP box setup flow", () => {
  it("installs on explicit Install, then requires a check and explicit enable", async () => {
    mount();
    const card = await getCard();
    expect(card.getByRole("switch")).toBeDisabled();
    await userEvent.click(card.getByRole("button", { name: "acp.install" }));
    expect(api.installRunner).toHaveBeenCalledWith("opencode");
    expect(api.updateGlobalRunners).not.toHaveBeenCalled();
    await waitFor(() =>
      expect(card.getByText("acp.installation_installed")).toBeInTheDocument(),
    );
    const panel = getSetupPanel();
    expect(
      panel.getByRole("button", { name: "acp.startLogin" }),
    ).toBeInTheDocument();
    expect(panel.queryByText("opencode auth login")).not.toBeInTheDocument();
    expect(panel.queryByText("private-installer")).not.toBeInTheDocument();
    expect(card.getByRole("switch")).toBeDisabled();
    await userEvent.click(
      panel.getByRole("button", { name: "acp.checkConnection" }),
    );
    await waitFor(() => expect(card.getByRole("switch")).toBeEnabled());
    expect(api.checkRunner).toHaveBeenCalledWith("opencode");
    expect(api.updateGlobalRunners).not.toHaveBeenCalled();
    await userEvent.click(
      card.getByRole("button", { name: "acp.enableRunner" }),
    );
    await waitFor(() =>
      expect(api.updateGlobalRunners).toHaveBeenCalledWith(
        {
          opencode: expect.objectContaining({
            enabled: true,
            command: "/managed/bin/opencode",
          }),
        },
        undefined,
      ),
    );
  });

  it("automatically prepares a missing runtime instead of asking the user to install Node", async () => {
    setup = { ...setup, installation: "missing_dependency", can_install: true };
    api.installRunner.mockImplementation(async () => {
      activeJobs = { opencode: currentJob };
      return currentJob;
    });
    mount();
    const card = await getCard();
    await userEvent.click(card.getByRole("button", { name: "acp.install" }));
    expect(
      await screen.findByText("acp.phase_downloading_runtime"),
    ).toBeInTheDocument();
    expect(getSetupPanel().getByRole("log")).toHaveTextContent(
      "Downloading runtime…",
    );
    expect(api.installRunner).toHaveBeenCalledTimes(1);
    expect(card.getByRole("switch")).toBeDisabled();
    await userEvent.click(
      getSetupPanel().getByRole("button", { name: "Close" }),
    );
    expect(api.cancelJob).not.toHaveBeenCalled();
    await userEvent.click(
      card.getByRole("button", { name: "acp.openInstallProgress" }),
    );
    await userEvent.click(
      getSetupPanel().getByRole("button", { name: "acp.cancelJob" }),
    );
    await waitFor(() =>
      expect(api.cancelJob).toHaveBeenCalledWith("install-1"),
    );
    expect(
      await screen.findByText("acp.job_install_cancelled"),
    ).toBeInTheDocument();
    expect(api.updateGlobalRunners).not.toHaveBeenCalled();
  });

  it("restores an active box job after refresh and polls it without restarting installation", async () => {
    activeJobs = { opencode: currentJob };
    api.getJob.mockImplementation(async () => {
      activeJobs = {};
      setup = { ...setup, installation: "installed" };
      runners.opencode.command = "/managed/bin/opencode";
      return { ...currentJob, status: "succeeded", phase: "completed", setup };
    });
    mount();
    const card = await getCard();
    await userEvent.click(
      card.getByRole("button", { name: "acp.openInstallProgress" }),
    );
    expect(getSetupPanel().getByRole("log")).toHaveTextContent(
      "Downloading runtime…",
    );
    await waitFor(
      () =>
        expect(
          card.getByText("acp.installation_installed"),
        ).toBeInTheDocument(),
      { timeout: 2500 },
    );
    expect(api.getJob).toHaveBeenCalledWith("install-1");
    expect(api.installRunner).not.toHaveBeenCalled();
    expect(card.getByRole("switch")).toBeDisabled();
  });

  it("recovers a lost job after a box restart and allows installation to restart", async () => {
    activeJobs = { opencode: currentJob };
    api.getJob.mockImplementation(async () => {
      activeJobs = {};
      throw new Error("Request failed: 404 Not Found");
    });
    mount();
    const card = await getCard();
    await userEvent.click(
      card.getByRole("button", { name: "acp.openInstallProgress" }),
    );
    await waitFor(
      () =>
        expect(
          getSetupPanel().getByText("acp.jobInterrupted"),
        ).toBeInTheDocument(),
      { timeout: 2500 },
    );
    await waitFor(() =>
      expect(card.getByRole("button", { name: "acp.install" })).toBeEnabled(),
    );
    expect(api.getSetup).toHaveBeenCalledTimes(2);
    await userEvent.click(
      getSetupPanel().getByRole("button", { name: "Close" }),
    );
    await userEvent.click(card.getByRole("button", { name: "acp.install" }));
    await waitFor(() => expect(api.installRunner).toHaveBeenCalledTimes(1));
    expect(api.updateGlobalRunners).not.toHaveBeenCalled();
  });

  it("starts the login program on the box and shows its official URL and device code", async () => {
    setup = {
      ...setup,
      installation: "installed",
      connection: "auth_required",
    };
    mount();
    const card = await getCard();
    await userEvent.click(card.getByRole("button", { name: "acp.login" }));
    await userEvent.click(
      getSetupPanel().getByRole("button", { name: "acp.startLogin" }),
    );
    expect(api.loginRunner).toHaveBeenCalledWith("opencode");
    const panel = getSetupPanel();
    expect(await panel.findByText("ABCD-1234")).toBeInTheDocument();
    expect(
      panel.getByRole("link", { name: "acp.openAuthorization" }),
    ).toHaveAttribute("href", "https://opencode.ai/authorize");
    expect(panel.queryByLabelText("acp.loginResponse")).not.toBeInTheDocument();
    expect(api.updateGlobalRunners).not.toHaveBeenCalled();
    expect(api.checkRunner).not.toHaveBeenCalled();
    expect(card.getByRole("switch")).toBeDisabled();
  });

  it("sends login responses only to the dedicated login job and does not open a shell", async () => {
    setup = {
      ...setup,
      installation: "installed",
      connection: "auth_required",
    };
    api.loginRunner.mockImplementation(
      async () =>
        (currentJob = {
          ...initialJob,
          job_id: "login-1",
          kind: "login",
          input_mode: "terminal",
          phase: "waiting_for_user",
          status: "waiting_for_user",
          output: "Enter verification code",
        }),
    );
    mount();
    const card = await getCard();
    await userEvent.click(card.getByRole("button", { name: "acp.login" }));
    await userEvent.click(
      getSetupPanel().getByRole("button", { name: "acp.startLogin" }),
    );
    const panel = getSetupPanel();
    await userEvent.type(
      await panel.findByLabelText("acp.loginResponse"),
      "one-time-code",
    );
    await userEvent.click(
      panel.getByRole("button", { name: "acp.submitLoginResponse" }),
    );
    await waitFor(() =>
      expect(api.inputJob).toHaveBeenCalledWith("login-1", "one-time-code\r"),
    );
    expect(
      await panel.findByText("acp.job_login_succeeded"),
    ).toBeInTheDocument();
    expect(api.updateGlobalRunners).not.toHaveBeenCalled();
    expect(api.checkRunner).not.toHaveBeenCalled();
  });

  it("saves a dynamically offered provider API key, checks the connection and clears the key", async () => {
    setup = {
      ...setup,
      installation: "installed",
      connection: "auth_required",
      can_login: false,
      credential_options: [{ id: "codebuddy_cn", label: "CodeBuddy China" }],
    };
    api.checkRunner.mockImplementation(
      async () =>
        (setup = {
          ...setup,
          connection: "error",
          message: "Provider did not connect",
        }),
    );
    mount();
    const card = await getCard();
    await userEvent.click(card.getByRole("button", { name: "acp.login" }));
    const panel = getSetupPanel();
    expect(
      panel.queryByRole("button", { name: "acp.startLogin" }),
    ).not.toBeInTheDocument();
    expect(panel.getByText("CodeBuddy China")).toBeInTheDocument();
    const key = panel.getByLabelText("API Key");
    await userEvent.type(key, "fake-api-key");
    await userEvent.click(
      panel.getByRole("button", { name: "acp.saveCredentialsAndCheck" }),
    );
    await waitFor(() =>
      expect(api.saveCredentials).toHaveBeenCalledWith(
        "opencode",
        "codebuddy_cn",
        "fake-api-key",
      ),
    );
    await waitFor(() =>
      expect(api.checkRunner).toHaveBeenCalledWith("opencode"),
    );
    await waitFor(() => expect(key).toHaveValue(""));
    expect(api.updateGlobalRunners).not.toHaveBeenCalled();
    expect(card.getByRole("switch")).toBeDisabled();
  });

  it("does not mistake a launch command or npx for installation or readiness", async () => {
    runners.opencode.command = "npx";
    mount();
    const card = await getCard();
    expect(
      card.getByText("acp.installation_not_installed"),
    ).toBeInTheDocument();
    expect(card.getByRole("switch")).toBeDisabled();
    expect(api.installRunner).not.toHaveBeenCalled();
    expect(api.checkRunner).not.toHaveBeenCalled();
  });

  it("allows turning off an enabled runner even when its connection fails", async () => {
    runners.opencode.enabled = true;
    setup = {
      ...setup,
      installation: "installed",
      enabled: true,
      connection: "error",
    };
    mount();
    const card = await getCard();
    expect(card.getByRole("switch")).toBeEnabled();
    await userEvent.click(card.getByRole("switch"));
    await waitFor(() =>
      expect(api.updateGlobalRunners).toHaveBeenCalledWith(
        { opencode: expect.objectContaining({ enabled: false }) },
        undefined,
      ),
    );
  });

  it("keeps unchecked distinct from sign-in required", async () => {
    setup = { ...setup, installation: "installed" };
    api.checkRunner.mockResolvedValue({
      ...setup,
      connection: "auth_required",
    });
    mount();
    const card = await getCard();
    expect(card.getByText("acp.unchecked")).toBeInTheDocument();
    expect(card.queryByText("acp.authRequired")).not.toBeInTheDocument();
    await userEvent.click(
      card.getByRole("button", { name: "acp.checkConnection" }),
    );
    expect(await screen.findByText("acp.boxSetupHint")).toBeInTheDocument();
    expect(card.getByText("acp.authRequired")).toBeInTheDocument();
    expect(card.getByRole("switch")).toBeDisabled();
  });

  it("shows a failed start request without enabling", async () => {
    let rejectInstall: (error: Error) => void = () => {};
    api.installRunner.mockImplementation(
      () =>
        new Promise((_, reject) => {
          rejectInstall = reject;
        }),
    );
    mount();
    const card = await getCard();
    await userEvent.click(card.getByRole("button", { name: "acp.install" }));
    expect(card.getByRole("button", { name: /acp.installing/ })).toBeDisabled();
    await act(async () => {
      rejectInstall(new Error("Installation could not start"));
    });
    expect(
      (await screen.findAllByText("Installation could not start")).length,
    ).toBeGreaterThan(0);
    expect(card.getByRole("button", { name: "acp.install" })).toBeEnabled();
    expect(card.getByRole("switch")).toBeDisabled();
  });

  it("treats a failed job returned with HTTP success as a failure", async () => {
    api.installRunner.mockResolvedValue({
      ...initialJob,
      status: "failed",
      phase: "failed",
      message: "Installer exited unsuccessfully",
    });
    mount();
    const card = await getCard();
    await userEvent.click(card.getByRole("button", { name: "acp.install" }));
    expect(
      (await screen.findAllByText("Installer exited unsuccessfully")).length,
    ).toBeGreaterThan(0);
    expect(screen.queryByText("acp.installSuccess")).not.toBeInTheDocument();
    expect(card.getByRole("switch")).toBeDisabled();
  });

  it("disables a runner when advanced launch settings change", async () => {
    runners.opencode.enabled = true;
    setup = {
      ...setup,
      installation: "installed",
      connection: "ready",
      enabled: true,
    };
    api.updateGlobalRunners.mockImplementation(async (next) => {
      setup = { ...setup, connection: "unchecked", enabled: false };
      return { runners: (runners = next) };
    });
    mount();
    const card = await getCard();
    await userEvent.click(
      card.getByRole("button", { name: "acp.advancedConfig" }),
    );
    const command = await screen.findByLabelText("acp.command");
    await userEvent.clear(command);
    await userEvent.type(command, "other-opencode");
    await userEvent.click(screen.getByRole("button", { name: "common.save" }));
    await waitFor(() =>
      expect(api.updateGlobalRunners).toHaveBeenCalledWith(
        {
          opencode: expect.objectContaining({
            command: "other-opencode",
            enabled: false,
          }),
        },
        undefined,
      ),
    );
    expect(card.getByRole("switch")).toBeDisabled();
    expect(api.installRunner).not.toHaveBeenCalled();
  });

  it("never inspects or installs local programs from a remote expert drawer", async () => {
    mount("bridge:peer:expert");
    expect(await screen.findByText("acp.remoteHostHint")).toBeInTheDocument();
    await waitFor(() =>
      expect(api.getConfig).toHaveBeenCalledWith("bridge:peer:expert"),
    );
    expect(api.getGlobalRunners).not.toHaveBeenCalled();
    expect(api.getSetup).not.toHaveBeenCalled();
    expect(api.installRunner).not.toHaveBeenCalled();
    expect(api.loginRunner).not.toHaveBeenCalled();
    expect(api.getJob).not.toHaveBeenCalled();
    expect(screen.queryByRole("group")).not.toBeInTheDocument();
  });

  it("keeps the standalone page on its local host when a remote expert is selected", async () => {
    agentContext.activeAgentId = "bridge:peer:expert";
    agentContext.agents = [
      { agent_id: "bridge:peer:expert", bridge: true, config: {} },
    ];
    mount();
    await getCard();
    expect(api.getGlobalRunners).toHaveBeenCalledWith(undefined);
    expect(api.getSetup).toHaveBeenCalledWith();
    expect(screen.queryByText("acp.remoteHostHint")).not.toBeInTheDocument();
  });
});
