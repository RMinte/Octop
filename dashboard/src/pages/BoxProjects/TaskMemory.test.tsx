import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { taskExtrasApi } from "../../api/modules/taskExtras";
import TaskMemory from "./TaskMemory";
vi.mock("../../api/modules/taskExtras", () => ({
  taskExtrasApi: {
    memory: vi.fn(),
    saveMemory: vi.fn(),
    deleteMemory: vi.fn(),
  },
}));
vi.mock("react-i18next", () => {
  const t = (key: string) => key;
  return { useTranslation: () => ({ t }) };
});
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(taskExtrasApi.memory).mockResolvedValue({
    task_id: "task",
    enabled: true,
    entries: [
      { id: "atom/a", content: "fact", layer: "atom", editable: true },
      { id: "raw/r", content: "evidence", layer: "raw", editable: false },
    ],
  });
});
it("keeps raw evidence read-only and updates the selected canonical identity", async () => {
  vi.mocked(taskExtrasApi.saveMemory).mockResolvedValue({
    id: "atom/a",
    content: "corrected",
    layer: "atom",
    editable: true,
  });
  render(<TaskMemory taskID="task" />);
  fireEvent.click(
    screen.getByRole("button", { name: "projects.tasks.taskMemory.title" }),
  );
  await screen.findByText("evidence");
  expect(
    screen.getAllByRole("button", { name: "projects.tasks.workbench.edit" }),
  ).toHaveLength(1);
  fireEvent.click(
    screen.getByRole("button", { name: "projects.tasks.workbench.edit" }),
  );
  fireEvent.change(
    screen.getByRole("textbox", { name: "projects.tasks.taskMemory.content" }),
    { target: { value: "corrected" } },
  );
  fireEvent.click(
    screen.getByRole("button", { name: "projects.tasks.taskMemory.save" }),
  );
  await waitFor(() =>
    expect(taskExtrasApi.saveMemory).toHaveBeenCalledWith("task", {
      id: "atom/a",
      content: "corrected",
      request_id: expect.any(String),
    }),
  );
});
it("retains an idempotency ID when retrying the same unknown save", async () => {
  vi.mocked(taskExtrasApi.saveMemory).mockRejectedValue(new Error("unknown"));
  render(<TaskMemory taskID="task" />);
  fireEvent.click(
    screen.getByRole("button", { name: "projects.tasks.taskMemory.title" }),
  );
  await screen.findByText("fact");
  fireEvent.change(
    screen.getByRole("textbox", { name: "projects.tasks.taskMemory.content" }),
    { target: { value: "new fact" } },
  );
  const save = screen.getByRole("button", {
    name: "projects.tasks.taskMemory.save",
  });
  fireEvent.click(save);
  await screen.findByText("unknown");
  await waitFor(() => expect(save).not.toBeDisabled());
  fireEvent.click(save);
  await waitFor(() =>
    expect(taskExtrasApi.saveMemory).toHaveBeenCalledTimes(2),
  );
  expect(vi.mocked(taskExtrasApi.saveMemory).mock.calls[0]).toEqual(
    vi.mocked(taskExtrasApi.saveMemory).mock.calls[1],
  );
});
