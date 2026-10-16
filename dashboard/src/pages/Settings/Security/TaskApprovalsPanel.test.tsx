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
    allowed_modes: ["ask_for_approval"],
    require_human_tools: ["publish", "custom__charge"],
  });
  api.saveTaskApprovals.mockImplementation(async (body) => body);
});

it("管理员开放自动审批时保留强制人工工具，请求批准始终可用", async () => {
  const user = userEvent.setup();
  render(<TaskApprovalsPanel />);
  const ask = await screen.findByRole("checkbox", {
    name: zh.projects.tasks.approvalMode.ask_for_approval,
  });
  expect(ask).toBeChecked();
  expect(ask).toBeDisabled();
  await user.click(
    screen.getByRole("checkbox", {
      name: zh.projects.tasks.approvalMode.approve_for_me,
    }),
  );
  await user.click(
    screen.getByRole("button", { name: zh.security.taskApprovals.save }),
  );
  await waitFor(() =>
    expect(api.saveTaskApprovals).toHaveBeenCalledWith({
      allowed_modes: ["ask_for_approval", "approve_for_me"],
      require_human_tools: ["publish", "custom__charge"],
    }),
  );
  await screen.findByText(zh.security.saved);
});

it("保存失败保留用户选择与原列表，不显示成功", async () => {
  api.saveTaskApprovals.mockRejectedValueOnce(new Error("Settings offline"));
  const user = userEvent.setup();
  render(<TaskApprovalsPanel />);
  const full = await screen.findByRole("checkbox", {
    name: zh.projects.tasks.approvalMode.full_access,
  });
  await user.click(full);
  await user.click(
    screen.getByRole("button", { name: zh.security.taskApprovals.save }),
  );
  await screen.findByText("Settings offline");
  expect(full).toBeChecked();
  expect(
    screen.getByRole("textbox", {
      name: zh.security.taskApprovals.requireHuman,
    }),
  ).toHaveValue("publish\ncustom__charge");
  expect(screen.queryByText(zh.security.saved)).not.toBeInTheDocument();
});
