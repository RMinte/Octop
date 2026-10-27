import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import zh from "../../locales/zh.json";
import {
  projectTasksApi,
  type Project,
  type TaskDetail,
  type TaskInputAttachment,
  type TaskStatus,
} from "../../api/modules/projects";
import TaskConversations from "./TaskConversations";

vi.mock("../../api/modules/projects", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../api/modules/projects")>()),
  projectTasksApi: {
    settings: vi.fn(),
    contextUsage: vi.fn(),
    list: vi.fn(),
    sync: vi.fn(),
    get: vi.fn(),
    open: vi.fn(),
    create: vi.fn(),
    send: vi.fn(),
    cancel: vi.fn(),
    control: vi.fn(),
    respond: vi.fn(),
    setApproval: vi.fn(),
    approvalPolicy: vi.fn(),
    updateApprovalPolicy: vi.fn(),
    approvalEvents: vi.fn(),
    events: vi.fn(),
    retry: vi.fn(),
    fork: vi.fn(),
    resources: vi.fn(),
    inspectEngine: vi.fn(),
    update: vi.fn(),
    delete: vi.fn(),
    downloadArtifact: vi.fn(),
    uploadAttachment: vi.fn(),
    attachmentPreview: vi.fn(),
  },
}));
vi.mock("../../hooks/useServerTimezone", () => ({
  useServerTimezone: () => "Asia/Shanghai",
}));
vi.mock("../../components/Markdown/LazyMarkdown", () => ({
  default: ({ content }: { content: string }) => <div>{content}</div>,
}));
vi.mock("react-i18next", () => {
  const t = (key: string) =>
    key
      .split(".")
      .reduce<unknown>((v, k) => (v as Record<string, unknown>)?.[k], zh) ??
    key;
  return { useTranslation: () => ({ t }) };
});
const api = vi.mocked(projectTasksApi, true);

async function conversationTool(name: string) {
  if (!screen.queryByRole("button", { name, exact: true }))
    await userEvent.click(
      await screen.findByRole("button", {
        name: zh.projects.workspace.conversationTools,
      }),
    );
  return screen.findByRole("button", { name, exact: true });
}

async function approvalSelector() {
  if (
    !screen.queryByRole("combobox", {
      name: zh.projects.tasks.approvalMode.label,
    })
  )
    await userEvent.click(
      await screen.findByRole("button", {
        name: zh.projects.composer.accountApproval,
      }),
    );
  return screen.findByRole("combobox", {
    name: zh.projects.tasks.approvalMode.label,
  });
}

async function turnSettings(group?: string) {
  if (
    !screen.queryByRole("dialog", { name: zh.projects.tasks.executionOptions })
  )
    await userEvent.click(
      screen.getByRole("button", { name: zh.projects.tasks.executionOptions }),
    );
  const panel = await screen.findByRole("dialog", {
    name: zh.projects.tasks.executionOptions,
  });
  if (group) {
    const heading = within(panel).getByRole("button", {
      name: new RegExp(group + "$"),
    });
    if (heading.getAttribute("aria-expanded") === "false")
      await userEvent.click(heading);
  }
  return panel;
}

afterEach(() => vi.useRealTimers());
const fixture: TaskDetail = {
  task_id: "task_a",
  project_id: "proj_a",
  title: "第一项任务",
  archived: false,
  status: "completed",
  active_turn_id: null,
  list_revision: 1,
  web_search_enabled: false,
  approval_policy: {
    mode: "ask_for_approval",
    revision: 1,
    updated_at: "2026-10-15T00:00:00Z",
  },
  created_at: "2026-10-15T00:00:00Z",
  updated_at: "2026-10-15T00:00:00Z",
  next_page_cursor: null,
  event_cursor: "event_first",
  turns: [
    {
      sequence: 1,
      turn_id: "turn_a",
      request_id: "request-first",
      prompt: "记住蓝鸟",
      attachments: [],
      reply: "已记住蓝鸟",
      reasoning: "",
      content_blocks: [],
      content_revision: 0,
      plan: null,
      plan_revision: 0,
      status: "completed",
      error_code: "",
      activity: "",
      artifacts: [],
      model_ref: "glm/glm",
      options: {},
      web_search_enabled: false,
      retry_of_turn_id: null,
      cancel_requested: false,
      pause_requested: false,
      interactions: [],
      interaction_revision: 0,
      project_revision: 1,
      approval_policy_at_start: {
        mode: "ask_for_approval",
        revision: 1,
        updated_at: "2026-10-15T00:00:00Z",
      },
      created_at: "2026-10-15T00:00:00Z",
      updated_at: "2026-10-15T00:00:00Z",
    },
  ],
};
const projectFixture: Project = {
  project_id: "proj_a",
  name: "项目A",
  description: "",
  directory: "/server/before",
  client_directory: "/client/before",
  instructions: "",
  skills: [],
  mcp_servers: [],
  plugins: [],
  subagents: [],
  memory_enabled: true,
  team_enabled: false,
  archived: false,
  revision: 1,
  created_at: fixture.created_at,
  updated_at: fixture.updated_at,
};
beforeEach(() => {
  vi.clearAllMocks();
  api.approvalPolicy.mockResolvedValue(fixture.approval_policy);
  api.updateApprovalPolicy.mockImplementation(async (body) => ({
    ...fixture.approval_policy,
    mode: body.mode,
    revision: body.expected_revision + 1,
  }));
  api.approvalEvents.mockImplementation((_receive, signal, onOpen) => {
    onOpen?.();
    return new Promise<void>((resolve) =>
      signal.addEventListener("abort", () => resolve(), { once: true }),
    );
  });
  api.settings.mockResolvedValue({
    action_timeline_enabled: true,
    step_panel_enabled: true,
  });
  api.contextUsage.mockResolvedValue({
    max_tokens: 128000,
    used_tokens: 0,
    input_tokens: 0,
    output_tokens: 0,
    segments: [],
    source: "empty",
    estimated: false,
  });
  api.list.mockResolvedValue({
    items: [fixture],
    next_page_cursor: null,
    sync_cursor: "sync_first",
  });
  api.sync.mockResolvedValue({
    base_sync_cursor: "sync_first",
    next_sync_cursor: "sync_next",
    next_page_cursor: null,
    changes: [],
  });
  api.events.mockImplementation((_id, _cursor, _onEvent, signal, onOpen) => {
    onOpen?.();
    return new Promise<void>((resolve) =>
      signal.addEventListener("abort", () => resolve(), { once: true }),
    );
  });
  api.resources.mockResolvedValue({
    experts: [
      {
        expert_id: "expert_a",
        name: "Finance expert",
        description: "",
        config_revision: 3,
      },
    ],
    skills: [],
    mcp_servers: [],
    plugins: [],
    subagents: [],
    directory_roots: [],
    models: [{ model_ref: "glm/glm", name: "GLM", supports_images: false }],
    approval: {
      allowed_modes: ["ask_for_approval", "approve_for_me", "full_access"],
      reviewer_ready: true,
      execution_profile: "host_process",
    },
    default_model_ref: "glm/glm",
  });
  api.update.mockResolvedValue(fixture);
  api.setApproval.mockResolvedValue(fixture);
  api.delete.mockResolvedValue(undefined);
  api.retry.mockResolvedValue(fixture);
  api.open.mockResolvedValue(fixture);
  api.get.mockResolvedValue(fixture);
  api.create.mockResolvedValue(fixture);
  api.send.mockResolvedValue(fixture);
  api.cancel.mockResolvedValue({ ...fixture, active_turn_id: null });
  api.downloadArtifact.mockResolvedValue(new Blob(["report"]));
  api.uploadAttachment.mockResolvedValue(imageAttachment);
  api.attachmentPreview.mockResolvedValue(
    new Blob(["image"], { type: "image/png" }),
  );
  let imageURL = 0;
  URL.createObjectURL = vi.fn(() => `blob:task-image-${++imageURL}`);
  URL.revokeObjectURL = vi.fn();
});

it("creates one record with an optional expert and freezes the execution selection", async () => {
  const user = userEvent.setup();
  mount("", null);
  const input = screen.getByRole("textbox", {
    name: zh.projects.tasks.message,
  });
  await user.type(input, "Review the report");
  await user.click(
    await screen.findByRole("combobox", { name: zh.projects.tasks.expert }),
  );
  await user.click(
    await screen.findByText("Finance expert", {
      selector: ".ant-select-item-option-content",
    }),
  );
  expect(input).toHaveValue("Review the report");
  await user.click(screen.getByText(zh.projects.tasks.actionModes.ask));
  await user.click(
    screen.getByRole("button", { name: zh.projects.tasks.send }),
  );
  await waitFor(() =>
    expect(api.create).toHaveBeenCalledWith(
      null,
      expect.objectContaining({
        prompt: "Review the report",
        selected_expert_id: "expert_a",
        action_mode: "ask",
        expected_project_revision: null,
      }),
    ),
  );
});

it("changes the expert only between turns, retaining the draft and both revisions", async () => {
  const selected = { ...fixture, configuration_revision: 5 };
  api.open.mockResolvedValue(selected);
  api.update.mockResolvedValue({
    ...selected,
    list_revision: 2,
    configuration_revision: 6,
    selected_expert_id: "expert_a",
  });
  const user = userEvent.setup();
  mount("?task=task_a");
  const input = await screen.findByRole("textbox", {
    name: zh.projects.tasks.message,
  });
  await waitFor(() => expect(input).toBeEnabled());
  await user.type(input, "Keep this draft");
  await user.click(
    screen.getByRole("combobox", { name: zh.projects.tasks.expert }),
  );
  await user.click(
    await screen.findByText("Finance expert", {
      selector: ".ant-select-item-option-content",
    }),
  );
  await waitFor(() =>
    expect(api.update).toHaveBeenCalledWith("task_a", {
      selected_expert_id: "expert_a",
      expected_revision: 1,
      expected_configuration_revision: 5,
    }),
  );
  expect(input).toHaveValue("Keep this draft");
  await user.click(
    screen.getByRole("button", { name: zh.projects.tasks.send }),
  );
  await waitFor(() =>
    expect(api.send).toHaveBeenCalledWith(
      "task_a",
      expect.objectContaining({ expected_configuration_revision: 6 }),
    ),
  );
});

it("explains an unavailable expert without silently changing identity and lets the user choose none", async () => {
  const selected = {
    ...fixture,
    configuration_revision: 5,
    selected_expert_id: "unavailable_expert",
  };
  api.open.mockResolvedValue(selected);
  api.update.mockResolvedValue({
    ...selected,
    list_revision: 2,
    configuration_revision: 6,
    selected_expert_id: null,
  });
  const user = userEvent.setup();
  mount("?task=task_a");
  await screen.findByText(zh.projects.tasks.expertUnavailableHint);
  expect(api.update).not.toHaveBeenCalled();
  const input = screen.getByRole("textbox", {
    name: zh.projects.tasks.message,
  });
  await waitFor(() => expect(input).toBeEnabled());
  await user.type(input, "Keep this draft");
  await user.click(
    screen.getByRole("combobox", { name: zh.projects.tasks.expert }),
  );
  await user.click(
    await screen.findByText(zh.projects.tasks.noExpert, {
      selector: ".ant-select-item-option-content",
    }),
  );
  await waitFor(() =>
    expect(api.update).toHaveBeenCalledWith("task_a", {
      selected_expert_id: null,
      expected_revision: 1,
      expected_configuration_revision: 5,
    }),
  );
  expect(input).toHaveValue("Keep this draft");
  await waitFor(() =>
    expect(
      screen.queryByText(zh.projects.tasks.expertUnavailableHint),
    ).not.toBeInTheDocument(),
  );
});

it("keeps the accepted expert fixed while a turn waits for approval", async () => {
  api.open.mockResolvedValue({
    ...fixture,
    selected_expert_id: "expert_a",
    status: "waiting_approval",
    active_turn_id: "turn_a",
  });
  mount("?task=task_a");
  await waitFor(() =>
    expect(
      screen.getByRole("combobox", { name: zh.projects.tasks.expert }),
    ).toBeDisabled(),
  );
  expect(api.update).not.toHaveBeenCalled();
});

it("keeps the narrow-screen navigation reachable from a project overview and closes it after changing projects", async () => {
  const user = userEvent.setup();
  const workspace = (projectID: string) => (
    <MemoryRouter>
      <TaskConversations
        projectID={projectID}
        selectedTaskID={null}
        workspace
        overview={<div>Project overview</div>}
        navigation={<nav aria-label="Project navigation">Projects</nav>}
      />
    </MemoryRouter>
  );
  const view = render(workspace("project_a"));
  const toggle = screen.getByRole("button", {
    name: zh.projects.workspace.navigation,
  });
  expect(toggle).toBeVisible();
  await user.click(toggle);
  expect(
    screen.getByRole("region", { name: zh.projects.tasks.title }),
  ).toHaveAttribute("data-list-open", "true");
  view.rerender(workspace("project_b"));
  await waitFor(() =>
    expect(
      screen.getByRole("region", { name: zh.projects.tasks.title }),
    ).toHaveAttribute("data-list-open", "false"),
  );
  expect(screen.getByText("Project overview")).toBeVisible();
});

it("uses the owner-wide scope rather than treating no project as all projects", async () => {
  render(
    <MemoryRouter>
      <TaskConversations projectID={null} allTasks />
    </MemoryRouter>,
  );
  await waitFor(() =>
    expect(api.list).toHaveBeenCalledWith(null, undefined, false, {
      scope: "all",
    }),
  );
});

it("edits into a new branch and leaves the source message and task untouched", async () => {
  api.fork.mockResolvedValue({ ...fixture, task_id: "branch_a" });
  const navigate = vi.fn();
  const user = userEvent.setup();
  render(
    <MemoryRouter>
      <TaskConversations
        projectID="proj_a"
        selectedTaskID="task_a"
        onSelectTask={navigate}
      />
    </MemoryRouter>,
  );
  await user.click(
    await screen.findByRole("button", { name: zh.projects.tasks.editBranch }),
  );
  const input = screen.getByRole("textbox", {
    name: zh.projects.tasks.branchPrompt,
  });
  await user.clear(input);
  await user.type(input, "Revised request");
  await user.click(
    screen.getByRole("button", {
      name: new RegExp(zh.common.confirm.split("").join("\\s*")),
    }),
  );
  await waitFor(() =>
    expect(api.fork).toHaveBeenCalledWith(
      "task_a",
      expect.objectContaining({
        operation: "edit",
        turn_id: "turn_a",
        prompt: "Revised request",
        project_id: "proj_a",
        selected_expert_id: null,
      }),
    ),
  );
  expect(navigate).toHaveBeenCalledWith("branch_a");
  expect(api.send).not.toHaveBeenCalled();
  expect(fixture.turns[0].prompt).not.toBe("Revised request");
});

it.each([
  ["fork", zh.projects.tasks.fork],
  ["edit", zh.projects.tasks.editBranch],
  ["regenerate", zh.projects.tasks.regenerate],
] as const)(
  "creates a %s branch from inherited history",
  async (operation, label) => {
    api.open.mockResolvedValue({
      ...fixture,
      turns: [
        {
          ...fixture.turns[0],
          history_only: true,
          source_task_id: "source_task",
          source_turn_id: "source_turn",
        },
      ],
    });
    api.fork.mockResolvedValue({ ...fixture, task_id: "branch_a" });
    const navigate = vi.fn();
    const user = userEvent.setup();
    render(
      <MemoryRouter>
        <TaskConversations
          projectID="proj_a"
          selectedTaskID="task_a"
          onSelectTask={navigate}
        />
      </MemoryRouter>,
    );
    await user.click(await screen.findByRole("button", { name: label }));
    if (operation !== "fork") {
      const input = screen.getByRole("textbox", {
        name: zh.projects.tasks.branchPrompt,
      });
      expect(input).toHaveValue(fixture.turns[0].prompt);
      if (operation === "edit") {
        await user.clear(input);
        await user.type(input, "Revised inherited request");
      } else expect(input).toHaveAttribute("readonly");
    }
    await user.click(
      screen.getByRole("button", {
        name: new RegExp(zh.common.confirm.split("").join("\\s*")),
      }),
    );
    await waitFor(() =>
      expect(api.fork).toHaveBeenCalledWith("task_a", {
        operation,
        turn_id: "turn_a",
        project_id: "proj_a",
        selected_expert_id: null,
        request_id: expect.any(String),
        ...(operation === "edit"
          ? { prompt: "Revised inherited request" }
          : {}),
      }),
    );
    expect(navigate).toHaveBeenCalledWith("branch_a");
    expect(api.send).not.toHaveBeenCalled();
    expect(api.retry).not.toHaveBeenCalled();
  },
);

it.each([false, true])(
  "offers the latest inherited answer in conversation tools (has local history: %s)",
  async (hasLocalHistory) => {
    api.open.mockResolvedValue({
      ...fixture,
      turns: [
        ...(hasLocalHistory ? [fixture.turns[0]] : []),
        {
          ...fixture.turns[0],
          sequence: 2,
          turn_id: "inherited_turn",
          history_only: true,
          source_task_id: "source_task",
          source_turn_id: "source_turn",
        },
      ],
    });
    api.fork.mockResolvedValue({ ...fixture, task_id: "branch_a" });
    const user = userEvent.setup();
    mount("?task=task_a");
    await user.click(
      await screen.findByRole("button", {
        name: zh.projects.workspace.conversationTools,
      }),
    );
    const tools = await screen.findByRole("tooltip");
    await user.click(
      within(tools).getByRole("button", { name: zh.projects.tasks.fork }),
    );
    await user.click(
      screen.getByRole("button", {
        name: new RegExp(zh.common.confirm.split("").join("\\s*")),
      }),
    );
    await waitFor(() =>
      expect(api.fork).toHaveBeenCalledWith(
        "task_a",
        expect.objectContaining({
          operation: "fork",
          turn_id: "inherited_turn",
        }),
      ),
    );
  },
);

it.each(["failed", "cancelled"] as const)(
  "marks %s inherited history without offering execution retry",
  async (status) => {
    api.open.mockResolvedValue({
      ...fixture,
      status,
      turns: [
        {
          ...fixture.turns[0],
          status,
          history_only: true,
          source_task_id: "source_task",
          source_turn_id: "source_turn",
        },
      ],
    });
    const navigate = vi.fn();
    const user = userEvent.setup();
    render(
      <MemoryRouter>
        <TaskConversations
          projectID="proj_a"
          selectedTaskID="task_a"
          onSelectTask={navigate}
        />
      </MemoryRouter>,
    );
    await screen.findByText(zh.projects.tasks.historyOnly);
    expect(
      screen.queryByRole("button", { name: zh.projects.tasks.retry }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: zh.projects.tasks.editBranch }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: zh.projects.tasks.regenerate }),
    ).not.toBeInTheDocument();
    await user.click(
      screen.getByRole("button", { name: zh.projects.tasks.viewSource }),
    );
    expect(navigate).toHaveBeenCalledWith("source_task");
  },
);
const imageAttachment: TaskInputAttachment = {
  attachment_id: "image_a",
  name: "截图.png",
  media_type: "image/png",
  byte_count: 5,
  sha256: "a".repeat(64),
  preview_url: "/api/project-tasks/attachments/image_a",
  created_at: "2026-10-15T00:00:00Z",
};
const fileAttachment: TaskInputAttachment = {
  ...imageAttachment,
  attachment_id: "file_a",
  name: "报告.pdf",
  media_type: "application/pdf",
  workspace_path: "inbound/file_a/报告.pdf",
  preview_url: "/api/project-tasks/attachments/file_a",
};
function mount(query = "", projectID: string | null = "proj_a") {
  return render(
    <MemoryRouter initialEntries={["/box-projects/proj_a" + query]}>
      <TaskConversations projectID={projectID} />
    </MemoryRouter>,
  );
}

describe("项目任务对话", () => {
  it("支持选择通用文件并以纯附件创建任务，文件显示下载卡片", async () => {
    api.uploadAttachment.mockResolvedValueOnce(fileAttachment);
    const user = userEvent.setup();
    mount();
    const input = await screen.findByLabelText("添加附件", {
      selector: "input",
    });
    await waitFor(() => expect(input).toBeEnabled());
    expect(input).not.toHaveAttribute("accept");
    const file = new File(["report"], "报告.pdf", { type: "application/pdf" });
    await user.upload(input, file);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "发送" })).toBeEnabled(),
    );
    expect(api.uploadAttachment).toHaveBeenCalledWith(
      file,
      expect.any(AbortSignal),
    );
    expect(screen.queryByAltText("报告.pdf")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "下载" })).toHaveAttribute(
      "download",
      "报告.pdf",
    );
    await user.click(screen.getByRole("button", { name: "发送" }));
    expect(api.create).toHaveBeenCalledWith(
      "proj_a",
      expect.objectContaining({ prompt: "", attachment_ids: ["file_a"] }),
    );
  });

  it("上传完成后支持纯图片创建任务，并在发送期间冻结图片", async () => {
    let finishUpload!: (value: TaskInputAttachment) => void;
    api.uploadAttachment.mockReturnValueOnce(
      new Promise((resolve) => {
        finishUpload = resolve;
      }),
    );
    let finishCreate!: (value: TaskDetail) => void;
    api.create.mockReturnValueOnce(
      new Promise((resolve) => {
        finishCreate = resolve;
      }),
    );
    const user = userEvent.setup();
    mount();
    const input = await screen.findByLabelText("添加附件", {
      selector: "input",
    });
    await waitFor(() => expect(input).toBeEnabled());
    const file = new File(["image"], "截图.png", { type: "image/png" });
    await user.upload(input, file);
    expect(api.uploadAttachment).toHaveBeenCalledWith(
      file,
      expect.any(AbortSignal),
    );
    expect(screen.getByRole("button", { name: "发送" })).toBeDisabled();
    expect(screen.getByAltText("截图.png")).toBeInTheDocument();
    await act(async () => finishUpload(imageAttachment));
    await user.click(screen.getByRole("button", { name: "发送" }));
    expect(api.create).toHaveBeenCalledWith(
      "proj_a",
      expect.objectContaining({
        prompt: "",
        attachment_ids: ["image_a"],
      }),
    );
    expect(screen.getByRole("button", { name: /移除附件/ })).toBeDisabled();
    expect(input).toBeDisabled();
    await act(async () => finishCreate(fixture));
    await waitFor(() =>
      expect(screen.queryByAltText("截图.png")).not.toBeInTheDocument(),
    );
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:task-image-1");
  });

  it("图文追问沿用当前任务，不创建新任务", async () => {
    const user = userEvent.setup();
    mount("?task=task_a");
    await screen.findByText("已记住蓝鸟");
    await user.upload(
      screen.getByLabelText("添加附件", { selector: "input" }),
      new File(["image"], "截图.png", { type: "image/png" }),
    );
    await user.type(
      screen.getByRole("textbox", { name: "任务消息" }),
      "看看这张截图",
    );
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "发送" })).toBeEnabled(),
    );
    await user.click(screen.getByRole("button", { name: "发送" }));
    expect(api.send).toHaveBeenCalledWith(
      "task_a",
      expect.objectContaining({
        prompt: "看看这张截图",
        attachment_ids: ["image_a"],
      }),
    );
    expect(api.create).not.toHaveBeenCalled();
  });

  it("网络重传保留同一图片引用，更换图片后使用新的发送标识", async () => {
    api.create.mockRejectedValueOnce(new Error("network unavailable"));
    api.create.mockRejectedValueOnce(new Error("network unavailable"));
    api.uploadAttachment.mockResolvedValueOnce(imageAttachment);
    api.uploadAttachment.mockResolvedValueOnce({
      ...imageAttachment,
      attachment_id: "image_b",
      name: "新图.png",
    });
    const user = userEvent.setup();
    mount();
    const input = await screen.findByLabelText("添加附件", {
      selector: "input",
    });
    await waitFor(() => expect(input).toBeEnabled());
    await user.upload(
      input,
      new File(["image"], "截图.png", { type: "image/png" }),
    );
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "发送" })).toBeEnabled(),
    );
    await user.click(screen.getByRole("button", { name: "发送" }));
    await screen.findByText("network unavailable");
    await user.click(screen.getByRole("button", { name: "发送" }));
    await waitFor(() => expect(api.create).toHaveBeenCalledTimes(2));
    expect(api.create.mock.calls[0][1]).toEqual(api.create.mock.calls[1][1]);
    expect(screen.getByAltText("截图.png")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /移除附件/ }));
    await user.upload(
      input,
      new File(["new"], "新图.png", { type: "image/png" }),
    );
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "发送" })).toBeEnabled(),
    );
    await user.click(screen.getByRole("button", { name: "发送" }));
    expect(api.create.mock.calls[2][1].attachment_ids).toEqual(["image_b"]);
    expect(api.create.mock.calls[2][1].request_id).not.toBe(
      api.create.mock.calls[0][1].request_id,
    );
    expect(api.uploadAttachment).toHaveBeenCalledTimes(2);
  });

  it("支持粘贴和拖入通用文件，并阻止失败上传的附件发送", async () => {
    api.uploadAttachment.mockResolvedValueOnce(fileAttachment);
    api.uploadAttachment.mockRejectedValueOnce(new Error("upload rejected"));
    mount();
    const composer = await screen.findByRole("textbox", { name: "任务消息" });
    await waitFor(() => expect(composer).toBeEnabled());
    fireEvent.paste(composer, {
      clipboardData: {
        files: [new File(["report"], "粘贴.pdf", { type: "application/pdf" })],
      },
    });
    await waitFor(() => expect(api.uploadAttachment).toHaveBeenCalledTimes(1));
    fireEvent.drop(composer, {
      dataTransfer: {
        files: [new File(["data"], "拖入.csv", { type: "text/csv" })],
      },
    });
    await screen.findByText("upload rejected");
    expect(api.uploadAttachment).toHaveBeenCalledTimes(2);
    expect(screen.getByRole("button", { name: "发送" })).toBeDisabled();
  });

  it("切换任务时清空通用附件，中止上传并忽略迟到的结果", async () => {
    let finishUpload!: (value: TaskInputAttachment) => void;
    api.uploadAttachment.mockReturnValueOnce(
      new Promise((resolve) => {
        finishUpload = resolve;
      }),
    );
    const user = userEvent.setup();
    mount("?task=task_a");
    await screen.findByText("已记住蓝鸟");
    await user.upload(
      screen.getByLabelText("添加附件", { selector: "input" }),
      new File(["report"], "旧任务.pdf", { type: "application/pdf" }),
    );
    const signal = api.uploadAttachment.mock.calls[0][1]!;
    await user.click(screen.getByRole("button", { name: "新任务" }));
    expect(signal.aborted).toBe(true);
    expect(screen.queryByText("旧任务.pdf")).not.toBeInTheDocument();
    await act(async () => finishUpload(fileAttachment));
    expect(screen.getByRole("button", { name: "发送" })).toBeDisabled();
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:task-image-1");
    await user.type(
      screen.getByRole("textbox", { name: "任务消息" }),
      "新的文字任务",
    );
    await user.click(screen.getByRole("button", { name: "发送" }));
    expect(api.create.mock.calls[0][1].attachment_ids).toBeUndefined();
  });

  it("切换项目时清空图片并中止旧上传", async () => {
    let finishUpload!: (value: TaskInputAttachment) => void;
    api.uploadAttachment.mockReturnValueOnce(
      new Promise((resolve) => {
        finishUpload = resolve;
      }),
    );
    const user = userEvent.setup();
    const view = mount();
    const input = await screen.findByLabelText("添加附件", {
      selector: "input",
    });
    await waitFor(() => expect(input).toBeEnabled());
    await user.upload(
      input,
      new File(["image"], "旧项目.png", { type: "image/png" }),
    );
    const signal = api.uploadAttachment.mock.calls[0][1]!;
    view.rerender(
      <MemoryRouter initialEntries={["/box-projects/proj_b"]}>
        <TaskConversations projectID="proj_b" />
      </MemoryRouter>,
    );
    expect(signal.aborted).toBe(true);
    await act(async () => finishUpload(imageAttachment));
    expect(screen.queryByAltText("旧项目.png")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "发送" })).toBeDisabled();
  });

  it("9 个混合附件可直接发送，第 10 个及超大文件不上传或发送", async () => {
    api.uploadAttachment.mockImplementation(async (file) => ({
      ...imageAttachment,
      attachment_id: `image_${file.name}`,
      name: file.name,
      media_type: file.type,
    }));
    const user = userEvent.setup();
    mount();
    const input = await screen.findByLabelText("添加附件", {
      selector: "input",
    });
    await waitFor(() => expect(input).toBeEnabled());
    const large = new File(["image"], "大图.png", { type: "image/png" });
    Object.defineProperty(large, "size", { value: 25 * 1024 * 1024 + 1 });
    fireEvent.change(input, { target: { files: [large] } });
    await screen.findByText(
      "每个附件不能超过 25 MiB；服务端可能设置更低的上传限额。",
    );
    expect(api.uploadAttachment).not.toHaveBeenCalled();
    fireEvent.change(input, {
      target: {
        files: Array.from({ length: 9 }, (_, i) =>
          i % 2
            ? new File(["text"], `说明${i}.txt`, { type: "text/plain" })
            : new File(["image"], `图片${i}.png`, { type: "image/png" }),
        ),
      },
    });
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "发送" })).toBeEnabled(),
    );
    expect(api.uploadAttachment).toHaveBeenCalledTimes(9);
    expect(input).toBeDisabled();
    expect(screen.getByRole("button", { name: "添加附件" })).toBeDisabled();
    expect(
      screen.queryByText("每条消息最多添加 9 个附件。"),
    ).not.toBeInTheDocument();
    fireEvent.drop(screen.getByRole("textbox", { name: "任务消息" }), {
      dataTransfer: {
        files: [new File(["image"], "第十张.png", { type: "image/png" })],
      },
    });
    await screen.findByText("每条消息最多添加 9 个附件。");
    expect(api.uploadAttachment).toHaveBeenCalledTimes(9);
    expect(screen.queryByAltText("第十张.png")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "发送" }));
    expect(api.create).toHaveBeenCalledWith(
      "proj_a",
      expect.objectContaining({
        prompt: "",
        attachment_ids: Array.from(
          { length: 9 },
          (_, i) => `image_${i % 2 ? `说明${i}.txt` : `图片${i}.png`}`,
        ),
      }),
    );
  });

  it("历史图片通过鉴权接口加载，关闭对话时释放预览 URL", async () => {
    api.open.mockResolvedValue({
      ...fixture,
      turns: [
        { ...fixture.turns[0], prompt: "", attachments: [imageAttachment] },
      ],
    });
    const view = mount("?task=task_a");
    const image = await screen.findByAltText("截图.png");
    await waitFor(() =>
      expect(image).toHaveAttribute("src", "blob:task-image-1"),
    );
    expect(api.attachmentPreview).toHaveBeenCalledWith(
      "image_a",
      expect.any(AbortSignal),
    );
    const signal = api.attachmentPreview.mock.calls[0][1]!;
    view.unmount();
    expect(signal.aborted).toBe(true);
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:task-image-1");
  });

  it("历史图片加载失败可重新加载", async () => {
    api.attachmentPreview.mockRejectedValueOnce(
      new Error("network unavailable"),
    );
    api.open.mockResolvedValue({
      ...fixture,
      turns: [{ ...fixture.turns[0], attachments: [imageAttachment] }],
    });
    const user = userEvent.setup();
    mount("?task=task_a");
    await user.click(
      await screen.findByRole("button", { name: "重新加载图片" }),
    );
    await screen.findByAltText("截图.png");
    expect(api.attachmentPreview).toHaveBeenCalledTimes(2);
  });

  it("混合历史附件保留图片预览，通用文件按需鉴权下载并保留文件名", async () => {
    const svgAttachment = {
      ...fileAttachment,
      attachment_id: "file_svg",
      name: "图示.svg",
      media_type: "image/svg+xml",
    };
    api.open.mockResolvedValue({
      ...fixture,
      turns: [
        {
          ...fixture.turns[0],
          attachments: [imageAttachment, fileAttachment, svgAttachment],
        },
      ],
    });
    const clicked: HTMLAnchorElement[] = [];
    const click = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(function () {
        clicked.push(this);
      });
    try {
      mount("?task=task_a");
      await screen.findByAltText("截图.png");
      expect(screen.getByText("报告.pdf")).toBeInTheDocument();
      expect(screen.getByText("图示.svg")).toBeInTheDocument();
      expect(screen.queryByAltText("图示.svg")).not.toBeInTheDocument();
      expect(api.attachmentPreview).toHaveBeenCalledTimes(1);
      fireEvent.click(screen.getAllByRole("button", { name: "下载" })[0]);
      await waitFor(() => expect(clicked).toHaveLength(1));
      expect(api.attachmentPreview).toHaveBeenLastCalledWith(
        "file_a",
        expect.any(AbortSignal),
      );
      expect(clicked[0].download).toBe("报告.pdf");
      expect(clicked[0].href).toMatch(/^blob:task-image-/);
      expect(URL.revokeObjectURL).toHaveBeenCalledWith(clicked[0].href);
    } finally {
      click.mockRestore();
    }
  });

  it("历史文件下载失败可以重试，关闭对话中止未完成的下载", async () => {
    api.open.mockResolvedValue({
      ...fixture,
      turns: [{ ...fixture.turns[0], attachments: [fileAttachment] }],
    });
    api.attachmentPreview.mockRejectedValueOnce(new Error("download failed"));
    let finishDownload!: (value: Blob) => void;
    api.attachmentPreview.mockReturnValueOnce(
      new Promise((resolve) => {
        finishDownload = resolve;
      }),
    );
    const user = userEvent.setup();
    const view = mount("?task=task_a");
    await user.click(await screen.findByRole("button", { name: "下载" }));
    await screen.findByText("附件下载失败，请重试。");
    await user.click(screen.getByRole("button", { name: "下载" }));
    const signal = api.attachmentPreview.mock.calls[1][1]!;
    view.unmount();
    expect(signal.aborted).toBe(true);
    await act(async () => finishDownload(new Blob(["report"])));
    expect(URL.createObjectURL).not.toHaveBeenCalled();
  });

  it("首条发送创建任务，并打开持久化的对话", async () => {
    const user = userEvent.setup();
    mount();
    const composer = await screen.findByRole("textbox", { name: "任务消息" });
    await waitFor(() => expect(composer).toBeEnabled());
    await user.type(composer, "记住蓝鸟");
    await user.click(screen.getByRole("button", { name: "发送" }));
    await waitFor(() =>
      expect(api.create).toHaveBeenCalledWith(
        "proj_a",
        expect.objectContaining({
          prompt: "记住蓝鸟",
          web_search_enabled: true,
          request_id: expect.any(String),
        }),
      ),
    );
    await waitFor(() => expect(api.events).toHaveBeenCalledTimes(1));
    expect(screen.getByText("已记住蓝鸟")).toBeInTheDocument();
    expect(api.open).toHaveBeenCalledWith("task_a");
  });
  it("打开历史后发送下一轮，使用同一个任务", async () => {
    const user = userEvent.setup();
    mount("?task=task_a");
    expect(await screen.findByText("已记住蓝鸟")).toBeInTheDocument();
    await user.type(
      screen.getByRole("textbox", { name: "任务消息" }),
      "记住的是什么？",
    );
    await user.click(screen.getByRole("button", { name: "发送" }));
    await waitFor(() =>
      expect(api.send).toHaveBeenCalledWith(
        "task_a",
        expect.objectContaining({ prompt: "记住的是什么？" }),
      ),
    );
    expect(api.create).not.toHaveBeenCalled();
  });
  it("网络失败保留输入，重试复用发送标识以防重复执行", async () => {
    api.create.mockRejectedValueOnce(new Error("network unavailable"));
    const user = userEvent.setup();
    mount();
    const composer = await screen.findByRole("textbox", { name: "任务消息" });
    await waitFor(() => expect(composer).toBeEnabled());
    await user.type(composer, "同一个请求");
    await user.click(screen.getByRole("button", { name: "发送" }));
    expect(await screen.findByText("network unavailable")).toBeInTheDocument();
    expect(composer).toHaveValue("同一个请求");
    await user.click(screen.getByRole("button", { name: "发送" }));
    await waitFor(() => expect(api.create).toHaveBeenCalledTimes(2));
    expect(api.create.mock.calls[0][1]).toEqual(api.create.mock.calls[1][1]);
  });
  it("仅已有任务提供压缩记忆入口", async () => {
    const user = userEvent.setup();
    mount();
    expect(screen.queryByRole("button", { name: "压缩记忆" })).toBeNull();
    await user.click(await screen.findByRole("button", { name: /第一项任务/ }));
    expect(await conversationTool("压缩记忆")).toBeEnabled();
  });
  it("手动压缩只发送控制命令，保留草稿和待发附件并阻止重复点击", async () => {
    let finishSend!: (value: TaskDetail) => void;
    api.send.mockReturnValueOnce(
      new Promise((resolve) => {
        finishSend = resolve;
      }),
    );
    const user = userEvent.setup();
    mount("?task=task_a");
    await conversationTool("压缩记忆");
    const composer = screen.getByRole("textbox", { name: "任务消息" });
    await user.type(composer, "保留这段追问");
    await user.upload(
      screen.getByLabelText("添加附件", { selector: "input" }),
      new File(["image"], "截图.png", { type: "image/png" }),
    );
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "发送" })).toBeEnabled(),
    );
    await conversationTool("压缩记忆");
    const compact = await conversationTool("压缩记忆");
    fireEvent.click(compact);
    fireEvent.click(compact);
    expect(api.send).toHaveBeenCalledTimes(1);
    expect(api.send).toHaveBeenCalledWith("task_a", {
      request_id: expect.any(String),
      prompt: "/compact",
      expected_configuration_revision: 1,
      expected_project_revision: null,
    });
    expect(compact).toBeDisabled();
    expect(composer).toHaveValue("保留这段追问");
    expect(screen.getByAltText("截图.png")).toBeInTheDocument();
    await act(async () => finishSend(fixture));
    expect(composer).toHaveValue("保留这段追问");
    expect(screen.getByAltText("截图.png")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "发送" }));
    expect(api.send.mock.calls[1][1]).toMatchObject({
      prompt: "保留这段追问",
      attachment_ids: ["image_a"],
    });
    expect(api.create).not.toHaveBeenCalled();
  });
  it("压缩请求失败后复用请求标识，成功后再次压缩使用新标识", async () => {
    api.send.mockRejectedValueOnce(new Error("network unavailable"));
    const user = userEvent.setup();
    mount("?task=task_a");
    await conversationTool("压缩记忆");
    const composer = screen.getByRole("textbox", { name: "任务消息" });
    await user.type(composer, "尚未发送的追问");
    await user.click(await conversationTool("压缩记忆"));
    await screen.findByText("network unavailable");
    expect(composer).toHaveValue("尚未发送的追问");
    await user.click(await conversationTool("压缩记忆"));
    await waitFor(() => expect(api.send).toHaveBeenCalledTimes(2));
    expect(api.send.mock.calls[0][1]).toEqual(api.send.mock.calls[1][1]);
    await user.click(await conversationTool("压缩记忆"));
    await waitFor(() => expect(api.send).toHaveBeenCalledTimes(3));
    expect(api.send.mock.calls[2][1].request_id).not.toBe(
      api.send.mock.calls[0][1].request_id,
    );
    expect(composer).toHaveValue("尚未发送的追问");
  });
  it("压缩回执丢失后通过实时详情确认，普通追问后再次压缩创建新请求", async () => {
    api.send.mockRejectedValueOnce(new Error("network unavailable"));
    const user = userEvent.setup();
    mount("?task=task_a");
    await conversationTool("压缩记忆");
    await user.click(await conversationTool("压缩记忆"));
    await screen.findByText("network unavailable");
    const requestID = api.send.mock.calls[0][1].request_id;
    const compactTurn = {
      ...fixture.turns[0],
      sequence: 2,
      turn_id: "turn_compact",
      request_id: requestID,
      prompt: "/compact",
      reply: "已压缩任务上下文",
    };
    await act(async () =>
      api.events.mock.calls[0][2]({
        type: "turn",
        data: {
          task: { ...fixture, list_revision: 2 },
          turn: compactTurn,
          cursor: "compact_confirmed",
        },
      }),
    );
    await screen.findByText("已压缩任务上下文");
    api.send.mockImplementationOnce(async (_id, body) => ({
      ...fixture,
      list_revision: 3,
      turns: [
        fixture.turns[0],
        compactTurn,
        {
          ...fixture.turns[0],
          sequence: 3,
          turn_id: "turn_followup",
          request_id: body.request_id,
          prompt: body.prompt ?? "",
          reply: "已补充结果",
        },
      ],
    }));
    await user.type(
      screen.getByRole("textbox", { name: "任务消息" }),
      "继续整理",
    );
    await user.click(screen.getByRole("button", { name: "发送" }));
    await screen.findByText("已补充结果");
    await user.click(await conversationTool("压缩记忆"));
    expect(api.send.mock.calls[2][1]).toEqual({
      request_id: expect.any(String),
      prompt: "/compact",
      expected_configuration_revision: 1,
      expected_project_revision: null,
    });
    expect(api.send.mock.calls[2][1].request_id).not.toBe(requestID);
  });
  it("压缩仅透传显式模型，失败后切换模型仍重试原请求", async () => {
    api.resources.mockResolvedValue({
      skills: [],
      mcp_servers: [],
      plugins: [],
      subagents: [],
      directory_roots: [],
      models: [
        { model_ref: "glm/glm", name: "GLM", supports_images: false },
        { model_ref: "other/model", name: "Other", supports_images: false },
      ],
      approval: {
        allowed_modes: ["ask_for_approval", "approve_for_me", "full_access"],
        reviewer_ready: true,
        execution_profile: "host_process",
      },
      default_model_ref: "glm/glm",
    });
    api.send.mockRejectedValueOnce(new Error("network unavailable"));
    const user = userEvent.setup();
    mount("?task=task_a");
    await conversationTool("压缩记忆");
    await user.click(
      screen.getByRole("button", { name: zh.projects.tasks.executionOptions }),
    );
    const model = await screen.findByRole("combobox", { name: "本轮模型" });
    await user.click(model);
    await user.click(await screen.findByText("GLM · glm/glm"));
    await turnSettings();
    await user.click(screen.getByRole("switch", { name: "自定义本轮 Skill" }));
    await user.click(screen.getByRole("switch", { name: "自定义本轮 MCP" }));
    await user.click(await conversationTool("压缩记忆"));
    await screen.findByText("network unavailable");
    expect(api.send.mock.calls[0][1]).toEqual({
      request_id: expect.any(String),
      prompt: "/compact",
      expected_configuration_revision: 1,
      expected_project_revision: null,
      options: { model_ref: "glm/glm" },
    });
    await user.click(model);
    await user.click(await screen.findByText("Other · other/model"));
    await user.click(await conversationTool("压缩记忆"));
    expect(api.send.mock.calls[1][1]).toEqual(api.send.mock.calls[0][1]);
    await user.click(await conversationTool("压缩记忆"));
    expect(api.send.mock.calls[2][1].options).toEqual({
      model_ref: "other/model",
    });
  });
  it.each([
    { state: "运行中", active_turn_id: "turn_a", archived: false },
    { state: "已归档", active_turn_id: null, archived: true },
  ])("任务$state时禁用手动压缩", async (changes) => {
    api.open.mockResolvedValue({ ...fixture, ...changes });
    mount("?task=task_a");
    expect(await conversationTool("压缩记忆")).toBeDisabled();
    expect(api.send).not.toHaveBeenCalled();
  });
  it.each([
    { attachments: [], compacting: true },
    { attachments: [imageAttachment], compacting: false },
  ])(
    "压缩维护轮不可暂停但仍可停止，是否维护轮：$compacting",
    async (changes) => {
      api.open.mockResolvedValue({
        ...fixture,
        active_turn_id: "turn_a",
        turns: [
          {
            ...fixture.turns[0],
            prompt: "/compact",
            attachments: changes.attachments,
            status: "running",
          },
        ],
      });
      const user = userEvent.setup();
      mount("?task=task_a");
      const pause = await screen.findByRole("button", { name: "暂停" });
      if (changes.compacting) {
        expect(pause).toBeDisabled();
        await user.click(pause);
        expect(api.control).not.toHaveBeenCalled();
      } else expect(pause).toBeEnabled();
      await user.click(screen.getByRole("button", { name: "停止回复" }));
      expect(api.cancel).toHaveBeenCalledWith("task_a", "turn_a");
    },
  );
  it("执行中可以停止，历史回复保留", async () => {
    api.open.mockResolvedValue({
      ...fixture,
      active_turn_id: "turn_a",
      turns: [{ ...fixture.turns[0], status: "running" }],
    });
    const user = userEvent.setup();
    mount("?task=task_a");
    await user.click(await screen.findByRole("button", { name: "停止回复" }));
    expect(api.cancel).toHaveBeenCalledWith("task_a", "turn_a");
    expect(await screen.findByText("已记住蓝鸟")).toBeInTheDocument();
  });
});

it("选择任务沿用已加载列表，后续只请求增量", async () => {
  const user = userEvent.setup();
  mount();
  await user.click(await screen.findByRole("button", { name: /第一项任务/ }));
  expect(await screen.findByText("已记住蓝鸟")).toBeInTheDocument();
  expect(api.list).toHaveBeenCalledTimes(1);
  expect(api.events).toHaveBeenCalledWith(
    "task_a",
    "event_first",
    expect.any(Function),
    expect.any(AbortSignal),
    expect.any(Function),
  );
});

it("一页任务直接显示列表状态，增量更新无需逐个读取详情", async () => {
  const statuses: TaskStatus[] = [
    "queued",
    "running",
    "completed",
    "failed",
    "cancelled",
  ];
  const items = statuses.map((status) => ({
    ...fixture,
    task_id: `task_${status}`,
    title: `任务 ${status}`,
    status,
    active_turn_id:
      status === "queued" || status === "running" ? `turn_${status}` : null,
  }));
  api.list.mockResolvedValue({
    items,
    next_page_cursor: null,
    sync_cursor: "sync_first",
  });
  const running = { ...items[0], status: "running" as const, list_revision: 2 };
  api.sync.mockResolvedValue({
    base_sync_cursor: "sync_first",
    next_sync_cursor: "sync_next",
    next_page_cursor: null,
    changes: [
      {
        operation: "upsert",
        task_id: running.task_id,
        list_revision: 2,
        data: running,
      },
    ],
  });
  mount();
  for (const status of statuses) {
    expect(
      await screen.findByRole("button", {
        name: new RegExp(`任务 ${status} ${zh.projects.tasks.status[status]}`),
      }),
    ).toBeInTheDocument();
  }
  expect(api.list).toHaveBeenCalledTimes(1);
  expect(api.get).not.toHaveBeenCalled();
  expect(api.events).not.toHaveBeenCalled();
  expect(api.open).not.toHaveBeenCalled();
  fireEvent(document, new Event("visibilitychange"));
  await screen.findByRole("button", { name: /任务 queued 回复中/ });
  expect(api.get).not.toHaveBeenCalled();
});

it("历史分页晚到不会覆盖增量更新的摘要", async () => {
  let resolvePage!: (
    value: Awaited<ReturnType<typeof projectTasksApi.list>>,
  ) => void;
  api.list.mockResolvedValueOnce({
    items: [fixture],
    next_page_cursor: "history_next",
    sync_cursor: "sync_first",
  });
  api.list.mockImplementationOnce(
    () =>
      new Promise((resolve) => {
        resolvePage = resolve;
      }),
  );
  const updated = {
    ...fixture,
    title: "新的任务标题",
    status: "failed" as const,
    list_revision: 3,
  };
  api.sync.mockResolvedValue({
    base_sync_cursor: "sync_first",
    next_sync_cursor: "sync_next",
    next_page_cursor: null,
    changes: [
      {
        operation: "upsert",
        task_id: updated.task_id,
        list_revision: 3,
        data: updated,
      },
    ],
  });
  const user = userEvent.setup();
  mount();
  await user.click(
    await screen.findByRole("button", { name: "加载更早的任务" }),
  );
  await waitFor(() =>
    expect(api.list).toHaveBeenCalledWith("proj_a", "history_next"),
  );
  fireEvent(document, new Event("visibilitychange"));
  await screen.findByRole("button", { name: /新的任务标题/ });
  await act(async () =>
    resolvePage({
      items: [
        fixture,
        { ...fixture, task_id: "task_old", title: "更早的任务" },
      ],
      next_page_cursor: null,
      sync_cursor: null,
    }),
  );
  expect(
    screen.getByRole("button", { name: /新的任务标题/ }),
  ).toHaveTextContent("执行失败");
  expect(
    screen.getByRole("button", { name: /更早的任务/ }),
  ).toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: /第一项任务/ }),
  ).not.toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: "加载更早的任务" }),
  ).not.toBeInTheDocument();
});

it("增量游标失效只重取第一页", async () => {
  api.sync.mockRejectedValueOnce(
    new Error('409 - {"error":{"code":"PROJECT_SYNC_RESET"}}'),
  );
  const view = mount();
  await screen.findByRole("button", { name: /第一项任务/ });
  fireEvent(document, new Event("visibilitychange"));
  await waitFor(() => expect(api.list).toHaveBeenCalledTimes(2));
  expect(api.list.mock.calls).toEqual([["proj_a"], ["proj_a"]]);
  view.unmount();
});

it("实时显示回复与终态，迟到的停止响应不能恢复执行中状态", async () => {
  const running: TaskDetail = {
    ...fixture,
    active_turn_id: "turn_a",
    turns: [{ ...fixture.turns[0], reply: "部分", status: "running" }],
  };
  api.open.mockResolvedValue(running);
  let finishStop!: (value: TaskDetail) => void;
  api.cancel.mockImplementation(
    () =>
      new Promise((resolve) => {
        finishStop = resolve;
      }),
  );
  const user = userEvent.setup();
  mount("?task=task_a");
  await waitFor(() => expect(api.events).toHaveBeenCalledTimes(1));
  const receive = api.events.mock.calls[0][2];
  await act(async () =>
    receive({
      type: "turn",
      data: {
        task: running,
        turn: { ...running.turns[0], reply: "持续输出" },
        cursor: "event_live",
      },
    }),
  );
  expect(await screen.findByText("持续输出")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "停止回复" }));
  const completed = { ...fixture, list_revision: 2 };
  await act(async () =>
    receive({
      type: "turn",
      data: {
        task: completed,
        turn: { ...fixture.turns[0], reply: "完整回复" },
        cursor: "event_done",
      },
    }),
  );
  await act(async () =>
    finishStop({
      ...running,
      turns: [{ ...running.turns[0], cancel_requested: true }],
    }),
  );
  expect(screen.getByText("完整回复")).toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: "停止回复" }),
  ).not.toBeInTheDocument();
  expect(api.open).toHaveBeenCalledTimes(1);
  expect(api.get).not.toHaveBeenCalled();
});

it("加载更早对话保留实时收到的新轮次", async () => {
  api.open.mockResolvedValueOnce({
    ...fixture,
    next_page_cursor: "messages_older",
  });
  let finishPage!: (value: TaskDetail) => void;
  api.get.mockImplementationOnce(
    () =>
      new Promise((resolve) => {
        finishPage = resolve;
      }),
  );
  const user = userEvent.setup();
  mount("?task=task_a");
  await waitFor(() => expect(api.events).toHaveBeenCalledTimes(1));
  await user.click(screen.getByRole("button", { name: "加载更早的对话" }));
  const turn = {
    ...fixture.turns[0],
    turn_id: "turn_b",
    sequence: 3,
    prompt: "新的追问",
    reply: "新的回复",
  };
  await act(async () =>
    api.events.mock.calls[0][2]({
      type: "turn",
      data: {
        task: { ...fixture, list_revision: 3 },
        turn,
        cursor: "event_new",
      },
    }),
  );
  await act(async () =>
    finishPage({
      ...fixture,
      event_cursor: null,
      turns: [
        {
          ...fixture.turns[0],
          turn_id: "turn_old",
          sequence: 0,
          prompt: "早期消息",
          reply: "早期回答",
        },
      ],
    }),
  );
  expect(screen.getByText("早期回答")).toBeInTheDocument();
  expect(screen.getByText("已记住蓝鸟")).toBeInTheDocument();
  expect(screen.getByText("新的回复")).toBeInTheDocument();
  await act(async () =>
    api.events.mock.calls[0][2]({
      type: "turn",
      data: {
        task: { ...fixture, list_revision: 3 },
        turn: {
          ...fixture.turns[0],
          sequence: 2,
          turn_id: "turn_middle",
          reply: "中间回复",
        },
        cursor: "event_middle",
      },
    }),
  );
  expect(
    screen
      .getByText("中间回复")
      .compareDocumentPosition(screen.getByText("新的回复")) &
      Node.DOCUMENT_POSITION_FOLLOWING,
  ).toBeTruthy();
  expect(api.get).toHaveBeenCalledWith("task_a", "messages_older");
  expect(
    screen.queryByRole("button", { name: "加载更早的对话" }),
  ).not.toBeInTheDocument();
});

it("失败后手动重试保留原轮次，网络重传沿用重试标识", async () => {
  const failed = {
    ...fixture,
    turns: [
      {
        ...fixture.turns[0],
        status: "failed" as const,
        error_code: "TASK_EXECUTION_FAILED",
      },
    ],
  };
  api.open.mockResolvedValue(failed);
  api.retry.mockRejectedValueOnce(new Error("lost response"));
  api.retry.mockResolvedValue({
    ...failed,
    active_turn_id: "turn_retry",
    list_revision: 2,
    turns: [
      ...failed.turns,
      {
        ...fixture.turns[0],
        turn_id: "turn_retry",
        reply: "",
        status: "queued",
        retry_of_turn_id: "turn_a",
      },
    ],
  });
  const user = userEvent.setup();
  mount("?task=task_a");
  await user.click(await screen.findByRole("button", { name: "重新尝试" }));
  expect(await screen.findByText("lost response")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "重新尝试" }));
  await waitFor(() => expect(api.retry).toHaveBeenCalledTimes(2));
  expect(api.retry.mock.calls[0]).toEqual(api.retry.mock.calls[1]);
  expect(api.retry.mock.calls[0].slice(0, 2)).toEqual(["task_a", "turn_a"]);
  expect(screen.getByText("已记住蓝鸟")).toBeInTheDocument();
  expect(api.send).not.toHaveBeenCalled();
});

it.each([
  ["failed", "directory"],
  ["failed", "client_directory"],
  ["cancelled", "directory"],
  ["cancelled", "client_directory"],
] as const)(
  "%s 轮次在 %s 目录变更后重试前须确认新执行范围",
  async (status, field) => {
    const failed: TaskDetail = {
      ...fixture,
      status,
      configuration_revision: 3,
      turns: [
        {
          ...fixture.turns[0],
          status,
          server_directory: projectFixture.directory,
          client_directory: projectFixture.client_directory,
        },
      ],
    };
    const changed = {
      ...projectFixture,
      [field]: projectFixture[field] + "/after",
      revision: 2,
    };
    api.open.mockResolvedValue(failed);
    api.retry.mockResolvedValue(failed);
    const page = (project: Project) => (
      <MemoryRouter initialEntries={["/box-projects/proj_a?task=task_a"]}>
        <TaskConversations projectID="proj_a" project={project} />
      </MemoryRouter>
    );
    const user = userEvent.setup();
    const view = render(page(changed));
    const retry = await screen.findByRole("button", {
      name: zh.projects.tasks.retry,
    });
    await screen.findByText(zh.projects.tasks.directoryChanged);
    expect(retry).toBeDisabled();
    await user.click(retry);
    expect(api.retry).not.toHaveBeenCalled();
    await user.click(
      screen.getByRole("button", { name: zh.projects.tasks.confirmDirectory }),
    );
    await waitFor(() => expect(retry).toBeEnabled());
    await user.click(retry);
    await waitFor(() => expect(api.retry).toHaveBeenCalledTimes(1));
    expect(api.retry).toHaveBeenCalledWith(
      "task_a",
      "turn_a",
      expect.any(String),
      {},
      {
        expected_configuration_revision: 3,
        expected_project_revision: 2,
      },
    );
    view.rerender(
      page({ ...changed, [field]: changed[field] + "/again", revision: 3 }),
    );
    await screen.findByText(zh.projects.tasks.directoryChanged);
    expect(retry).toBeDisabled();
    await user.click(retry);
    expect(api.retry).toHaveBeenCalledTimes(1);
  },
);

it("断线使用最后收到的游标续接，不重新下载会话历史", async () => {
  let disconnect!: (reason: Error) => void;
  api.events.mockImplementationOnce(
    (_id, _cursor, _onEvent, _signal, onOpen) => {
      onOpen?.();
      return new Promise((_resolve, reject) => {
        disconnect = reject;
      });
    },
  );
  mount("?task=task_a");
  await waitFor(() => expect(api.events).toHaveBeenCalledTimes(1));
  await act(async () =>
    api.events.mock.calls[0][2]({
      type: "turn",
      data: {
        task: fixture,
        turn: fixture.turns[0],
        cursor: "event_received",
      },
    }),
  );
  vi.useFakeTimers();
  await act(async () => disconnect(new Error("network offline")));
  await act(async () => vi.advanceTimersByTimeAsync(2000));
  expect(api.events.mock.calls[1][1]).toBe("event_received");
  expect(api.open).toHaveBeenCalledTimes(1);
  expect(api.get).not.toHaveBeenCalled();
});

it("详情恢复前台只恢复订阅，不重复发送打开事件", async () => {
  const hidden = vi.spyOn(document, "hidden", "get");
  try {
    mount("?task=task_a");
    await waitFor(() => expect(api.events).toHaveBeenCalledTimes(1));
    const signal = api.events.mock.calls[0][3];
    hidden.mockReturnValue(true);
    await act(async () => fireEvent(document, new Event("visibilitychange")));
    expect(signal.aborted).toBe(true);
    hidden.mockReturnValue(false);
    await act(async () => fireEvent(document, new Event("visibilitychange")));
    await waitFor(() => expect(api.events).toHaveBeenCalledTimes(2));
    expect(api.open).toHaveBeenCalledTimes(1);
    expect(api.get).not.toHaveBeenCalled();
  } finally {
    hidden.mockRestore();
  }
});

it("打开回执丢失后只读恢复详情，不重复触发标题补生成", async () => {
  api.open.mockRejectedValueOnce(new Error("lost open receipt"));
  vi.useFakeTimers();
  await act(async () => mount("?task=task_a"));
  expect(api.open).toHaveBeenCalledTimes(1);
  await act(async () => vi.advanceTimersByTimeAsync(2000));
  expect(api.open).toHaveBeenCalledTimes(1);
  expect(api.get).toHaveBeenCalledWith("task_a");
  expect(api.events).toHaveBeenCalledTimes(1);
  expect(screen.getByText("已记住蓝鸟")).toBeInTheDocument();
});

it("切换任务会关闭旧连接，迟到的事件不会串入新会话", async () => {
  const user = userEvent.setup();
  mount("?task=task_a");
  await waitFor(() => expect(api.events).toHaveBeenCalledTimes(1));
  const [, , receive, signal] = api.events.mock.calls[0];
  await user.click(screen.getByRole("button", { name: "新任务" }));
  expect(signal.aborted).toBe(true);
  await act(async () =>
    receive({
      type: "turn",
      data: {
        task: fixture,
        turn: { ...fixture.turns[0], turn_id: "late", reply: "不该显示" },
        cursor: "late",
      },
    }),
  );
  expect(screen.queryByText("不该显示")).not.toBeInTheDocument();
});

it("实时游标失效时重建最近一页，不沿用失效浏览范围", async () => {
  api.open.mockResolvedValueOnce({ ...fixture, next_page_cursor: "old_range" });
  api.events.mockRejectedValueOnce(
    new Error('409 - {"error":{"code":"PROJECT_SYNC_RESET"}}'),
  );
  vi.useFakeTimers();
  await act(async () => {
    mount("?task=task_a");
  });
  expect(screen.getByText("连接中断，正在重新连接…")).toBeInTheDocument();
  await act(async () => vi.advanceTimersByTimeAsync(2000));
  expect(api.open.mock.calls).toEqual([["task_a"]]);
  expect(api.get.mock.calls).toEqual([["task_a"]]);
  expect(
    screen.queryByRole("button", { name: "加载更早的对话" }),
  ).not.toBeInTheDocument();
});

it("任务可以改名，提交读取到的摘要版本", async () => {
  api.update.mockResolvedValue({
    ...fixture,
    title: "新的会话名称",
    list_revision: 2,
  });
  const user = userEvent.setup();
  mount("?task=task_a");
  await user.click(await screen.findByRole("button", { name: "修改任务名称" }));
  const title = screen.getByRole("textbox", { name: "" });
  await user.clear(title);
  await user.type(title, "新的会话名称");
  await user.tab();
  await waitFor(() =>
    expect(api.update).toHaveBeenCalledWith("task_a", {
      title: "新的会话名称",
      expected_revision: 1,
    }),
  );
  expect(
    await screen.findByRole("heading", { name: /新的会话名称/ }),
  ).toBeInTheDocument();
  expect(screen.getByText("已记住蓝鸟")).toBeInTheDocument();
});

it("归档保留会话并关闭发送，恢复后仍可继续", async () => {
  let stored = fixture;
  api.list.mockImplementation(async (_projectID, _page, archived = false) => ({
    items: stored.archived === archived ? [stored] : [],
    next_page_cursor: null,
    sync_cursor: "sync_filtered",
  }));
  api.update.mockImplementation(async (_id, patch) => {
    stored = { ...stored, ...patch, list_revision: stored.list_revision + 1 };
    return stored;
  });
  const user = userEvent.setup();
  mount("?task=task_a");
  await user.click(await conversationTool("归档"));
  expect(await conversationTool("恢复任务")).toBeInTheDocument();
  expect(screen.getByRole("textbox", { name: "任务消息" })).toBeDisabled();
  expect(screen.getByText("已记住蓝鸟")).toBeInTheDocument();
  await waitFor(() =>
    expect(api.list).toHaveBeenCalledWith("proj_a", undefined, true),
  );
  await user.click(screen.getByRole("button", { name: "恢复任务" }));
  await waitFor(() =>
    expect(screen.getByRole("textbox", { name: "任务消息" })).toBeEnabled(),
  );
  expect(api.update.mock.calls.map((call) => call[1])).toEqual([
    { archived: true, expected_revision: 1 },
    { archived: false, expected_revision: 2 },
  ]);
});

it("外部归档后的旧历史页不会让任务重新出现在未归档列表", async () => {
  api.list.mockResolvedValueOnce({
    items: [fixture],
    next_page_cursor: "old_page",
    sync_cursor: "sync_first",
  });
  let finishPage!: (
    value: Awaited<ReturnType<typeof projectTasksApi.list>>,
  ) => void;
  api.list.mockImplementationOnce(
    () =>
      new Promise((resolve) => {
        finishPage = resolve;
      }),
  );
  const user = userEvent.setup();
  mount("?task=task_a");
  await waitFor(() => expect(api.events).toHaveBeenCalledTimes(1));
  await user.click(screen.getByRole("button", { name: "加载更早的任务" }));
  await act(async () =>
    api.events.mock.calls[0][2]({
      type: "turn",
      data: {
        task: { ...fixture, archived: true, list_revision: 2 },
        turn: fixture.turns[0],
        cursor: "archived_event",
      },
    }),
  );
  await act(async () =>
    finishPage({ items: [fixture], next_page_cursor: null, sync_cursor: null }),
  );
  expect(
    screen.queryByRole("button", { name: /第一项任务/ }),
  ).not.toBeInTheDocument();
  expect(screen.getByRole("textbox", { name: "任务消息" })).toBeDisabled();
});

it.each(["proj_a", null])(
  "删除任务确认后移除列表并关闭会话，所属项目为 %s",
  async (projectID) => {
    const task = { ...fixture, project_id: projectID };
    api.list.mockResolvedValue({
      items: [task],
      next_page_cursor: null,
      sync_cursor: "sync_first",
    });
    api.open.mockResolvedValue(task);
    const user = userEvent.setup();
    mount("?task=task_a", projectID);
    await user.click(await conversationTool("删除任务"));
    expect(screen.getByText(zh.projects.tasks.deleteConfirm)).toBeTruthy();
    expect(api.delete).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: /取\s*消/ }));
    await user.click(await conversationTool("删除任务"));
    await user.click(screen.getByRole("button", { name: "确认删除" }));
    await waitFor(() => expect(api.delete).toHaveBeenCalledWith("task_a"));
    await waitFor(() =>
      expect(screen.queryByRole("heading", { name: fixture.title })).toBeNull(),
    );
    expect(screen.queryByRole("button", { name: /第一项任务/ })).toBeNull();
  },
);

it("删除失败保留任务和对话，可重试", async () => {
  api.delete.mockRejectedValueOnce(new Error("offline"));
  const user = userEvent.setup();
  mount("?task=task_a");
  await user.click(await conversationTool("删除任务"));
  await user.click(screen.getByRole("button", { name: "确认删除" }));
  expect(await screen.findByText("offline")).toBeTruthy();
  expect(screen.getByText("已记住蓝鸟")).toBeTruthy();
  await user.click(screen.getByRole("button", { name: "确认删除" }));
  await waitFor(() => expect(api.delete).toHaveBeenCalledTimes(2));
  await waitFor(() => expect(screen.queryByText("已记住蓝鸟")).toBeNull());
});

it("删除时服务端发现任务忙，显示具体原因并保留任务和对话", async () => {
  api.delete.mockRejectedValueOnce(
    new Error('409 - {"error":{"code":"TASK_BUSY"}}'),
  );
  const user = userEvent.setup();
  mount("?task=task_a");
  await user.click(await conversationTool("删除任务"));
  await user.click(screen.getByRole("button", { name: "确认删除" }));
  expect(await screen.findByText(zh.apiErrors.TASK_BUSY)).toBeTruthy();
  expect(screen.getByText("已记住蓝鸟")).toBeTruthy();
  expect(screen.getByRole("button", { name: "确认删除" })).toBeEnabled();
});

it.each(["queued", "running"] as const)(
  "%s 任务不能删除并显示原因",
  async (status) => {
    api.open.mockResolvedValue({
      ...fixture,
      active_turn_id: "turn_a",
      status,
    });
    mount("?task=task_a");
    expect(await conversationTool("删除任务")).toBeDisabled();
    expect(await conversationTool("删除任务")).toHaveAttribute(
      "title",
      zh.projects.tasks.deleteBusy,
    );
    expect(api.delete).not.toHaveBeenCalled();
  },
);

it("删除增量后，迟到的历史分页和会话更新不能恢复列表项", async () => {
  api.list.mockResolvedValueOnce({
    items: [fixture],
    next_page_cursor: "old_page",
    sync_cursor: "sync_first",
  });
  let finishPage!: (
    value: Awaited<ReturnType<typeof projectTasksApi.list>>,
  ) => void;
  api.list.mockImplementationOnce(
    () =>
      new Promise((resolve) => {
        finishPage = resolve;
      }),
  );
  api.sync.mockResolvedValue({
    base_sync_cursor: "sync_first",
    next_sync_cursor: "sync_deleted",
    next_page_cursor: null,
    changes: [
      { operation: "delete", task_id: "task_a", list_revision: 2, data: null },
    ],
  });
  const user = userEvent.setup();
  mount("?task=task_a");
  await waitFor(() => expect(api.events).toHaveBeenCalledTimes(1));
  await user.click(screen.getByRole("button", { name: "加载更早的任务" }));
  fireEvent(document, new Event("visibilitychange"));
  await waitFor(() =>
    expect(screen.queryByRole("button", { name: /第一项任务/ })).toBeNull(),
  );
  await act(async () =>
    finishPage({ items: [fixture], next_page_cursor: null, sync_cursor: null }),
  );
  await act(async () =>
    api.events.mock.calls[0][2]({
      type: "turn",
      data: {
        task: { ...fixture, list_revision: 2 },
        turn: fixture.turns[0],
        cursor: "late",
      },
    }),
  );
  expect(screen.queryByRole("button", { name: /第一项任务/ })).toBeNull();
});

it("本轮选择模型并清空 Skill/MCP，通过消息发送而不修改项目", async () => {
  api.resources.mockResolvedValue({
    skills: [],
    mcp_servers: [],
    plugins: [],
    subagents: [],
    directory_roots: [],
    models: [
      { model_ref: "other/model", name: "Other model", supports_images: false },
    ],
    approval: {
      allowed_modes: ["ask_for_approval", "approve_for_me", "full_access"],
      reviewer_ready: true,
      execution_profile: "host_process",
    },
    default_model_ref: "glm/glm",
  });
  const user = userEvent.setup();
  mount();
  await user.click(
    screen.getByRole("button", { name: zh.projects.tasks.executionOptions }),
  );
  await user.click(await screen.findByRole("combobox", { name: "本轮模型" }));
  await user.click(await screen.findByText("Other model · other/model"));
  await turnSettings();
  await user.click(screen.getByRole("switch", { name: "自定义本轮 Skill" }));
  await user.click(screen.getByRole("switch", { name: "自定义本轮 MCP" }));
  await user.type(
    screen.getByRole("textbox", { name: "任务消息" }),
    "按本轮设置执行",
  );
  await user.click(screen.getByRole("button", { name: "发送" }));
  await waitFor(() =>
    expect(api.create).toHaveBeenCalledWith(
      "proj_a",
      expect.objectContaining({
        prompt: "按本轮设置执行",
        options: { model_ref: "other/model", skills: [], mcp_servers: [] },
      }),
    ),
  );
  expect(api.update).not.toHaveBeenCalled();
  await waitFor(() => expect(api.open).toHaveBeenCalledWith("task_a"));
  await user.type(
    screen.getByRole("textbox", { name: "任务消息" }),
    "继续使用同一模型",
  );
  await user.click(screen.getByRole("button", { name: "发送" }));
  await waitFor(() =>
    expect(api.send).toHaveBeenCalledWith(
      "task_a",
      expect.objectContaining({
        prompt: "继续使用同一模型",
        options: { model_ref: "other/model" },
      }),
    ),
  );
});

it("常驻模型选择用于重试，跟随默认和切换任务不会沿用另一任务的模型", async () => {
  api.resources.mockResolvedValue({
    skills: [],
    mcp_servers: [],
    plugins: [],
    subagents: [],
    directory_roots: [],
    approval: {
      allowed_modes: [],
      reviewer_ready: true,
      execution_profile: "host_process",
    },
    default_model_ref: "glm/glm",
    models: [
      {
        model_ref: "glm/glm",
        name: "GLM",
        provider_kind: "openai",
        supports_images: false,
      },
      {
        model_ref: "box/RM-01 VLM",
        name: "RM-01 VLM",
        provider_kind: "box",
        supports_images: false,
      },
    ],
  });
  const failed = {
    ...fixture,
    status: "failed" as const,
    turns: [{ ...fixture.turns[0], status: "failed" as const }],
  };
  const other = { ...fixture, task_id: "task_b", title: "另一任务" };
  api.list.mockResolvedValue({
    items: [failed, other],
    next_page_cursor: null,
    sync_cursor: "sync_first",
  });
  api.open.mockImplementation(async (id) => (id === "task_b" ? other : failed));
  api.retry.mockResolvedValue({ ...fixture, list_revision: 2 });
  api.send.mockResolvedValue({ ...fixture, list_revision: 3 });
  const user = userEvent.setup();
  mount("?task=task_a");
  const selector = await screen.findByRole("combobox", { name: "本轮模型" });
  expect(selector).toBeVisible();
  await user.click(selector);
  await user.click(await screen.findByText("RM-01 VLM · box/RM-01 VLM"));
  await user.click(await screen.findByRole("button", { name: "重新尝试" }));
  await waitFor(() =>
    expect(api.retry).toHaveBeenCalledWith(
      "task_a",
      "turn_a",
      expect.any(String),
      expect.objectContaining({ model_ref: "box/RM-01 VLM" }),
      { expected_configuration_revision: 1, expected_project_revision: null },
    ),
  );
  expect(selector.closest(".ant-select")).toHaveTextContent("RM-01 VLM");
  await user.click(selector);
  await user.click(await screen.findByText("跟随默认 · {{model}}"));
  await user.type(
    screen.getByRole("textbox", { name: "任务消息" }),
    "跟随默认继续",
  );
  await user.click(screen.getByRole("button", { name: "发送" }));
  await waitFor(() => expect(api.send).toHaveBeenCalledOnce());
  expect(api.send.mock.calls[0][1].options?.model_ref).toBeUndefined();
  await user.click(selector);
  await user.click(await screen.findByText("RM-01 VLM · box/RM-01 VLM"));
  await user.click(screen.getByRole("button", { name: /另一任务/ }));
  await waitFor(() => expect(api.open).toHaveBeenCalledWith("task_b"));
  expect(
    screen.getByRole("combobox", { name: "本轮模型" }).closest(".ant-select"),
  ).toHaveTextContent("跟随默认");
});

it("独立展示思考内容并合并实时更新，迟到内容不截断已收到的思考", async () => {
  const running: TaskDetail = {
    ...fixture,
    active_turn_id: "turn_a",
    turns: [
      {
        ...fixture.turns[0],
        presentation: {
          action_timeline_enabled: false,
          step_panel_enabled: true,
        },
        status: "running",
        reply: "",
        reasoning: "先分析问题。",
      },
    ],
  };
  api.open.mockResolvedValue(running);
  const user = userEvent.setup();
  mount("?task=task_a");
  await waitFor(() => expect(api.events).toHaveBeenCalledTimes(1));
  const fold = screen
    .getByText("思考过程", { selector: "summary" })
    .closest("details")!;
  expect(fold).not.toHaveAttribute("open");
  await user.click(screen.getByText("思考过程", { selector: "summary" }));
  expect(screen.getByText("先分析问题。")).toBeVisible();
  const receive = api.events.mock.calls[0][2];
  const reasoning = "先分析问题。再检查证据。";
  await act(async () =>
    receive({
      type: "turn",
      data: {
        task: running,
        turn: { ...running.turns[0], reasoning },
        cursor: "thinking-longer",
      },
    }),
  );
  expect(screen.getByText(reasoning)).toBeVisible();
  await act(async () =>
    receive({
      type: "turn",
      data: {
        task: fixture,
        turn: { ...fixture.turns[0], reasoning: "先分析问题。" },
        cursor: "thinking-done",
      },
    }),
  );
  expect(screen.getByText(reasoning)).toBeVisible();
  expect(screen.getByText("已记住蓝鸟")).toBeVisible();
  expect(fold).toHaveAttribute("open");
});

it("摘要与工具按顺序展示，稳定标识保留展开状态，迟到快照不会覆盖新内容块", async () => {
  const running: TaskDetail = {
    ...fixture,
    active_turn_id: "turn_a",
    turns: [
      {
        ...fixture.turns[0],
        presentation: {
          action_timeline_enabled: false,
          step_panel_enabled: true,
        },
        status: "running",
        reply: "",
        reasoning: "准备检查。",
        content_revision: 1,
        content_blocks: [
          {
            block_id: "summary-1",
            type: "reasoning_summary",
            text: "准备检查。",
            status: "streaming",
            tool_call_id: null,
            tool_name: null,
          },
        ],
      },
    ],
  };
  api.open.mockResolvedValue(running);
  const user = userEvent.setup();
  mount("?task=task_a");
  await waitFor(() => expect(api.events).toHaveBeenCalledTimes(1));
  await user.click(screen.getByText("思考摘要"));
  const fold = screen.getByText("思考摘要").closest("details")!;
  const blocks: TaskDetail["turns"][number]["content_blocks"] = [
    {
      ...running.turns[0].content_blocks[0],
      text: "准备检查。先核对目录。",
      status: "completed",
    },
    {
      block_id: "tool-1",
      type: "tool",
      text: "",
      status: "completed",
      tool_call_id: "read",
      tool_name: "read_file",
    },
    {
      block_id: "summary-2",
      type: "reasoning_summary",
      text: "检查已完成。",
      status: "streaming",
      tool_call_id: null,
      tool_name: null,
    },
    {
      block_id: "answer-1",
      type: "text",
      text: "正式交付。",
      status: "streaming",
      tool_call_id: null,
      tool_name: null,
    },
  ];
  const receive = api.events.mock.calls[0][2];
  await act(async () =>
    receive({
      type: "turn",
      data: {
        task: running,
        turn: {
          ...running.turns[0],
          content_revision: 4,
          content_blocks: blocks,
        },
        cursor: "blocks-newer",
      },
    }),
  );
  expect(fold).toHaveAttribute("open");
  expect(screen.getByText("准备检查。先核对目录。")).toBeVisible();
  expect(screen.getAllByText("思考摘要")).toHaveLength(2);
  expect(
    screen.queryByText("思考过程", { selector: "summary" }),
  ).not.toBeInTheDocument();
  const tool = screen.getByText(zh.projects.tasks.tool);
  expect(
    fold.compareDocumentPosition(tool) & Node.DOCUMENT_POSITION_FOLLOWING,
  ).toBeTruthy();
  expect(
    tool.compareDocumentPosition(screen.getByText("正式交付。")) &
      Node.DOCUMENT_POSITION_FOLLOWING,
  ).toBeTruthy();
  await act(async () =>
    receive({
      type: "turn",
      data: {
        task: fixture,
        turn: { ...running.turns[0], status: "completed", content_revision: 2 },
        cursor: "blocks-old-terminal",
      },
    }),
  );
  expect(screen.getByText("正式交付。")).toBeVisible();
  expect(screen.getByText("准备检查。先核对目录。")).toBeVisible();
  expect(fold).toHaveAttribute("open");
});

it("模型支持时发送本轮思考模式与强度，切换模型后清除旧选择", async () => {
  api.resources.mockResolvedValue({
    skills: [],
    mcp_servers: [],
    plugins: [],
    subagents: [],
    directory_roots: [],
    approval: {
      allowed_modes: ["ask_for_approval", "approve_for_me", "full_access"],
      reviewer_ready: true,
      execution_profile: "host_process",
    },
    default_model_ref: "glm/glm",
    models: [
      {
        model_ref: "glm/glm",
        name: "GLM",
        supports_images: false,
        reasoning_config: {
          supported: true,
          toggle: true,
          default_mode: "auto",
          default_effort: null,
          efforts: ["low", "high"],
          effort_type: "enum",
          adapter: "thinking",
        },
      },
      {
        model_ref: "plain/model",
        name: "Plain model",
        supports_images: false,
        reasoning_config: null,
      },
    ],
  });
  const user = userEvent.setup();
  mount();
  await user.click(
    screen.getByRole("button", { name: zh.projects.tasks.executionOptions }),
  );
  await turnSettings(zh.projects.composer.groups.reasoning);
  await user.click(await screen.findByRole("combobox", { name: "思考模式" }));
  await user.click(await screen.findByText("开启"));
  await user.click(screen.getByRole("combobox", { name: "思考强度" }));
  await user.click(await screen.findByTitle("high"));
  await user.type(
    screen.getByRole("textbox", { name: "任务消息" }),
    "先认真分析",
  );
  await user.click(screen.getByRole("button", { name: "发送" }));
  await waitFor(() =>
    expect(api.create).toHaveBeenCalledWith(
      "proj_a",
      expect.objectContaining({
        options: expect.objectContaining({
          reasoning_mode: "enabled",
          reasoning_effort: "high",
        }),
      }),
    ),
  );
  await turnSettings(zh.projects.composer.groups.reasoning);
  await user.click(screen.getByRole("combobox", { name: "思考模式" }));
  await user.click(await screen.findByText("开启"));
  await user.click(screen.getByRole("combobox", { name: "思考强度" }));
  await user.click(await screen.findByTitle("high"));
  await user.click(screen.getByRole("combobox", { name: "本轮模型" }));
  await user.click(await screen.findByText("Plain model · plain/model"));
  expect(
    screen.queryByRole("combobox", { name: "思考模式" }),
  ).not.toBeInTheDocument();
  await user.type(
    screen.getByRole("textbox", { name: "任务消息" }),
    "普通模型继续",
  );
  await user.click(screen.getByRole("button", { name: "发送" }));
  await waitFor(() =>
    expect(api.send).toHaveBeenCalledWith(
      "task_a",
      expect.objectContaining({
        options: expect.objectContaining({
          model_ref: "plain/model",
          reasoning_mode: undefined,
          reasoning_effort: undefined,
        }),
      }),
    ),
  );
});

it("改名期间收到新版本时使用开始编辑的版本，冲突后不覆盖远端", async () => {
  api.update.mockRejectedValue(
    new Error('409 - {"error":{"code":"TASK_REVISION_CONFLICT"}}'),
  );
  const user = userEvent.setup();
  mount("?task=task_a");
  await waitFor(() => expect(api.events).toHaveBeenCalledTimes(1));
  await user.click(screen.getByRole("button", { name: "修改任务名称" }));
  await act(async () =>
    api.events.mock.calls[0][2]({
      type: "turn",
      data: {
        task: { ...fixture, title: "另一设备修改的标题", list_revision: 2 },
        turn: fixture.turns[0],
        cursor: "renamed_elsewhere",
      },
    }),
  );
  const title = screen.getByRole("textbox", { name: "" });
  await user.clear(title);
  await user.type(title, "本机编辑的标题");
  await user.tab();
  await waitFor(() =>
    expect(api.update).toHaveBeenCalledWith("task_a", {
      title: "本机编辑的标题",
      expected_revision: 1,
    }),
  );
  expect(
    await screen.findByText("任务已发生变化，请刷新后重新修改。"),
  ).toBeInTheDocument();
  expect(
    screen.getByRole("heading", { name: /另一设备修改的标题/ }),
  ).toBeInTheDocument();
  expect(api.update).toHaveBeenCalledTimes(1);
});

it("downloads a published attachment through the authenticated API using its resource IDs", async () => {
  const artifact = {
    artifact_id: "artifact_report",
    task_id: fixture.task_id,
    turn_id: fixture.turns[0].turn_id,
    name: "报告.md",
    media_type: "text/markdown",
    byte_count: 6,
    sha256: "a".repeat(64),
    source_path: null,
    download_url:
      "/api/project-tasks/task_a/artifacts/artifact_report/download",
    created_at: fixture.created_at,
  };
  api.open.mockResolvedValue({
    ...fixture,
    turns: [{ ...fixture.turns[0], artifacts: [artifact] }],
  });
  URL.createObjectURL = vi.fn(() => "blob:attachment-report");
  URL.revokeObjectURL = vi.fn();
  const clicked: HTMLAnchorElement[] = [];
  const click = vi
    .spyOn(HTMLAnchorElement.prototype, "click")
    .mockImplementation(function () {
      clicked.push(this);
    });
  try {
    mount("?task=task_a");
    fireEvent.click(await screen.findByRole("button", { name: artifact.name }));
    await waitFor(() =>
      expect(api.downloadArtifact).toHaveBeenCalledWith(
        "task_a",
        "artifact_report",
      ),
    );
    await waitFor(() => expect(clicked).toHaveLength(1));
    expect(clicked[0].download).toBe("报告.md");
    expect(clicked[0].href).toBe("blob:attachment-report");
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:attachment-report");
  } finally {
    click.mockRestore();
  }
});

it("审批丢回执重试使用原请求；迟到正文不重开已处理卡片", async () => {
  const item = {
    interaction_id: "interaction_server",
    task_id: "task_a",
    turn_id: "turn_a",
    tool_call_id: "call_a",
    kind: "approval" as const,
    status: "pending" as const,
    revision: 1,
    payload: {
      tool_name: "execute",
      arguments: { command: "example" },
      action_digest: "fixed-digest",
      allowed_decisions: ["allow_once", "deny"] as ("allow_once" | "deny")[],
    },
    resolution: null,
    created_at: fixture.created_at,
    expires_at: fixture.created_at,
    resolved_at: null,
  };
  const waiting: TaskDetail = {
    ...fixture,
    status: "waiting_approval",
    active_turn_id: "turn_a",
    turns: [
      {
        ...fixture.turns[0],
        status: "waiting_approval",
        interactions: [item],
        interaction_revision: 1,
      },
    ],
  };
  api.open.mockResolvedValue(waiting);
  api.respond.mockRejectedValueOnce(new Error("Lost receipt"));
  api.respond.mockResolvedValueOnce({
    ...waiting,
    turns: [
      {
        ...waiting.turns[0],
        interaction_revision: 2,
        interactions: [
          {
            ...item,
            status: "resolved",
            revision: 2,
            resolution: { outcome: "allow_once", source: "user", answer: null },
          },
        ],
      },
    ],
  });
  const user = userEvent.setup();
  mount("?task=task_a");
  await user.click(
    await screen.findByRole("button", { name: zh.projects.tasks.allowOnce }),
  );
  await waitFor(() => expect(api.respond).toHaveBeenCalledTimes(1));
  await user.click(
    screen.getByRole("button", { name: zh.projects.tasks.allowOnce }),
  );
  await waitFor(() => expect(api.respond).toHaveBeenCalledTimes(2));
  expect(api.respond.mock.calls[0]).toEqual(api.respond.mock.calls[1]);
  expect(api.respond.mock.calls[0][3]).toMatchObject({
    expected_revision: 1,
    response: { action_digest: "fixed-digest", decision: "allow_once" },
  });
  await waitFor(() =>
    expect(
      screen.queryByRole("button", { name: zh.projects.tasks.allowOnce }),
    ).not.toBeInTheDocument(),
  );
  const receive = api.events.mock.calls[0][2];
  await act(async () =>
    receive({
      type: "turn",
      data: {
        task: waiting,
        turn: { ...waiting.turns[0], reply: "Longer old reply" },
        cursor: "old",
      },
    }),
  );
  expect(
    screen.queryByRole("button", { name: zh.projects.tasks.allowOnce }),
  ).not.toBeInTheDocument();
  expect(screen.getByText("Longer old reply")).toBeVisible();
});

it("暂停与继续绑定原轮次，暂停意图确认后显示继续", async () => {
  const running: TaskDetail = {
    ...fixture,
    active_turn_id: "turn_a",
    status: "running",
    turns: [{ ...fixture.turns[0], status: "running" }],
  };
  api.open.mockResolvedValue(running);
  api.control.mockResolvedValueOnce({
    ...running,
    turns: [
      { ...running.turns[0], pause_requested: true, interaction_revision: 1 },
    ],
  });
  api.control.mockResolvedValueOnce({
    ...running,
    turns: [{ ...running.turns[0], interaction_revision: 2 }],
  });
  const user = userEvent.setup();
  mount("?task=task_a");
  await user.click(
    await screen.findByRole("button", { name: zh.projects.tasks.pause }),
  );
  expect(api.control.mock.calls[0].slice(0, 3)).toEqual([
    "task_a",
    "turn_a",
    true,
  ]);
  await user.click(
    await screen.findByRole("button", { name: zh.projects.tasks.resume }),
  );
  expect(api.control.mock.calls[1].slice(0, 3)).toEqual([
    "task_a",
    "turn_a",
    false,
  ]);
});

it.each(["paused", "interrupted"] as const)(
  "%s 原轮恢复保留冻结目录，不被新轮目录确认阻断",
  async (status) => {
    const paused: TaskDetail = {
      ...fixture,
      status,
      active_turn_id: "turn_a",
      turns: [
        {
          ...fixture.turns[0],
          status,
          server_directory: projectFixture.directory,
          client_directory: projectFixture.client_directory,
          recovery:
            status === "interrupted"
              ? {
                  can_resume: true,
                  needs_reconciliation: false,
                  phase: "mcp_connect",
                  reason_code: "TASK_RESOURCE_UNAVAILABLE",
                  cause_type: "ConnectionError",
                  diagnostic_id: "directory-recovery",
                  resource_type: "mcp",
                  resource_id: "service",
                  status_code: 503,
                }
              : undefined,
        },
      ],
    };
    api.open.mockResolvedValue(paused);
    api.control.mockResolvedValue({
      ...paused,
      status: "running",
      turns: [{ ...paused.turns[0], status: "running" }],
    });
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={["/box-projects/proj_a?task=task_a"]}>
        <TaskConversations
          projectID="proj_a"
          project={{
            ...projectFixture,
            directory: "/server/after",
            revision: 2,
          }}
        />
      </MemoryRouter>,
    );
    await screen.findByText(zh.projects.tasks.directoryChanged);
    const resume = screen.getByRole("button", {
      name:
        status === "interrupted"
          ? zh.projects.recovery.resume
          : zh.projects.tasks.resume,
    });
    expect(resume).toBeEnabled();
    await user.click(resume);
    await waitFor(() =>
      expect(api.control).toHaveBeenCalledWith(
        "task_a",
        "turn_a",
        false,
        expect.any(String),
      ),
    );
    expect(api.retry).not.toHaveBeenCalled();
    expect(api.send).not.toHaveBeenCalled();
  },
);

it("新任务默认开启网络搜索并保存到创建顶层字段，后续消息沿用任务配置", async () => {
  const enabled = { ...fixture, web_search_enabled: true };
  api.create.mockResolvedValue(enabled);
  api.open.mockResolvedValue(enabled);
  api.send.mockResolvedValue(enabled);
  const user = userEvent.setup();
  mount();
  const toggle = await screen.findByRole("switch", { name: "网络搜索" });
  expect(toggle).toBeChecked();
  await user.type(
    screen.getByRole("textbox", { name: "任务消息" }),
    "搜索资料",
  );
  await user.click(screen.getByRole("button", { name: "发送" }));
  await waitFor(() =>
    expect(api.create).toHaveBeenCalledWith(
      "proj_a",
      expect.objectContaining({ web_search_enabled: true }),
    ),
  );
  expect(api.create.mock.calls[0][1].options).toBeUndefined();
  await waitFor(() =>
    expect(screen.getByRole("switch", { name: "网络搜索" })).toBeChecked(),
  );
  await user.type(
    screen.getByRole("textbox", { name: "任务消息" }),
    "继续搜索",
  );
  await user.click(screen.getByRole("button", { name: "发送" }));
  await waitFor(() => expect(api.send).toHaveBeenCalledTimes(1));
  expect(api.send.mock.calls[0][1]).not.toHaveProperty("web_search_enabled");
  expect(api.send.mock.calls[0][1].options).toBeUndefined();
});

it("主动关闭搜索后创建和追问保留关闭状态，切回新任务恢复默认开启", async () => {
  const user = userEvent.setup();
  mount();
  const toggle = await screen.findByRole("switch", { name: "网络搜索" });
  expect(toggle).toBeChecked();
  await user.click(toggle);
  expect(toggle).not.toBeChecked();
  await user.type(
    screen.getByRole("textbox", { name: "任务消息" }),
    "只看本地资料",
  );
  await user.click(screen.getByRole("button", { name: "发送" }));
  await waitFor(() =>
    expect(api.create).toHaveBeenCalledWith(
      "proj_a",
      expect.objectContaining({ web_search_enabled: false }),
    ),
  );
  await waitFor(() => expect(api.open).toHaveBeenCalledWith("task_a"));
  await waitFor(() =>
    expect(screen.getByRole("switch", { name: "网络搜索" })).not.toBeChecked(),
  );
  await user.type(
    screen.getByRole("textbox", { name: "任务消息" }),
    "继续本地分析",
  );
  await user.click(screen.getByRole("button", { name: "发送" }));
  await waitFor(() => expect(api.send).toHaveBeenCalledTimes(1));
  expect(api.send.mock.calls[0][1]).not.toHaveProperty("web_search_enabled");
  expect(api.send.mock.calls[0][1].options).toBeUndefined();
  expect(screen.getByRole("switch", { name: "网络搜索" })).not.toBeChecked();
  await user.click(screen.getByRole("button", { name: "新任务" }));
  await waitFor(() =>
    expect(screen.getByRole("switch", { name: "网络搜索" })).toBeChecked(),
  );
  expect(api.update).not.toHaveBeenCalled();
});

it("任务运行中可保存网络搜索开关，切换任务和重新打开时恢复各自配置", async () => {
  const running: TaskDetail = {
    ...fixture,
    active_turn_id: "turn_a",
    status: "running",
    turns: [{ ...fixture.turns[0], status: "running" }],
  };
  const enabled = { ...running, list_revision: 2, web_search_enabled: true };
  const other = { ...fixture, task_id: "task_b", title: "另一任务" };
  api.list.mockResolvedValue({
    items: [running, other],
    next_page_cursor: null,
    sync_cursor: "sync_first",
  });
  api.open
    .mockResolvedValueOnce(running)
    .mockResolvedValueOnce(other)
    .mockResolvedValueOnce(enabled);
  api.update.mockResolvedValue(enabled);
  const user = userEvent.setup();
  mount("?task=task_a");
  const toggle = await screen.findByRole("switch", { name: "网络搜索" });
  await waitFor(() => expect(toggle).toBeEnabled());
  await user.click(toggle);
  await waitFor(() =>
    expect(api.update).toHaveBeenCalledWith("task_a", {
      expected_revision: 1,
      web_search_enabled: true,
    }),
  );
  await waitFor(() => expect(toggle).toBeChecked());
  expect(api.send).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: /另一任务/ }));
  await waitFor(() => expect(api.open).toHaveBeenCalledWith("task_b"));
  await waitFor(() =>
    expect(screen.getByRole("switch", { name: "网络搜索" })).not.toBeChecked(),
  );
  await user.click(screen.getByRole("button", { name: /第一项任务/ }));
  await waitFor(() => expect(api.open).toHaveBeenCalledTimes(3));
  await waitFor(() =>
    expect(screen.getByRole("switch", { name: "网络搜索" })).toBeChecked(),
  );
});

it("网络搜索保存期间阻止发送和重试，冲突后保留实际值并支持刷新", async () => {
  const failed: TaskDetail = {
    ...fixture,
    turns: [{ ...fixture.turns[0], status: "failed" }],
  };
  api.open.mockResolvedValue(failed);
  let reject!: (error: Error) => void;
  api.update.mockReturnValueOnce(
    new Promise((_resolve, no) => {
      reject = no;
    }),
  );
  const user = userEvent.setup();
  mount("?task=task_a");
  const toggle = await screen.findByRole("switch", { name: "网络搜索" });
  await waitFor(() => expect(toggle).toBeEnabled());
  await user.type(screen.getByRole("textbox", { name: "任务消息" }), "继续");
  await user.click(toggle);
  expect(screen.getByRole("button", { name: "发送" })).toBeDisabled();
  expect(
    screen.getByRole("button", { name: zh.projects.tasks.retry }),
  ).toBeDisabled();
  await user.type(screen.getByRole("textbox", { name: "任务消息" }), "{Enter}");
  expect(api.send).not.toHaveBeenCalled();
  await act(async () =>
    reject(new Error('409 - {"error":{"code":"TASK_REVISION_CONFLICT"}}')),
  );
  await screen.findByText("任务已发生变化，请刷新后重新修改。");
  expect(toggle).not.toBeChecked();
  expect(toggle).toBeDisabled();
  api.get.mockResolvedValue({
    ...failed,
    list_revision: 2,
    web_search_enabled: true,
  });
  await user.click(
    screen.getByRole("button", {
      name: zh.projects.tasks.approvalMode.refresh,
    }),
  );
  await waitFor(() => expect(toggle).toBeChecked());
  expect(toggle).toBeEnabled();
  expect(api.update).toHaveBeenCalledTimes(1);
});

it("网络搜索保存中切到另一任务再返回，旧请求完成前继续冻结该任务", async () => {
  const enabled = { ...fixture, list_revision: 2, web_search_enabled: true };
  const other = { ...fixture, task_id: "task_b", title: "另一任务" };
  api.list.mockResolvedValue({
    items: [fixture, other],
    next_page_cursor: null,
    sync_cursor: "sync_first",
  });
  api.open
    .mockResolvedValueOnce(fixture)
    .mockResolvedValueOnce(other)
    .mockResolvedValueOnce(enabled);
  let finish!: (value: TaskDetail) => void;
  api.update.mockReturnValueOnce(
    new Promise((resolve) => {
      finish = resolve;
    }),
  );
  const user = userEvent.setup();
  mount("?task=task_a");
  const toggle = await screen.findByRole("switch", { name: "网络搜索" });
  await waitFor(() => expect(toggle).toBeEnabled());
  await user.click(toggle);
  await user.click(screen.getByRole("button", { name: /另一任务/ }));
  await waitFor(() => expect(api.open).toHaveBeenCalledWith("task_b"));
  await waitFor(() =>
    expect(screen.getByRole("switch", { name: "网络搜索" })).toBeEnabled(),
  );
  await user.click(screen.getByRole("button", { name: /第一项任务/ }));
  await waitFor(() => expect(api.open).toHaveBeenCalledTimes(3));
  const restored = screen.getByRole("switch", { name: "网络搜索" });
  expect(restored).toBeChecked();
  expect(restored).toBeDisabled();
  await user.type(
    screen.getByRole("textbox", { name: "任务消息" }),
    "保存后继续",
  );
  expect(screen.getByRole("button", { name: "发送" })).toBeDisabled();
  await user.type(screen.getByRole("textbox", { name: "任务消息" }), "{Enter}");
  expect(api.send).not.toHaveBeenCalled();
  expect(api.update).toHaveBeenCalledTimes(1);
  await act(async () => finish(enabled));
  await waitFor(() => expect(restored).toBeEnabled());
  expect(restored).toBeChecked();
  expect(screen.getByRole("button", { name: "发送" })).toBeEnabled();
});

it("网络搜索保存失败时重试同一版本，迟到的旧事件不能覆盖配置", async () => {
  const enabled = { ...fixture, list_revision: 2, web_search_enabled: true };
  api.update
    .mockRejectedValueOnce(new Error("Lost search receipt"))
    .mockResolvedValueOnce(enabled);
  const user = userEvent.setup();
  mount("?task=task_a");
  const toggle = await screen.findByRole("switch", { name: "网络搜索" });
  await waitFor(() => expect(toggle).toBeEnabled());
  await user.click(toggle);
  await screen.findByText("Lost search receipt");
  await user.click(
    screen.getByRole("button", { name: zh.projects.tasks.approvalMode.retry }),
  );
  await waitFor(() => expect(toggle).toBeChecked());
  expect(api.update).toHaveBeenCalledTimes(2);
  expect(api.update.mock.calls[0]).toEqual(api.update.mock.calls[1]);
  await act(async () =>
    api.events.mock.calls[0][2]({
      type: "turn",
      data: { task: fixture, turn: fixture.turns[0], cursor: "old-search" },
    }),
  );
  expect(toggle).toBeChecked();
});

it("任务模式切换丢回执后显式重试原请求，确认前保留远端模式", async () => {
  const approved: TaskDetail = {
    ...fixture,
    list_revision: 2,
    approval_policy: {
      ...fixture.approval_policy,
      mode: "approve_for_me",
      revision: 2,
    },
  };
  api.updateApprovalPolicy.mockRejectedValueOnce(
    new Error("Lost mode receipt"),
  );
  api.updateApprovalPolicy.mockResolvedValueOnce(approved.approval_policy);
  const user = userEvent.setup();
  mount("?task=task_a");
  const selector = await approvalSelector();
  await waitFor(() => expect(selector).toBeEnabled());
  await user.click(selector);
  await user.click(
    await screen.findByText(zh.projects.tasks.approvalMode.approve_for_me),
  );
  await screen.findByText("Lost mode receipt");
  expect(selector.closest(".ant-select")).toHaveTextContent(
    zh.projects.tasks.approvalMode.ask_for_approval,
  );
  expect(selector).toBeDisabled();
  await user.click(
    screen.getByRole("button", { name: zh.projects.tasks.approvalMode.retry }),
  );
  await waitFor(() =>
    expect(api.updateApprovalPolicy).toHaveBeenCalledTimes(2),
  );
  expect(api.updateApprovalPolicy.mock.calls[0]).toEqual(
    api.updateApprovalPolicy.mock.calls[1],
  );
  expect(api.updateApprovalPolicy.mock.calls[0][0]).toMatchObject({
    expected_revision: 1,
    mode: "approve_for_me",
    request_id: expect.any(String),
  });
  await waitFor(() =>
    expect(selector.closest(".ant-select")).toHaveTextContent(
      zh.projects.tasks.approvalMode.approve_for_me,
    ),
  );
  await act(async () =>
    api.events.mock.calls[0][2]({
      type: "turn",
      data: {
        task: { ...fixture, list_revision: 3 },
        turn: fixture.turns[0],
        cursor: "old-mode-new-body",
      },
    }),
  );
  expect(selector.closest(".ant-select")).toHaveTextContent(
    zh.projects.tasks.approvalMode.approve_for_me,
  );
});

it("新任务完全访问先确认真实范围，全局保存后创建继承而不发送任务模式", async () => {
  const user = userEvent.setup();
  mount();
  const selector = await approvalSelector();
  await waitFor(() => expect(selector).toBeEnabled());
  await user.click(selector);
  await user.click(
    await screen.findByText(zh.projects.tasks.approvalMode.full_access),
  );
  const dialog = await screen.findByRole("dialog");
  expect(dialog).toHaveTextContent(zh.projects.tasks.approvalMode.hostScope);
  expect(selector.closest(".ant-select")).toHaveTextContent(
    zh.projects.tasks.approvalMode.ask_for_approval,
  );
  expect(api.setApproval).not.toHaveBeenCalled();
  await user.click(
    screen.getByRole("button", {
      name: zh.projects.tasks.approvalMode.confirm,
    }),
  );
  await user.type(
    screen.getByRole("textbox", { name: "任务消息" }),
    "检查服务端项目",
  );
  await user.click(screen.getByRole("button", { name: "发送" }));
  await waitFor(() =>
    expect(api.create).toHaveBeenCalledWith(
      "proj_a",
      expect.objectContaining({ prompt: "检查服务端项目" }),
    ),
  );
  expect(api.create.mock.calls[0][1]).not.toHaveProperty("approval_mode");
  expect(api.create.mock.calls[0][1].options).toBeUndefined();
  expect(api.updateApprovalPolicy).toHaveBeenCalledWith(
    expect.objectContaining({ mode: "full_access", expected_revision: 1 }),
  );
});

it("自动审查中没有人工批准按钮，自动结果仅留折叠记录", async () => {
  const item = {
    interaction_id: "review_a",
    task_id: "task_a",
    turn_id: "turn_a",
    tool_call_id: "call_a",
    kind: "approval" as const,
    status: "reviewing" as const,
    revision: 1,
    payload: {
      tool_name: "execute",
      action_digest: "digest",
      approval_mode: "approve_for_me" as const,
      approval_revision: 2,
      allowed_decisions: ["allow_once", "deny"] as ("allow_once" | "deny")[],
    },
    resolution: null,
    created_at: fixture.created_at,
    expires_at: fixture.created_at,
    resolved_at: null,
  };
  const reviewing: TaskDetail = {
    ...fixture,
    status: "running",
    active_turn_id: "turn_a",
    turns: [
      {
        ...fixture.turns[0],
        status: "running",
        interactions: [item],
        interaction_revision: 1,
      },
    ],
  };
  api.open.mockResolvedValue(reviewing);
  mount("?task=task_a");
  await screen.findByText(zh.projects.tasks.approvalMode.reviewing);
  expect(
    screen.queryByRole("button", { name: zh.projects.tasks.allowOnce }),
  ).not.toBeInTheDocument();
  expect(
    screen.queryByText(zh.projects.tasks.approvalTitle),
  ).not.toBeInTheDocument();
  await act(async () =>
    api.events.mock.calls[0][2]({
      type: "turn",
      data: {
        task: reviewing,
        turn: {
          ...reviewing.turns[0],
          interaction_revision: 2,
          interactions: [
            {
              ...item,
              status: "resolved",
              revision: 2,
              resolution: {
                source: "auto_reviewer",
                outcome: "allow_once",
                answer: null,
              },
            },
          ],
        },
        cursor: "review-finished",
      },
    }),
  );
  const summary = screen.getByText(/AI 审批记录/).closest("details")!;
  expect(summary).not.toHaveAttribute("open");
  expect(
    screen.queryByText(zh.projects.tasks.approvalMode.reviewing),
  ).not.toBeInTheDocument();
  expect(api.respond).not.toHaveBeenCalled();
});

it.each([
  {
    allowed_modes: ["ask_for_approval"] as const,
    reviewer_ready: true,
  },
  {
    allowed_modes: ["ask_for_approval", "approve_for_me"] as const,
    reviewer_ready: false,
  },
])(
  "三种审批模式始终可选，审查就绪状态为 $reviewer_ready",
  async ({ allowed_modes, reviewer_ready }) => {
    api.resources.mockResolvedValue({
      skills: [],
      mcp_servers: [],
      plugins: [],
      subagents: [],
      directory_roots: [],
      models: [],
      default_model_ref: null,
      approval: {
        allowed_modes: [...allowed_modes],
        reviewer_ready,
        execution_profile: "host_process",
      },
    });
    const user = userEvent.setup();
    mount();
    const selector = await approvalSelector();
    await waitFor(() => expect(selector).toBeEnabled());
    await user.click(selector);
    for (const mode of [
      "ask_for_approval",
      "approve_for_me",
      "full_access",
    ] as const) {
      const option = screen
        .getAllByText(zh.projects.tasks.approvalMode[mode])
        .find((element) => element.closest(".ant-select-item-option"));
      expect(option).toBeDefined();
      expect(option?.closest(".ant-select-item-option")).not.toHaveClass(
        "ant-select-item-option-disabled",
      );
    }
    const option = screen.getByText(
      zh.projects.tasks.approvalMode.approve_for_me,
    );
    await user.click(option);
    expect(api.setApproval).not.toHaveBeenCalled();
    await waitFor(() =>
      expect(selector.closest(".ant-select")).toHaveTextContent(
        zh.projects.tasks.approvalMode.approve_for_me,
      ),
    );
    expect(api.updateApprovalPolicy).toHaveBeenCalledWith(
      expect.objectContaining({ mode: "approve_for_me" }),
    );
  },
);

it("Reviewer 转人工时在原卡显示稳定原因并保留人工决定入口", async () => {
  const item = {
    interaction_id: "review_manual",
    task_id: "task_a",
    turn_id: "turn_a",
    tool_call_id: "call_a",
    kind: "approval" as const,
    status: "pending" as const,
    revision: 2,
    payload: {
      tool_name: "execute",
      action_digest: "digest",
      allowed_decisions: ["allow_once", "deny"] as ("allow_once" | "deny")[],
    },
    resolution: null,
    created_at: fixture.created_at,
    expires_at: fixture.created_at,
    resolved_at: null,
    review: {
      review_id: "review_work",
      status: "completed" as const,
      decision: "requires_user" as const,
      reason_code: "review_timeout",
      action_digest: "digest",
      context_digest: null,
      model_ref: "glm/glm",
      profile_version: 1 as const,
      created_at: fixture.created_at,
      updated_at: fixture.created_at,
      expires_at: fixture.created_at,
    },
  };
  api.open.mockResolvedValue({
    ...fixture,
    status: "waiting_approval",
    active_turn_id: "turn_a",
    turns: [
      {
        ...fixture.turns[0],
        status: "waiting_approval",
        interactions: [item],
        interaction_revision: 2,
      },
    ],
  });
  mount("?task=task_a");
  await screen.findByText(
    zh.projects.tasks.approvalMode.reviewReason.review_timeout,
  );
  expect(
    screen.getByRole("button", { name: zh.projects.tasks.allowOnce }),
  ).toBeEnabled();
  expect(
    screen.getByRole("button", { name: /^\u62d2\s*\u7edd$/ }),
  ).toBeEnabled();
});

it("历史回合使用各轮展示快照，恢复焦点和全局开关变化不改变历史展示", async () => {
  const history: TaskDetail = {
    ...fixture,
    turns: [1, 2, 3].map((sequence) => ({
      ...fixture.turns[0],
      turn_id: `turn_${sequence}`,
      sequence,
      presentation: {
        action_timeline_enabled: sequence !== 3,
        step_panel_enabled: sequence === 1,
      },
      reply: `第 ${sequence} 轮答复`,
      plan: {
        source: "octop_planning" as const,
        plan_id: `plan_${sequence}`,
        title: `第 ${sequence} 轮计划`,
        revision: 1,
        steps: [
          {
            step_id: `step_${sequence}`,
            content: "检查",
            status: "completed" as const,
            result: "检查完成",
          },
        ],
      },
      content_blocks: [
        {
          block_id: `progress_${sequence}`,
          type: "progress" as const,
          text: `第 ${sequence} 轮公开进展`,
          status: "completed" as const,
          tool_call_id: null,
          tool_name: null,
        },
      ],
    })),
  };
  api.open.mockResolvedValue(history);
  api.get.mockResolvedValue(history);
  mount("?task=task_a");
  await screen.findByText("第 1 轮计划");
  expect(screen.queryByText("第 2 轮计划")).not.toBeInTheDocument();
  expect(screen.queryByText("第 3 轮计划")).not.toBeInTheDocument();
  expect(screen.getByRole("region", { name: "动作链路" })).toHaveTextContent(
    "第 2 轮公开进展",
  );
  expect(screen.getByRole("region", { name: "思考过程" })).toHaveTextContent(
    "第 3 轮公开进展",
  );
  expect(api.settings).not.toHaveBeenCalled();
  api.settings.mockResolvedValue({
    action_timeline_enabled: false,
    step_panel_enabled: false,
  });
  await act(async () => window.dispatchEvent(new Event("focus")));
  expect(screen.getByText("第 1 轮计划")).toBeVisible();
  expect(screen.queryByText("第 1 轮公开进展")).not.toBeInTheDocument();
  expect(screen.getByText("第 2 轮公开进展")).toBeVisible();
  expect(screen.getByText("第 3 轮公开进展")).toBeVisible();
  expect(screen.getAllByRole("region", { name: "动作链路" })).toHaveLength(1);
  expect(screen.getAllByRole("region", { name: "思考过程" })).toHaveLength(1);
  expect(api.settings).not.toHaveBeenCalled();
});

it("账号审批在任务之间共享，迟到任务摘要和低版本推送不覆盖已确认模式", async () => {
  const other = { ...fixture, task_id: "task_b", title: "另一任务" };
  api.list.mockResolvedValue({
    items: [fixture, other],
    next_page_cursor: null,
    sync_cursor: "sync_first",
  });
  api.open.mockImplementation(async (id) =>
    id === "task_b" ? other : fixture,
  );
  const user = userEvent.setup();
  mount("?task=task_a");
  const selector = await approvalSelector();
  await waitFor(() => expect(selector).toBeEnabled());
  await user.click(selector);
  await user.click(
    await screen.findByText(zh.projects.tasks.approvalMode.approve_for_me),
  );
  await waitFor(() =>
    expect(selector.closest(".ant-select")).toHaveTextContent(
      zh.projects.tasks.approvalMode.approve_for_me,
    ),
  );
  await user.click(screen.getByRole("button", { name: /另一任务/ }));
  await waitFor(() => expect(api.open).toHaveBeenCalledWith("task_b"));
  const otherSelector = await approvalSelector();
  expect(otherSelector.closest(".ant-select")).toHaveTextContent(
    zh.projects.tasks.approvalMode.approve_for_me,
  );
  const push = api.approvalEvents.mock.calls[0][0];
  await act(async () =>
    push({ ...fixture.approval_policy, mode: "full_access", revision: 3 }),
  );
  await act(async () => push(fixture.approval_policy));
  expect(otherSelector.closest(".ant-select")).toHaveTextContent(
    zh.projects.tasks.approvalMode.full_access,
  );
  expect(api.updateApprovalPolicy).toHaveBeenCalledTimes(1);
  expect(api.respond).not.toHaveBeenCalled();
  expect(fixture.turns[0].approval_policy_at_start.mode).toBe(
    "ask_for_approval",
  );
});

it("审批未知回执在只读刷新后仍保留可见重试，确认较新全局版本后不重发旧意图", async () => {
  api.updateApprovalPolicy.mockRejectedValueOnce(
    new Error("Unknown global receipt"),
  );
  const user = userEvent.setup();
  mount();
  const selector = await approvalSelector();
  await waitFor(() => expect(selector).toBeEnabled());
  await user.click(selector);
  await user.click(
    await screen.findByText(zh.projects.tasks.approvalMode.approve_for_me),
  );
  await screen.findByText("Unknown global receipt");
  await user.click(
    screen.getByRole("button", {
      name: zh.projects.tasks.approvalMode.refresh,
    }),
  );
  expect(
    await screen.findByRole("button", {
      name: zh.projects.tasks.approvalMode.retry,
    }),
  ).toBeEnabled();
  expect(selector).toBeDisabled();
  api.approvalPolicy.mockResolvedValue({
    ...fixture.approval_policy,
    mode: "approve_for_me",
    revision: 2,
  });
  await user.click(
    screen.getByRole("button", { name: zh.projects.tasks.approvalMode.retry }),
  );
  await waitFor(() => expect(selector).toBeEnabled());
  expect(selector.closest(".ant-select")).toHaveTextContent(
    zh.projects.tasks.approvalMode.approve_for_me,
  );
  expect(api.updateApprovalPolicy).toHaveBeenCalledTimes(1);
});

it("新任务接收独立账号推送，无任务正文和旧资源门禁也能同步审批模式", async () => {
  api.resources.mockRejectedValue(new Error("Legacy resources unavailable"));
  mount();
  const selector = await approvalSelector();
  await waitFor(() => expect(selector).toBeEnabled());
  await act(async () =>
    api.approvalEvents.mock.calls[0][0]({
      ...fixture.approval_policy,
      mode: "approve_for_me",
      revision: 5,
    }),
  );
  expect(selector.closest(".ant-select")).toHaveTextContent(
    zh.projects.tasks.approvalMode.approve_for_me,
  );
  expect(api.updateApprovalPolicy).not.toHaveBeenCalled();
  expect(api.get).not.toHaveBeenCalled();
});

const octopEngine = {
  engine_id: "octop",
  connection_id: null,
  model_id: null,
} as const;
const codexEngine = {
  engine_id: "acp",
  connection_id: "codex",
  model_id: null,
} as const;
const nativeEngineCapabilities = {
  action_modes: ["ask", "plan", "craft"],
  images: true,
  permissions: true,
  native_resume: true,
  pause: true,
  cancel: true,
  skills: true,
  mcp: true,
  plugins: true,
  subagents: true,
  knowledge: true,
  memory: true,
  compact: true,
  model_selection: true,
} as import("../../api/modules/projects").EngineCapabilities;
const acpEngineCapabilities = {
  ...nativeEngineCapabilities,
  action_modes: ["craft"],
  images: false,
  native_resume: false,
  pause: false,
  plugins: false,
  subagents: false,
  knowledge: false,
  compact: false,
  model_selection: false,
} as import("../../api/modules/projects").EngineCapabilities;
async function withEngineResources() {
  const resources = {
    ...(await api.resources()),
    execution_engines: [
      {
        engine: octopEngine,
        label: "Octop",
        available: true,
        unavailable_reason: null,
        capabilities: nativeEngineCapabilities,
        models: [],
      },
      {
        engine: codexEngine,
        label: "Codex",
        available: true,
        unavailable_reason: null,
        capabilities: acpEngineCapabilities,
        models: [],
      },
    ],
  };
  api.resources.mockResolvedValue(resources);
  return resources;
}
async function chooseEngine(label = "Codex") {
  await userEvent.click(
    await screen.findByRole("combobox", { name: zh.projects.tasks.engine }),
  );
  await userEvent.click(
    await screen.findByText(label, {
      selector: ".ant-select-item-option-content",
    }),
  );
}

describe("independent Task execution engines", () => {
  it("creates with independent expert and engine choices without Octop model routing in ACP options", async () => {
    await withEngineResources();
    mount("", null);
    await userEvent.click(
      await screen.findByRole("combobox", { name: zh.projects.tasks.expert }),
    );
    await userEvent.click(
      await screen.findByText("Finance expert", {
        selector: ".ant-select-item-option-content",
      }),
    );
    await userEvent.click(
      screen.getByRole("combobox", { name: zh.projects.tasks.model }),
    );
    await userEvent.click(
      await screen.findByText("GLM · glm/glm", {
        selector: ".ant-select-item-option-content",
      }),
    );
    await chooseEngine();
    expect(
      screen.queryByRole("combobox", { name: zh.projects.tasks.model }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByText(zh.projects.tasks.engineDefaultModel),
    ).toBeInTheDocument();
    await userEvent.type(
      screen.getByRole("textbox", { name: zh.projects.tasks.message }),
      "Use my expert with Codex",
    );
    await userEvent.click(
      screen.getByRole("button", { name: zh.projects.tasks.send }),
    );
    await waitFor(() =>
      expect(api.create).toHaveBeenCalledWith(
        null,
        expect.objectContaining({
          selected_expert_id: "expert_a",
          engine: codexEngine,
          action_mode: "craft",
        }),
      ),
    );
    expect(api.create.mock.calls[0][1].options?.model_ref).toBeUndefined();
    expect(
      api.create.mock.calls[0][1].options?.reasoning_effort,
    ).toBeUndefined();
    expect(api.inspectEngine).not.toHaveBeenCalled();
  });

  it("switches only the next turn’s engine on the same Task and preserves expert, draft and historic identity", async () => {
    await withEngineResources();
    const detail = {
      ...fixture,
      engine: octopEngine,
      selected_expert_id: "expert_a",
      action_mode: "ask" as const,
      configuration_revision: 5,
      turns: [
        {
          ...fixture.turns[0],
          expert_name: "Original expert",
          execution_engine: {
            ...octopEngine,
            label: "Original Octop engine",
            capabilities: nativeEngineCapabilities,
          },
        },
      ],
    };
    api.open.mockResolvedValue(detail);
    api.update.mockImplementation(async (_id, changes) => ({
      ...detail,
      ...changes,
      action_mode: "craft",
      list_revision: 2,
      configuration_revision: 6,
    }));
    mount("?task=task_a", "proj_a");
    const input = await screen.findByRole("textbox", {
      name: zh.projects.tasks.message,
    });
    await waitFor(() => expect(input).toBeEnabled());
    await userEvent.type(input, "Continue in the same workspace");
    await chooseEngine();
    await waitFor(() =>
      expect(api.update).toHaveBeenCalledWith("task_a", {
        engine: codexEngine,
        action_mode: "craft",
        expected_revision: 1,
        expected_configuration_revision: 5,
      }),
    );
    expect(input).toHaveValue("Continue in the same workspace");
    expect(screen.getByText("Original expert")).toBeInTheDocument();
    expect(screen.getByText("Original Octop engine")).toBeInTheDocument();
    await userEvent.click(
      screen.getByRole("button", { name: zh.projects.tasks.send }),
    );
    await waitFor(() =>
      expect(api.send).toHaveBeenCalledWith(
        "task_a",
        expect.objectContaining({
          expected_configuration_revision: 6,
          expected_project_revision: null,
        }),
      ),
    );
    expect(api.create).not.toHaveBeenCalled();
    expect(api.update.mock.calls[0][1]).not.toHaveProperty(
      "selected_expert_id",
    );
    expect(api.update.mock.calls[0][1]).not.toHaveProperty("project_id");
  });

  it("keeps engine selection locked during an ACP turn and offers cancellation without pause", async () => {
    await withEngineResources();
    api.open.mockResolvedValue({
      ...fixture,
      engine: codexEngine,
      status: "running",
      active_turn_id: "turn_a",
      turns: [
        {
          ...fixture.turns[0],
          status: "running",
          execution_engine: {
            ...codexEngine,
            label: "Codex",
            capabilities: acpEngineCapabilities,
          },
        },
      ],
    });
    mount("?task=task_a");
    const selector = await screen.findByRole("combobox", {
      name: zh.projects.tasks.engine,
    });
    await waitFor(() => expect(selector).toBeDisabled());
    expect(
      screen.queryByRole("button", { name: zh.projects.tasks.pause }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: zh.projects.tasks.stop }),
    ).toBeEnabled();
  });

  it("preserves an unavailable selected engine instead of silently switching back to Octop", async () => {
    const resources = await withEngineResources();
    resources.execution_engines[1].available = false;
    resources.execution_engines[1].unavailable_reason = "auth_required";
    api.open.mockResolvedValue({ ...fixture, engine: codexEngine });
    mount("?task=task_a");
    await screen.findByRole("combobox", { name: zh.projects.tasks.engine });
    expect(
      (await screen.findAllByText(zh.projects.tasks.engineUnavailable)).length,
    ).toBeGreaterThan(0);
    await userEvent.type(
      screen.getByRole("textbox", { name: zh.projects.tasks.message }),
      "Keep my selection",
    );
    expect(
      screen.getByRole("button", { name: zh.projects.tasks.send }),
    ).toBeDisabled();
    expect(api.update).not.toHaveBeenCalled();
  });

  it("inspects only on request and offers the actual native model ids separately", async () => {
    const resources = await withEngineResources();
    api.open.mockResolvedValue({ ...fixture, engine: codexEngine });
    api.inspectEngine.mockImplementation(async () => {
      const entry = {
        ...resources.execution_engines[1],
        capabilities: { ...acpEngineCapabilities, model_selection: true },
        models: [{ model_id: "native-model", label: "Native engine model" }],
      };
      resources.execution_engines[1] = entry;
      return entry;
    });
    api.update.mockImplementation(async (_id, changes) => ({
      ...fixture,
      engine: codexEngine,
      ...changes,
      list_revision: 2,
      configuration_revision: 2,
    }));
    mount("?task=task_a");
    const inspect = await screen.findByRole("button", {
      name: zh.projects.tasks.inspectEngine,
    });
    expect(api.inspectEngine).not.toHaveBeenCalled();
    await userEvent.click(inspect);
    expect(api.inspectEngine).toHaveBeenCalledWith(codexEngine);
    await userEvent.click(
      await screen.findByRole("combobox", {
        name: zh.projects.tasks.engineModel,
      }),
    );
    await userEvent.click(
      await screen.findByText("Native engine model", {
        selector: ".ant-select-item-option-content",
      }),
    );
    await waitFor(() =>
      expect(api.update).toHaveBeenCalledWith(
        "task_a",
        expect.objectContaining({
          engine: { ...codexEngine, model_id: "native-model" },
        }),
      ),
    );
    expect(
      screen.queryByRole("combobox", { name: zh.projects.tasks.model }),
    ).not.toBeInTheDocument();
  });
});

it("rejects an ACP/client-directory draft without rewriting either project directory", async () => {
  await withEngineResources();
  api.open.mockResolvedValue({ ...fixture, engine: codexEngine });
  render(
    <MemoryRouter initialEntries={["?task=task_a"]}>
      <TaskConversations projectID="proj_a" project={projectFixture} />
    </MemoryRouter>,
  );
  expect(
    await screen.findByText(zh.projects.tasks.engineClientDirectoryUnsupported),
  ).toBeInTheDocument();
  await userEvent.type(
    screen.getByRole("textbox", { name: zh.projects.tasks.message }),
    "Keep the directories",
  );
  expect(
    screen.getByRole("button", { name: zh.projects.tasks.send }),
  ).toBeDisabled();
  expect(api.update).not.toHaveBeenCalled();
  expect(projectFixture.directory).toBe("/server/before");
  expect(projectFixture.client_directory).toBe("/client/before");
});

it("strips recovered Octop routing from an ACP send while keeping other draft overrides", async () => {
  await withEngineResources();
  const options = {
    model_ref: "glm/glm",
    routing: { mode: "hybrid" as const, level: 80 as const },
    reasoning_mode: "enabled" as const,
    reasoning_effort: "high",
    skills: [],
    mcp_servers: [],
  };
  const item = {
    id: "queued-routing",
    body: {
      request_id: "queued-routing",
      prompt: "Recovered routing draft",
      options,
    },
    attachments: [],
  };
  const shared: import("./useTaskMessageQueue").TaskMessageQueue = {
    drafts: { current: new Map() },
    queues: {
      task_a: {
        items: [item],
        projectID: "proj_a",
        error: null,
        sendingID: null,
      },
    },
    delivered: {},
    enqueue: vi.fn(() => "ok" as const),
    remove: vi.fn(),
    reclaim: vi.fn(() => {
      shared.queues.task_a.items = [];
      return item;
    }),
    clear: vi.fn(),
    retry: vi.fn(),
    hold: vi.fn(),
    isHeld: vi.fn(() => false),
  };
  api.update.mockImplementation(async (_id, patch) => ({
    ...fixture,
    ...patch,
    list_revision: 2,
    configuration_revision: 2,
  }));
  render(
    <MemoryRouter initialEntries={["?task=task_a"]}>
      <TaskConversations projectID="proj_a" queue={shared} />
    </MemoryRouter>,
  );
  await screen.findByRole("textbox", { name: zh.projects.tasks.message });
  await userEvent.click(
    screen.getByRole("button", { name: zh.projects.composer.pendingCount }),
  );
  await userEvent.click(
    await screen.findByRole("button", { name: zh.chat.queue.reclaim }),
  );
  expect(
    screen.getByRole("textbox", { name: zh.projects.tasks.message }),
  ).toHaveValue("Recovered routing draft");
  await chooseEngine();
  await userEvent.click(
    screen.getByRole("button", { name: zh.projects.tasks.send }),
  );
  await waitFor(() =>
    expect(api.send).toHaveBeenCalledWith(
      "task_a",
      expect.objectContaining({
        prompt: "Recovered routing draft",
        options: { skills: [], mcp_servers: [] },
      }),
    ),
  );
  expect(options.routing).toEqual({ mode: "hybrid", level: 80 });
});
