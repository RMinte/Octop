import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";
import { bridgeApi, type BridgeConnection } from "../../api/modules/bridge";
import { projectTasksApi, type TaskSummary } from "../../api/modules/projects";
import TaskHostDirectory from "./TaskHostDirectory";
vi.mock("../../api/modules/bridge", () => ({
  bridgeApi: {
    list: vi.fn(),
    taskCapabilities: vi.fn(),
    taskReferences: vi.fn(),
  },
}));
vi.mock("../../api/modules/projects", () => ({
  projectTasksApi: { list: vi.fn() },
  createProjectTasksApi: vi.fn(),
}));
vi.mock("react-i18next", () => {
  const t = (key: string) => key;
  return { useTranslation: () => ({ t }) };
});
function Location() {
  return (
    <output data-testid="location">
      {useLocation().pathname}
      {useLocation().search}
    </output>
  );
}
beforeEach(() => vi.clearAllMocks());
it("keeps offline reference summaries separate from same-ID local rows and reopens the exact host", async () => {
  const local = {
    task_id: "same_id",
    title: "Local conversation",
    updated_at: "2026-10-23T00:00:00Z",
  } as TaskSummary;
  vi.mocked(bridgeApi.list).mockResolvedValue([
    { connection_id: "peer", display_name: "Other host" },
  ] as BridgeConnection[]);
  vi.mocked(bridgeApi.taskCapabilities).mockRejectedValue(new Error("offline"));
  vi.mocked(projectTasksApi.list).mockResolvedValue({
    items: [local],
  } as Awaited<ReturnType<typeof projectTasksApi.list>>);
  vi.mocked(bridgeApi.taskReferences).mockResolvedValue({
    host_id: "peer",
    peer_identity: "bound-peer",
    cached: true,
    items: [
      {
        host_id: "peer",
        task_id: "same_id",
        observed_at: 1,
        summary: { ...local, title: "Remote conversation" },
      },
    ],
  });
  render(
    <MemoryRouter>
      <TaskHostDirectory hostID={null} />
      <Location />
    </MemoryRouter>,
  );
  await waitFor(() =>
    expect(bridgeApi.taskCapabilities).toHaveBeenCalledWith("peer"),
  );
  await userEvent.click(screen.getByText("projects.tasks.hosts.all"));
  await screen.findByRole("button", { name: /Remote conversation/ });
  expect(
    screen.getByRole("button", { name: /Local conversation/ }),
  ).toBeInTheDocument();
  expect(screen.getByText("projects.tasks.hosts.cached")).toBeInTheDocument();
  await userEvent.click(
    screen.getByRole("button", { name: /Remote conversation/ }),
  );
  expect(screen.getByTestId("location")).toHaveTextContent(
    "/conversations/same_id?host_id=peer",
  );
});
