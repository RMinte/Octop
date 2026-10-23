import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";
import { taskMigrationsApi } from "../../api/modules/taskMigrations";
import TaskMigrations from "./TaskMigrations";
vi.mock("../../api/modules/taskMigrations", () => ({
  taskMigrationsApi: {
    list: vi.fn(),
    automatic: vi.fn(),
    setAutomatic: vi.fn(),
    preflight: vi.fn(),
    create: vi.fn(),
    control: vi.fn(),
  },
}));
vi.mock("react-i18next", () => {
  const t = (key: string) => key;
  return { useTranslation: () => ({ t }) };
});
const api = vi.mocked(taskMigrationsApi);
beforeEach(() => {
  vi.clearAllMocks();
  api.list.mockResolvedValue([]);
  api.automatic.mockResolvedValue({ enabled: false });
  api.preflight.mockResolvedValue({
    total: 1,
    eligible: 1,
    requires_model: false,
    candidates: [
      {
        thread_id: "old",
        agent_id: "expert",
        channel_type: "web",
        disposition: "eligible",
      },
    ],
  });
});
it("previews without executing and retries an unknown create using the same migration intent", async () => {
  api.create
    .mockRejectedValueOnce(new Error("unknown outcome"))
    .mockResolvedValueOnce({
      batch_id: "batch",
      status: "queued",
      created_at: 1,
      updated_at: 1,
      items: [],
    });
  render(
    <MemoryRouter>
      <TaskMigrations projects={[]} />
    </MemoryRouter>,
  );
  await userEvent.click(
    screen.getByRole("button", { name: "projects.tasks.migration.title" }),
  );
  await waitFor(() => expect(api.automatic).toHaveBeenCalledOnce());
  fireEvent.click(
    screen.getByRole("button", { name: "projects.tasks.migration.preflight" }),
  );
  await screen.findByText("projects.tasks.migration.preview");
  expect(api.create).not.toHaveBeenCalled();
  fireEvent.click(
    screen.getByRole("button", { name: "projects.tasks.migration.start" }),
  );
  await screen.findByText("unknown outcome");
  const first = api.create.mock.calls[0][0];
  expect(first).toMatchObject({ thread_ids: [], project_id: null });
  fireEvent.click(
    screen.getByRole("button", { name: "projects.tasks.migration.start" }),
  );
  await waitFor(() => expect(api.create).toHaveBeenCalledTimes(2));
  expect(api.create.mock.calls[1][0]).toEqual(first);
});
