import { useState } from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import {
  projectTasksApi,
  type TaskExecutionOptions,
} from "../../api/modules/projects";
import zh from "../../locales/zh.json";
import TaskExecutionSettings from "./TaskExecutionSettings";

vi.mock("../../api/modules/projects", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../api/modules/projects")>()),
  projectTasksApi: { resources: vi.fn() },
}));
vi.mock("react-i18next", () => {
  const t = (key: string, options?: Record<string, unknown>) => {
    const value = key
      .split(".")
      .reduce<unknown>(
        (item, part) => (item as Record<string, unknown>)?.[part],
        zh,
      );
    return typeof value === "string"
      ? value.replace(/\{\{(\w+)\}\}/g, (match, name) =>
          String(options?.[name] ?? match),
        )
      : key;
  };
  return { useTranslation: () => ({ t }) };
});

function Settings() {
  const [value, onChange] = useState<TaskExecutionOptions>({});
  return (
    <TaskExecutionSettings value={value} onChange={onChange} disabled={false} />
  );
}

it("模型无需展开设置即可选择，按来源分组并显示具体默认和本次模型", async () => {
  vi.mocked(projectTasksApi.resources).mockResolvedValue({
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
    default_model_ref: "remote/glm",
    models: [
      {
        model_ref: "remote/glm",
        name: "GLM",
        provider_kind: "openai",
        supports_images: false,
      },
      {
        model_ref: "box/RM-01 VLM",
        name: "RM-01 VLM",
        provider_kind: "box",
        supports_images: true,
      },
    ],
  });
  render(<Settings />);
  expect(screen.getByRole("combobox", { name: "本轮模型" })).toBeVisible();
  expect(screen.queryByRole("switch", { name: "自定义本轮 Skill" })).toBeNull();
  await screen.findByText("本次使用：GLM · remote/glm");
  const user = userEvent.setup();
  await user.click(screen.getByRole("combobox", { name: "本轮模型" }));
  expect(screen.getByText("盒子模型")).toBeInTheDocument();
  expect(screen.getAllByText("远端与其他服务").length).toBeGreaterThan(0);
  await user.click(
    await screen.findByText("RM-01 VLM · box/RM-01 VLM · 支持图片"),
  );
  await screen.findByText("本次使用：RM-01 VLM · box/RM-01 VLM · 支持图片");
  await user.click(screen.getByRole("combobox", { name: "本轮模型" }));
  await user.click(await screen.findByText("跟随默认 · GLM · remote/glm"));
  await waitFor(() =>
    expect(screen.getByText("本次使用：GLM · remote/glm")).toBeInTheDocument(),
  );
});
