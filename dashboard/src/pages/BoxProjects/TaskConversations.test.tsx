import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
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
const fixture: TaskDetail = {
  task_id: "task_a",
  project_id: "proj_a",
  title: "第一项任务",
  active_turn_id: null,
  list_revision: 1,
  created_at: "2026-10-15T00:00:00Z",
  updated_at: "2026-10-15T00:00:00Z",
  turns: [
    {
      turn_id: "turn_a",
      request_id: "request-first",
      prompt: "记住蓝鸟",
      reply: "已记住蓝鸟",
      status: "completed",
      error_code: "",
      activity: "",
      model_ref: "glm/glm",
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
    expect(api.cancel).toHaveBeenCalledWith("task_a");
    expect(await screen.findByText("已记住蓝鸟")).toBeInTheDocument();
  });
});

it("选择任务沿用已加载列表，后续只请求增量", async () => {
  const user = userEvent.setup();
  mount();
  await user.click(await screen.findByRole("button", { name: /第一项任务/ }));
  expect(await screen.findByText("已记住蓝鸟")).toBeInTheDocument();
  expect(api.list).toHaveBeenCalledTimes(1);
  expect(api.sync).toHaveBeenCalledWith("proj_a", "sync_first", null);
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
