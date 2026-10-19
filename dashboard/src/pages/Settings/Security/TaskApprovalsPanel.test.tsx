import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import { securityApi } from "../../../api/modules/security";
import zh from "../../../locales/zh.json";
import TaskApprovalsPanel from "./TaskApprovalsPanel";

vi.mock("../../../api/modules/security", () => ({
  securityApi: { getTaskApprovals: vi.fn(), saveTaskApprovals: vi.fn() },
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
const api = vi.mocked(securityApi, true);
beforeEach(() => {
  vi.clearAllMocks();
  api.getTaskApprovals.mockResolvedValue({
    require_human_tools: ["publish", "custom__charge"],
  });
  api.saveTaskApprovals.mockImplementation(async (body) => body);
});

it("管理员仅管理强制人工工具列表，保存时去重并去除空行", async () => {
  const user = userEvent.setup();
  render(<TaskApprovalsPanel />);
  const tools = await screen.findByRole("textbox", {
    name: zh.security.taskApprovals.requireHuman,
  });
  expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
  await user.clear(tools);
  await user.type(tools, " publish \n\ncustom__charge\npublish\n");
  await user.click(
    screen.getByRole("button", { name: zh.security.taskApprovals.save }),
  );
  await waitFor(() =>
    expect(api.saveTaskApprovals).toHaveBeenCalledWith({
      require_human_tools: ["publish", "custom__charge"],
    }),
  );
  await screen.findByText(zh.security.saved);
});

it("保存失败保留用户编辑的列表，不显示成功", async () => {
  api.saveTaskApprovals.mockRejectedValueOnce(new Error("Settings offline"));
  const user = userEvent.setup();
  render(<TaskApprovalsPanel />);
  const tools = await screen.findByRole("textbox", {
    name: zh.security.taskApprovals.requireHuman,
  });
  await user.type(tools, "\nexecute");
  await user.click(
    screen.getByRole("button", { name: zh.security.taskApprovals.save }),
  );
  await screen.findByText("Settings offline");
  expect(tools).toHaveValue("publish\ncustom__charge\nexecute");
  expect(screen.queryByText(zh.security.saved)).not.toBeInTheDocument();
});
