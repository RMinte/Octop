import { useState } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  knowledgeBasesApi,
  type KnowledgeBase,
} from "../../api/modules/knowledgeBases";
import {
  projectTasksApi,
  type TaskExecutionOptions,
} from "../../api/modules/projects";
import zh from "../../locales/zh.json";
import TaskExecutionSettings from "./TaskExecutionSettings";

const currentUser = vi.hoisted(() => ({ id: 7 as number | null }));
vi.mock("../../api/modules/knowledgeBases", () => ({
  knowledgeBasesApi: { getCapability: vi.fn(), list: vi.fn() },
}));
vi.mock("../../api/modules/projects", () => ({
  projectTasksApi: { resources: vi.fn() },
}));
vi.mock("../../hooks/useCurrentUser", () => ({
  useCurrentUser: () =>
    currentUser.id == null ? null : { id: currentUser.id },
}));
vi.mock("react-i18next", () => {
  const t = (key: string) =>
    key
      .split(".")
      .reduce<unknown>(
        (value, part) => (value as Record<string, unknown>)?.[part],
        zh,
      ) ?? key;
  return { useTranslation: () => ({ t }) };
});

const knowledge = vi.mocked(knowledgeBasesApi);
const defaults = [
  {
    id: "owned-default",
    name: "我的默认库",
    owner_user_id: 7,
    default_open: true,
    shared: false,
  },
  {
    id: "owned-other",
    name: "我的其他库",
    owner_user_id: 7,
    default_open: false,
    shared: false,
  },
  {
    id: "shared-default",
    name: "同事共享库",
    owner_user_id: 8,
    default_open: true,
    shared: true,
  },
] as KnowledgeBase[];

beforeEach(() => {
  vi.clearAllMocks();
  currentUser.id = 7;
  knowledge.getCapability.mockResolvedValue({ usable: true } as Awaited<
    ReturnType<typeof knowledgeBasesApi.getCapability>
  >);
  knowledge.list.mockResolvedValue(defaults);
  vi.mocked(projectTasksApi.resources).mockResolvedValue({
    models: [],
    default_model_ref: null,
    plugins: [],
    subagents: [],
    skills: [],
    mcp_servers: [],
    directory_roots: [],
    approval: {
      allowed_modes: [],
      reviewer_ready: false,
      execution_profile: "host_process",
    },
  });
});

function Settings({
  initial = {},
  changed,
}: {
  initial?: TaskExecutionOptions;
  changed: (value: TaskExecutionOptions) => void;
}) {
  const [value, setValue] = useState(initial);
  return (
    <TaskExecutionSettings
      value={value}
      disabled={false}
      onChange={(next) => {
        setValue(next);
        changed(next);
      }}
    />
  );
}

describe("任务本轮知识库设置", () => {
  it("继承本人默认库，自选共享库和清空后重开仍保留显式禁用", async () => {
    const user = userEvent.setup();
    const changed = vi.fn();
    const view = render(
      <Settings changed={changed} initial={{ plugins: [] }} />,
    );
    expect(knowledge.list).not.toHaveBeenCalled();
    await user.click(screen.getByText("本轮设置"));
    await screen.findByText(/发送时使用我默认开启的知识库/);
    await waitFor(() => expect(knowledge.list).toHaveBeenCalledWith());
    expect(knowledge.getCapability).toHaveBeenCalledWith();
    expect(screen.getByText(/发送时使用我默认开启的知识库/)).toHaveTextContent(
      "我的默认库",
    );
    expect(
      screen.getByText(/发送时使用我默认开启的知识库/),
    ).not.toHaveTextContent("同事共享库");
    expect(changed).not.toHaveBeenCalled();

    await user.click(screen.getByRole("switch", { name: "为本轮选择知识库" }));
    expect(changed).toHaveBeenLastCalledWith({
      plugins: [],
      knowledge_base_ids: ["owned-default"],
    });
    await user.click(screen.getByRole("combobox", { name: "本轮知识库" }));
    await user.click(screen.getByText("同事共享库 · 共享"));
    expect(changed).toHaveBeenLastCalledWith({
      plugins: [],
      knowledge_base_ids: ["owned-default", "shared-default"],
    });
    await user.keyboard("{Escape}");
    const clear = view.container.querySelector(".ant-select-clear");
    expect(clear).not.toBeNull();
    fireEvent.mouseDown(clear!);
    expect(changed).toHaveBeenLastCalledWith({
      plugins: [],
      knowledge_base_ids: [],
    });
    const saved = JSON.parse(
      JSON.stringify(changed.mock.calls.at(-1)?.[0]),
    ) as TaskExecutionOptions;
    view.unmount();

    render(<Settings initial={saved} changed={changed} />);
    await user.click(screen.getByText("本轮设置"));
    await waitFor(() =>
      expect(
        screen.getByRole("switch", { name: "为本轮选择知识库" }),
      ).not.toBeDisabled(),
    );
    expect(
      screen.getByRole("switch", { name: "为本轮选择知识库" }),
    ).toBeChecked();
    expect(screen.getByText("本轮不使用知识库")).toBeVisible();
    await user.click(screen.getByRole("switch", { name: "为本轮选择知识库" }));
    expect(changed).toHaveBeenLastCalledWith({
      plugins: [],
      knowledge_base_ids: undefined,
    });
  });

  it("加载失败保持既有选择，不自动覆盖本轮选项", async () => {
    const user = userEvent.setup();
    const changed = vi.fn();
    knowledge.list.mockRejectedValue(new Error("知识库暂不可读取"));
    render(
      <Settings
        initial={{ knowledge_base_ids: ["shared-default"] }}
        changed={changed}
      />,
    );
    await user.click(screen.getByText("本轮设置"));
    expect(await screen.findByText("知识库暂不可读取")).toBeVisible();
    expect(
      screen.getByRole("switch", { name: "为本轮选择知识库" }),
    ).toBeDisabled();
    expect(changed).not.toHaveBeenCalled();
  });

  it("未取得当前用户时自选从空列表开始，共享默认库不会混入", async () => {
    currentUser.id = null;
    const user = userEvent.setup();
    const changed = vi.fn();
    render(<Settings changed={changed} />);
    await user.click(screen.getByText("本轮设置"));
    await waitFor(() =>
      expect(
        screen.getByRole("switch", { name: "为本轮选择知识库" }),
      ).not.toBeDisabled(),
    );
    await user.click(screen.getByRole("switch", { name: "为本轮选择知识库" }));
    expect(changed).toHaveBeenLastCalledWith({ knowledge_base_ids: [] });
    expect(screen.getByText("本轮不使用知识库")).toBeVisible();
  });
});
