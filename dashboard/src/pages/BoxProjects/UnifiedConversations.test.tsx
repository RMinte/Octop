import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";
import type { ReactNode } from "react";
import {
  projectsApi,
  projectTasksApi,
  type Project,
} from "../../api/modules/projects";
import UnifiedConversations from "./UnifiedConversations";

vi.mock("../../api/modules/projects", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../api/modules/projects")>()),
  projectsApi: { list: vi.fn(), get: vi.fn() },
  projectTasksApi: { get: vi.fn() },
}));
vi.mock("../../layouts/PageShell", () => ({
  default: ({ children }: { children: ReactNode }) => <div>{children}</div>,
}));
vi.mock("react-i18next", () => {
  const t = (key: string) => key;
  return { useTranslation: () => ({ t }) };
});
vi.mock("./useTaskMessageQueue", () => ({ useTaskMessageQueue: () => ({}) }));
vi.mock("./TaskConversations", () => ({
  default: ({
    selectedTaskID,
    defaultExpertID,
    projectID,
    allTasks,
    onSelectTask,
  }: {
    selectedTaskID: string | null;
    defaultExpertID: string | null;
    projectID: string | null;
    allTasks: boolean;
    onSelectTask: (id: string | null) => void;
  }) => (
    <div>
      <output data-testid="selection">
        {JSON.stringify({
          selectedTaskID,
          defaultExpertID,
          projectID,
          allTasks,
        })}
      </output>
      <button onClick={() => onSelectTask("next_task")}>Open next</button>
    </div>
  ),
}));
const api = vi.mocked(projectTasksApi, true);
const projects = vi.mocked(projectsApi, true);
const project: Project = {
  project_id: "project_a",
  name: "A",
  description: "",
  directory: null,
  client_directory: null,
  instructions: "",
  skills: [],
  mcp_servers: [],
  plugins: [],
  subagents: [],
  memory_enabled: true,
  team_enabled: false,
  archived: false,
  revision: 4,
  created_at: "2026-10-23T00:00:00Z",
  updated_at: "2026-10-23T00:00:00Z",
};
beforeEach(() => {
  vi.clearAllMocks();
  projects.list.mockResolvedValue([project]);
});
function mount(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/conversations" element={<UnifiedConversations />} />
        <Route
          path="/conversations/:taskID"
          element={<UnifiedConversations />}
        />
      </Routes>
    </MemoryRouter>,
  );
}

it("resolves a stable Task URL from its server-owned project instead of a query override", async () => {
  api.get.mockResolvedValue({ project_id: "project_a" } as Awaited<
    ReturnType<typeof projectTasksApi.get>
  >);
  mount("/conversations/task_a?project_id=wrong&expert_id=expert_a");
  const selection = await screen.findByTestId("selection");
  expect(JSON.parse(selection.textContent!)).toEqual({
    selectedTaskID: "task_a",
    defaultExpertID: "expert_a",
    projectID: "project_a",
    allTasks: true,
  });
  expect(
    screen.getByRole("combobox", { name: "projects.tasks.project" }),
  ).toBeDisabled();
  api.get.mockResolvedValue({ project_id: null } as Awaited<
    ReturnType<typeof projectTasksApi.get>
  >);
  await userEvent.click(screen.getByRole("button", { name: "Open next" }));
  await waitFor(() =>
    expect(JSON.parse(screen.getByTestId("selection").textContent!)).toEqual({
      selectedTaskID: "next_task",
      defaultExpertID: null,
      projectID: null,
      allTasks: true,
    }),
  );
});

it("starts without a Task or expert and keeps the project optional", async () => {
  mount("/conversations");
  expect(
    JSON.parse((await screen.findByTestId("selection")).textContent!),
  ).toEqual({
    selectedTaskID: null,
    defaultExpertID: null,
    projectID: null,
    allTasks: true,
  });
  expect(api.get).not.toHaveBeenCalled();
  expect(
    screen.getByRole("combobox", { name: "projects.tasks.project" }),
  ).toBeEnabled();
});
