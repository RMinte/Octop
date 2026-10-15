import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import zh from "../../locales/zh.json";
import { projectTasksApi, type TaskDetail } from "../../api/modules/projects";
import TaskConversations from "./TaskConversations";

vi.mock("../../api/modules/projects", () => ({
  projectTasksApi: {
    list: vi.fn(),
    sync: vi.fn(),
    get: vi.fn(),
    create: vi.fn(),
    send: vi.fn(),
    cancel: vi.fn(),
    events: vi.fn(),
    retry: vi.fn(),
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
afterEach(() => vi.useRealTimers());
const fixture: TaskDetail = {
  task_id: "task_a",
  project_id: "proj_a",
  title: "第一项任务",
  active_turn_id: null,
  list_revision: 1,
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
      reply: "已记住蓝鸟",
      status: "completed",
      error_code: "",
      activity: "",
      model_ref: "glm/glm",
      retry_of_turn_id: null,
      cancel_requested: false,
      project_revision: 1,
      created_at: "2026-10-15T00:00:00Z",
      updated_at: "2026-10-15T00:00:00Z",
    },
  ],
};
beforeEach(() => {
  vi.clearAllMocks();
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
  api.retry.mockResolvedValue(fixture);
  api.get.mockResolvedValue(fixture);
  api.create.mockResolvedValue(fixture);
  api.send.mockResolvedValue(fixture);
  api.cancel.mockResolvedValue({ ...fixture, active_turn_id: null });
});
function mount(query = "", projectID: string | null = "proj_a") {
  return render(
    <MemoryRouter initialEntries={["/box-projects/proj_a" + query]}>
      <TaskConversations projectID={projectID} />
    </MemoryRouter>,
  );
}

describe("项目任务对话", () => {
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
          request_id: expect.any(String),
        }),
      ),
    );
    expect(await screen.findByText("已记住蓝鸟")).toBeInTheDocument();
    expect(api.get).toHaveBeenCalledWith("task_a");
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
  it("执行中可以停止，历史回复保留", async () => {
    api.get.mockResolvedValue({
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
  const updated = { ...fixture, title: "新的任务标题", list_revision: 3 };
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
  ).toBeInTheDocument();
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
  api.get.mockResolvedValue(running);
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
  expect(api.get).toHaveBeenCalledTimes(1);
});

it("加载更早对话保留实时收到的新轮次", async () => {
  api.get.mockResolvedValueOnce({
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
  api.get.mockResolvedValue(failed);
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
  expect(api.get).toHaveBeenCalledTimes(1);
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
  api.get.mockResolvedValueOnce({ ...fixture, next_page_cursor: "old_range" });
  api.events.mockRejectedValueOnce(
    new Error('409 - {"error":{"code":"PROJECT_SYNC_RESET"}}'),
  );
  vi.useFakeTimers();
  await act(async () => {
    mount("?task=task_a");
  });
  expect(screen.getByText("连接中断，正在重新连接…")).toBeInTheDocument();
  await act(async () => vi.advanceTimersByTimeAsync(2000));
  expect(api.get.mock.calls).toEqual([["task_a"], ["task_a"]]);
  expect(
    screen.queryByRole("button", { name: "加载更早的对话" }),
  ).not.toBeInTheDocument();
});

it("实时接口解析跨网络分块的中文与 SSE 帧，并释放连接", async () => {
  const requests = await import("../../api/request");
  const actual = await vi.importActual<
    typeof import("../../api/modules/projects")
  >("../../api/modules/projects");
  const payload = {
    task: fixture,
    turn: fixture.turns[0],
    cursor: "event_parsed",
  };
  const bytes = new TextEncoder().encode(
    `: heartbeat\r\n\r\nid: event_parsed\r\nevent: turn\r\ndata: ${JSON.stringify(
      payload,
    )}\r\n\r\n`,
  );
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (let index = 0; index < bytes.length; index += 7)
        controller.enqueue(bytes.slice(index, index + 7));
      controller.close();
    },
  });
  const stream = vi
    .spyOn(requests, "requestStream")
    .mockResolvedValue({ contentType: "text/event-stream", body });
  const receive = vi.fn();
  const controller = new AbortController();
  try {
    await actual.projectTasksApi.events(
      "task_a",
      "event_start",
      receive,
      controller.signal,
    );
    expect(receive).toHaveBeenCalledExactlyOnceWith({
      type: "turn",
      data: payload,
    });
    expect(stream).toHaveBeenCalledWith(
      "/project-tasks/task_a/events?cursor=event_start",
      expect.objectContaining({ signal: controller.signal }),
    );
    expect(body.locked).toBe(false);
  } finally {
    stream.mockRestore();
  }
});
