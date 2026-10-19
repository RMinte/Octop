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
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  projectTasksApi,
  type TaskDetail,
  type TaskEvent,
  type TaskInputAttachment,
  type TaskMessage,
  type TaskStatus,
  type TaskTurn,
} from "../../api/modules/projects";
import zh from "../../locales/zh.json";
import TaskConversations from "./TaskConversations";
import type { TaskMessageQueue } from "./useTaskMessageQueue";

vi.mock("../../api/modules/projects", () => ({
  projectTasksApi: {
    settings: vi.fn(),
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
    resources: vi.fn(),
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
  const t = (key: string, options?: Record<string, unknown>) => {
    const value = key
      .split(".")
      .reduce<unknown>(
        (current, segment) => (current as Record<string, unknown>)?.[segment],
        zh,
      );
    return String(value ?? key).replace(
      /\{\{(\w+)\}\}/g,
      (match, name: string) =>
        options && name in options ? String(options[name]) : match,
    );
  };
  return { useTranslation: () => ({ t }) };
});

const api = vi.mocked(projectTasksApi, true);
const timestamp = "2026-10-15T00:00:00Z";
const policy = {
  mode: "ask_for_approval" as const,
  revision: 1,
  updated_at: timestamp,
};
const image: TaskInputAttachment = {
  attachment_id: "image_a",
  name: "截图.png",
  media_type: "image/png",
  byte_count: 5,
  sha256: "a".repeat(64),
  preview_url: "/api/project-tasks/attachments/image_a",
  created_at: timestamp,
};
const document: TaskInputAttachment = {
  ...image,
  attachment_id: "file_a",
  name: "报告.pdf",
  media_type: "application/pdf",
  preview_url: "/api/project-tasks/attachments/file_a",
  workspace_path: "inbound/file_a/报告.pdf",
};

function turn(
  id: string,
  sequence: number,
  status: TaskStatus,
  body?: TaskMessage,
): TaskTurn {
  return {
    sequence,
    turn_id: id,
    request_id: body?.request_id ?? `request_${id}`,
    prompt: body?.prompt ?? "原始问题",
    attachments: (body?.attachment_ids ?? []).map((id) =>
      id === image.attachment_id ? image : document,
    ),
    reply: "已有回复",
    reasoning: "",
    content_blocks: [],
    content_revision: 0,
    plan: null,
    plan_revision: 0,
    status,
    error_code: "",
    activity: "",
    artifacts: [],
    model_ref: "glm/glm",
    options: body?.options ?? {},
    web_search_enabled: false,
    project_revision: 1,
    approval_policy_at_start: policy,
    retry_of_turn_id: null,
    cancel_requested: false,
    pause_requested: false,
    interactions: [],
    interaction_revision: 0,
    created_at: timestamp,
    updated_at: timestamp,
  };
}

function task(id = "task_a", title = "第一项任务"): TaskDetail {
  const initialTurn = turn(`turn_${id}_1`, 1, "running");
  return {
    task_id: id,
    project_id: "proj_a",
    title,
    archived: false,
    status: "running",
    active_turn_id: initialTurn.turn_id,
    list_revision: 1,
    web_search_enabled: false,
    approval_policy: policy,
    created_at: timestamp,
    updated_at: timestamp,
    turns: [initialTurn],
    next_page_cursor: null,
    event_cursor: `event_${id}_1`,
  };
}

let snapshots: Map<string, TaskDetail>;
let subscriptions: Array<{
  taskID: string;
  receive: (event: TaskEvent) => void;
  signal: AbortSignal;
}>;

beforeEach(() => {
  vi.clearAllMocks();
  api.approvalPolicy.mockResolvedValue(policy);
  api.updateApprovalPolicy.mockImplementation(async (body) => ({
    ...policy,
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
  sessionStorage.clear();
  snapshots = new Map([
    ["task_a", task()],
    ["task_b", task("task_b", "另一项任务")],
  ]);
  subscriptions = [];
  api.list.mockImplementation(async () => ({
    items: [...snapshots.values()],
    next_page_cursor: null,
    sync_cursor: "sync_1",
  }));
  api.sync.mockResolvedValue({
    base_sync_cursor: "sync_1",
    next_sync_cursor: "sync_2",
    next_page_cursor: null,
    changes: [],
  });
  api.open.mockImplementation(async (id) => snapshots.get(id)!);
  api.get.mockImplementation(async (id) => snapshots.get(id)!);
  api.events.mockImplementation((taskID, _cursor, receive, signal, onOpen) => {
    subscriptions.push({ taskID, receive, signal });
    onOpen?.();
    return new Promise<void>((resolve) =>
      signal.addEventListener("abort", () => resolve(), { once: true }),
    );
  });
  api.send.mockImplementation(async (id, body) => accept(id, body));
  api.cancel.mockImplementation(async (id) => {
    const current = snapshots.get(id)!;
    const incoming = {
      ...current,
      list_revision: current.list_revision + 1,
      turns: current.turns.map((row) =>
        row.turn_id === current.active_turn_id
          ? { ...row, cancel_requested: true }
          : row,
      ),
    };
    snapshots.set(id, incoming);
    return incoming;
  });
  api.resources.mockResolvedValue({
    skills: [],
    mcp_servers: [],
    plugins: [],
    subagents: [],
    directory_roots: [],
    models: [{ model_ref: "glm/glm", name: "GLM", supports_images: true }],
    approval: {
      allowed_modes: ["ask_for_approval", "approve_for_me", "full_access"],
      reviewer_ready: true,
      execution_profile: "host_process",
    },
    default_model_ref: "glm/glm",
  });
  api.uploadAttachment.mockImplementation(async (file) =>
    file.type === "image/png" ? image : document,
  );
  api.attachmentPreview.mockResolvedValue(new Blob(["attachment"]));
  let imageURL = 0;
  URL.createObjectURL = vi.fn(() => `blob:queued-file-${++imageURL}`);
  URL.revokeObjectURL = vi.fn();
});

function accept(taskID: string, body: TaskMessage) {
  const current = snapshots.get(taskID)!;
  const nextTurn = turn(
    `turn_${taskID}_${current.turns.length + 1}`,
    current.turns.length + 1,
    "running",
    body,
  );
  const incoming: TaskDetail = {
    ...current,
    status: "running",
    active_turn_id: nextTurn.turn_id,
    list_revision: current.list_revision + 1,
    turns: [...current.turns, nextTurn],
  };
  snapshots.set(taskID, incoming);
  return incoming;
}

async function publish(taskID: string, status: TaskStatus) {
  const current = snapshots.get(taskID)!;
  const terminal =
    status === "completed" || status === "failed" || status === "cancelled";
  const incoming: TaskDetail = {
    ...current,
    status,
    active_turn_id: terminal ? null : current.active_turn_id,
    list_revision: current.list_revision + 1,
    event_cursor: `event_${taskID}_${current.list_revision + 1}`,
    turns: current.turns.map((row) =>
      row.turn_id === current.active_turn_id ? { ...row, status } : row,
    ),
  };
  snapshots.set(taskID, incoming);
  await act(async () => {
    for (const subscription of subscriptions) {
      if (subscription.taskID === taskID && !subscription.signal.aborted)
        subscription.receive({ type: "snapshot", data: incoming });
    }
  });
}

async function mount() {
  const view = render(
    <MemoryRouter initialEntries={["/box-projects/proj_a?task=task_a"]}>
      <TaskConversations projectID="proj_a" />
    </MemoryRouter>,
  );
  await waitFor(() =>
    expect(screen.getByRole("textbox", { name: "任务消息" })).toBeEnabled(),
  );
  return view;
}

const composer = () => screen.getByRole("textbox", { name: "任务消息" });
const queue = () => screen.getByLabelText("排队中的消息");
async function enqueue(text: string) {
  fireEvent.change(composer(), { target: { value: text } });
  await userEvent.click(screen.getByRole("button", { name: "排队" }));
  await waitFor(() => expect(composer()).toHaveValue(""));
}

describe("项目任务消息队列", () => {
  it("执行中可通过按钮和 Enter 排队，清空草稿且不立即发送", async () => {
    await mount();
    expect(screen.getByRole("button", { name: "排队" })).toBeDisabled();
    await enqueue("先整理结果");
    fireEvent.change(composer(), { target: { value: "然后补充结论" } });
    fireEvent.keyDown(composer(), { key: "Enter", code: "Enter" });
    await waitFor(() => expect(composer()).toHaveValue(""));
    expect(within(queue()).getByText("先整理结果")).toBeInTheDocument();
    expect(within(queue()).getByText("然后补充结论")).toBeInTheDocument();
    expect(api.send).not.toHaveBeenCalled();
    expect(api.create).not.toHaveBeenCalled();
  });

  it("纯图片和通用文件可一起排队，取回后复用附件并在终态发送", async () => {
    await mount();
    const input = screen.getByLabelText("添加附件", { selector: "input" });
    await userEvent.upload(input, [
      new File(["image"], image.name, { type: image.media_type }),
      new File(["pdf"], document.name, { type: document.media_type }),
    ]);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "排队" })).toBeEnabled(),
    );
    await userEvent.click(screen.getByRole("button", { name: "排队" }));
    expect(screen.queryByAltText(image.name)).not.toBeInTheDocument();
    await userEvent.click(within(queue()).getByTitle("取回编辑"));
    expect(screen.getByAltText(image.name)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "下载" })).toHaveAttribute(
      "download",
      document.name,
    );
    expect(api.uploadAttachment).toHaveBeenCalledTimes(2);
    await userEvent.click(screen.getByRole("button", { name: "排队" }));
    await publish("task_a", "completed");
    await waitFor(() => expect(api.send).toHaveBeenCalledTimes(1));
    expect(api.send).toHaveBeenCalledWith(
      "task_a",
      expect.objectContaining({
        prompt: "",
        attachment_ids: [image.attachment_id, document.attachment_id],
      }),
    );
    expect(api.uploadAttachment).toHaveBeenCalledTimes(2);
  });

  it("终态后按顺序逐轮发送，收到发送回执前保留队头", async () => {
    let finish!: (value: TaskDetail) => void;
    api.send.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    await mount();
    await enqueue("第一条追问");
    await enqueue("第二条追问");
    await publish("task_a", "completed");
    await waitFor(() => expect(api.send).toHaveBeenCalledTimes(1));
    expect(within(queue()).getByText("第一条追问")).toBeInTheDocument();
    expect(within(queue()).getByText("第二条追问")).toBeInTheDocument();
    const firstBody = api.send.mock.calls[0][1];
    await act(async () => finish(accept("task_a", firstBody)));
    await waitFor(() =>
      expect(within(queue()).queryByText("第一条追问")).not.toBeInTheDocument(),
    );
    expect(api.send).toHaveBeenCalledTimes(1);
    await publish("task_a", "completed");
    await waitFor(() => expect(api.send).toHaveBeenCalledTimes(2));
    expect(api.send.mock.calls.map(([id, body]) => [id, body.prompt])).toEqual([
      ["task_a", "第一条追问"],
      ["task_a", "第二条追问"],
    ]);
  });

  it("等待审批、回答、客户端工具及暂停均保留队列，恢复完成后才发送", async () => {
    await mount();
    await enqueue("等待当前轮完成");
    for (const status of [
      "waiting_approval",
      "waiting_answer",
      "waiting_client_tool",
      "paused",
    ] as const) {
      await publish("task_a", status);
      expect(api.send).not.toHaveBeenCalled();
      expect(within(queue()).getByText("等待当前轮完成")).toBeInTheDocument();
    }
    await publish("task_a", "completed");
    await waitFor(() => expect(api.send).toHaveBeenCalledTimes(1));
  });

  it("最多排队十条，超过上限时保留当前草稿", async () => {
    await mount();
    for (let index = 1; index <= 10; index += 1)
      await enqueue(`排队第${index}条`);
    fireEvent.change(composer(), { target: { value: "第十一条草稿" } });
    await userEvent.click(screen.getByRole("button", { name: "排队" }));
    expect(composer()).toHaveValue("第十一条草稿");
    expect(within(queue()).getAllByTitle("取回编辑")).toHaveLength(10);
    expect(await screen.findByText(zh.chat.queue.full)).toBeInTheDocument();
    expect(api.send).not.toHaveBeenCalled();
  });

  it("支持移除队列消息，已有草稿时取回编辑必须确认覆盖", async () => {
    await mount();
    await enqueue("可以移除");
    await enqueue("可以取回");
    await userEvent.click(
      within(queue()).getAllByRole("button", { name: "从队列移除" })[0],
    );
    expect(within(queue()).queryByText("可以移除")).not.toBeInTheDocument();
    fireEvent.change(composer(), { target: { value: "未完成草稿" } });
    await userEvent.click(within(queue()).getByTitle("取回编辑"));
    const modal = await screen.findByRole("dialog");
    expect(modal).toHaveAccessibleName("替换当前草稿？");
    expect(composer()).toHaveValue("未完成草稿");
    await userEvent.click(
      within(modal).getByRole("button", { name: /确\s*认/ }),
    );
    await waitFor(() => expect(composer()).toHaveValue("可以取回"));
    expect(screen.queryByLabelText("排队中的消息")).not.toBeInTheDocument();
  });

  it("发送失败保留队头，后续事件不自动重发，手动重试复用请求标识", async () => {
    api.send.mockRejectedValueOnce(new Error("network unavailable"));
    await mount();
    await enqueue("需要重试的追问");
    await publish("task_a", "failed");
    const retry = await screen.findByRole("button", { name: "重试发送" });
    expect(within(queue()).getByText("需要重试的追问")).toBeInTheDocument();
    expect(api.send).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "重新尝试" })).toBeDisabled();
    await enqueue("追加到队尾");
    expect(api.send).toHaveBeenCalledTimes(1);
    expect(within(queue()).getByText("追加到队尾")).toBeInTheDocument();
    const requestID = api.send.mock.calls[0][1].request_id;
    await publish("task_a", "completed");
    expect(api.send).toHaveBeenCalledTimes(1);
    await userEvent.click(retry);
    await waitFor(() => expect(api.send).toHaveBeenCalledTimes(2));
    expect(api.send.mock.calls[1][1].request_id).toBe(requestID);
    await waitFor(() =>
      expect(
        within(queue()).queryByText("需要重试的追问"),
      ).not.toBeInTheDocument(),
    );
    expect(within(queue()).getByText("追加到队尾")).toBeInTheDocument();
    await publish("task_a", "completed");
    await waitFor(() => expect(api.send).toHaveBeenCalledTimes(3));
    expect(api.send.mock.calls[2][1].prompt).toBe("追加到队尾");
  });

  it("停止只取消当前轮次，取消终态确认后继续发送排队消息", async () => {
    await mount();
    await enqueue("停止后继续");
    await userEvent.click(screen.getByRole("button", { name: "停止回复" }));
    expect(api.cancel).toHaveBeenCalledWith("task_a", "turn_task_a_1");
    expect(api.send).not.toHaveBeenCalled();
    expect(within(queue()).getByText("停止后继续")).toBeInTheDocument();
    await publish("task_a", "cancelled");
    await waitFor(() => expect(api.send).toHaveBeenCalledTimes(1));
    expect(api.send).toHaveBeenCalledWith(
      "task_a",
      expect.objectContaining({ prompt: "停止后继续" }),
    );
  });

  it("切换任务保留各自队列，后台任务完成只发送到原任务", async () => {
    await mount();
    await enqueue("原任务后台追问");
    await userEvent.click(screen.getByRole("button", { name: /另一项任务/ }));
    await waitFor(() => expect(api.open).toHaveBeenCalledWith("task_b"));
    await waitFor(() => expect(composer()).toBeEnabled());
    expect(screen.queryByLabelText("排队中的消息")).not.toBeInTheDocument();
    await enqueue("新任务队列");
    await publish("task_a", "completed");
    await waitFor(() => expect(api.send).toHaveBeenCalledTimes(1));
    expect(api.send).toHaveBeenCalledWith(
      "task_a",
      expect.objectContaining({ prompt: "原任务后台追问" }),
    );
    expect(within(queue()).getByText("新任务队列")).toBeInTheDocument();
    expect(screen.queryByText("原任务后台追问")).not.toBeInTheDocument();
  });

  it("新任务清空当前任务的待发队列，原任务随后完成不会再派发", async () => {
    await mount();
    await enqueue("不再发送");
    await userEvent.click(screen.getByRole("button", { name: "新任务" }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "发送" })).toBeInTheDocument(),
    );
    expect(composer()).toHaveValue("");
    expect(screen.queryByLabelText("排队中的消息")).not.toBeInTheDocument();
    await publish("task_a", "completed");
    expect(api.send).not.toHaveBeenCalled();
  });

  it("取回确认期间切换任务，迟到确认不能覆盖新任务草稿", async () => {
    await mount();
    await enqueue("原任务待取回消息");
    fireEvent.change(composer(), { target: { value: "原任务草稿" } });
    await userEvent.click(within(queue()).getByTitle("取回编辑"));
    const modal = await screen.findByRole("dialog");
    await userEvent.click(screen.getByRole("button", { name: /另一项任务/ }));
    await waitFor(() => expect(api.open).toHaveBeenCalledWith("task_b"));
    await waitFor(() => expect(composer()).toBeEnabled());
    fireEvent.change(composer(), { target: { value: "新任务草稿" } });
    await userEvent.click(
      within(modal).getByRole("button", { name: /确\s*认/ }),
    );
    expect(composer()).toHaveValue("新任务草稿");
    await userEvent.click(screen.getByRole("button", { name: /第一项任务/ }));
    await waitFor(() =>
      expect(within(queue()).getByText("原任务待取回消息")).toBeInTheDocument(),
    );
    expect(api.send).not.toHaveBeenCalled();
  });

  it("较新的队列回执不被迟到打开结果覆盖，旧回执也不能回退终态", async () => {
    const old = task();
    const newer: TaskDetail = {
      ...old,
      list_revision: 5,
      active_turn_id: null,
      status: "completed",
      turns: [{ ...old.turns[0], status: "completed", reply: "最新终态回复" }],
    };
    let finishOpen!: (value: TaskDetail) => void;
    api.open.mockReturnValueOnce(
      new Promise((resolve) => {
        finishOpen = resolve;
      }),
    );
    const shared: TaskMessageQueue = {
      queues: {},
      delivered: { task_a: newer },
      enqueue: vi.fn(() => "ok" as const),
      remove: vi.fn(),
      reclaim: vi.fn(() => null),
      clear: vi.fn(),
      retry: vi.fn(),
      hold: vi.fn(),
      isHeld: vi.fn(() => false),
    };
    const page = (queue: TaskMessageQueue) => (
      <MemoryRouter initialEntries={["/box-projects/proj_a?task=task_a"]}>
        <TaskConversations projectID="proj_a" queue={queue} />
      </MemoryRouter>
    );
    const view = render(page(shared));
    await waitFor(() => expect(api.open).toHaveBeenCalledWith("task_a"));
    await act(async () => finishOpen(old));
    expect(await screen.findByText("最新终态回复")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "停止回复" }),
    ).not.toBeInTheDocument();
    view.rerender(
      page({ ...shared, delivered: { task_a: { ...old, list_revision: 3 } } }),
    );
    expect(screen.getByText("最新终态回复")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "停止回复" }),
    ).not.toBeInTheDocument();
    expect(api.send).not.toHaveBeenCalled();
  });
});
