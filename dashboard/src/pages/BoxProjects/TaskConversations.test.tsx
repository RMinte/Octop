import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import zh from "../../locales/zh.json";
import { projectTasksApi, type TaskDetail } from "../../api/modules/projects";
import TaskConversations from "./TaskConversations";

vi.mock("../../api/modules/projects", () => ({
  projectTasksApi: {
    list: vi.fn(),
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
  api.list.mockResolvedValue([fixture]);
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
