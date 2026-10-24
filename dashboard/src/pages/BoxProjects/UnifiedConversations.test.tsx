import { act, render, screen, waitFor, within } from "@testing-library/react";
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
vi.mock("../../hooks/useServerTimezone", () => ({
  useServerTimezone: () => "Asia/Shanghai",
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
    navigation,
    overview,
    listScope,
    onWorkbenchDirtyChange,
  }: {
    selectedTaskID: string | null;
    defaultExpertID: string | null;
    projectID: string | null;
    allTasks: boolean;
    onSelectTask: (id: string | null) => void;
    navigation?: ReactNode;
    overview?: ReactNode;
    listScope?: object;
    onWorkbenchDirtyChange?: (dirty: boolean) => void;
  }) => (
    <div>
      {navigation}
      {overview}
      <output data-testid="list-scope">{JSON.stringify(listScope)}</output>
      <output data-testid="selection">
        {JSON.stringify({
          selectedTaskID,
          defaultExpertID,
          projectID,
          allTasks,
        })}
      </output>
      <button onClick={() => onSelectTask("next_task")}>Open next</button>
      <button onClick={() => onSelectTask(null)}>New conversation</button>
      <button onClick={() => onWorkbenchDirtyChange?.(true)}>
        Unsaved workbench
      </button>
      <button
        onClick={() => {
          onWorkbenchDirtyChange?.(false);
          onSelectTask(null);
        }}
      >
        Confirmed task deletion
      </button>
      <input aria-label="Local composer state" />
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

it("keeps project navigation reachable and opens its overview inside the workspace", async () => {
  const user = userEvent.setup();
  mount("/conversations");
  await screen.findByTestId("selection");
  expect(
    screen.queryByRole("button", { name: "projects.workspace.projectMenu" }),
  ).toBeNull();
  expect(
    screen.getAllByRole("navigation", {
      name: "projects.workspace.navigation",
    }),
  ).toHaveLength(1);
  await user.click(screen.getByRole("button", { name: "A", exact: true }));
  expect(
    await screen.findByRole("region", { name: "projects.workspace.overview" }),
  ).toBeTruthy();
  expect(JSON.parse(screen.getByTestId("list-scope").textContent!)).toEqual({
    projectID: "project_a",
  });
  expect(
    screen.getByRole("combobox", { name: "projects.tasks.project" }),
  ).toBeDisabled();
  await user.click(
    screen.getByRole("button", { name: "projects.workspace.newInProject" }),
  );
  await waitFor(() =>
    expect(
      screen.queryByRole("region", { name: "projects.workspace.overview" }),
    ).toBeNull(),
  );
  expect(
    JSON.parse(screen.getByTestId("selection").textContent!),
  ).toMatchObject({ selectedTaskID: null, projectID: "project_a" });
  expect(
    screen.getByRole("combobox", { name: "projects.tasks.project" }),
  ).toBeEnabled();
});

it("keeps a fallback project navigator reachable when the conversation cannot load", async () => {
  api.get.mockRejectedValue(new Error("offline"));
  const user = userEvent.setup();
  mount("/conversations/task_a");
  await screen.findByRole("button", { name: "projects.refresh" });
  await user.click(
    screen.getByRole("button", { name: "projects.workspace.projectMenu" }),
  );
  await waitFor(() =>
    expect(
      screen.getByRole("navigation", {
        name: "projects.workspace.navigation",
      }),
    ).toBeVisible(),
  );
  expect(
    screen.getByRole("button", { name: "projects.workspace.all" }),
  ).toBeVisible();
});

it("changes only the list scope when browsing pinned and standalone conversations", async () => {
  api.get.mockResolvedValue({ project_id: "project_a" } as Awaited<
    ReturnType<typeof projectTasksApi.get>
  >);
  const user = userEvent.setup();
  mount("/conversations/task_a");
  await screen.findByTestId("selection");
  await user.click(
    screen.getByRole("button", { name: "projects.workspace.pinned" }),
  );
  await waitFor(() =>
    expect(JSON.parse(screen.getByTestId("list-scope").textContent!)).toEqual({
      pinned: true,
    }),
  );
  await user.click(
    screen.getByRole("button", { name: "projects.workspace.standalone" }),
  );
  await waitFor(() =>
    expect(JSON.parse(screen.getByTestId("list-scope").textContent!)).toEqual({
      projectID: null,
    }),
  );
  expect(
    JSON.parse(screen.getByTestId("selection").textContent!),
  ).toMatchObject({ selectedTaskID: "task_a", projectID: "project_a" });
});

it.each(["all", "standalone", "active", "pinned"])(
  "starts a project-free draft from the %s scope despite the selected Task project",
  async (scope) => {
    api.get.mockResolvedValue({ project_id: "project_a" } as Awaited<
      ReturnType<typeof projectTasksApi.get>
    >);
    const user = userEvent.setup();
    mount("/conversations/task_a?workspace_scope=" + scope);
    await screen.findByTestId("selection");
    await user.click(screen.getByRole("button", { name: "New conversation" }));
    await waitFor(() =>
      expect(
        JSON.parse(screen.getByTestId("selection").textContent!),
      ).toMatchObject({ selectedTaskID: null, projectID: null }),
    );
  },
);

it("clears the project overview context when entering a global scope", async () => {
  const user = userEvent.setup();
  mount(
    "/conversations?workspace_scope=project&scope_project=project_a&project_id=project_a&view=project",
  );
  await screen.findByRole("region", { name: "projects.workspace.overview" });
  await user.click(
    screen.getByRole("button", { name: "projects.workspace.all" }),
  );
  await waitFor(() =>
    expect(
      JSON.parse(screen.getByTestId("selection").textContent!),
    ).toMatchObject({ selectedTaskID: null, projectID: null }),
  );
  expect(
    screen.queryByRole("region", { name: "projects.workspace.overview" }),
  ).toBeNull();
});

it("keeps the conversation mounted and inert while resolving the next Task project", async () => {
  let resolveNext!: (
    value: Awaited<ReturnType<typeof projectTasksApi.get>>,
  ) => void;
  api.get.mockImplementation(async (id) =>
    id === "next_task"
      ? new Promise((resolve) => {
          resolveNext = resolve;
        })
      : ({ project_id: "project_a" } as Awaited<
          ReturnType<typeof projectTasksApi.get>
        >),
  );
  const user = userEvent.setup();
  mount("/conversations/task_a");
  const localInput = await screen.findByRole("textbox", {
    name: "Local composer state",
  });
  await user.type(localInput, "keep mounted");
  await user.click(screen.getByRole("button", { name: "Open next" }));
  await waitFor(() => expect(api.get).toHaveBeenCalledWith("next_task"));
  expect(screen.getByLabelText("Local composer state")).toBe(localInput);
  expect(localInput).not.toBeVisible();
  expect(
    JSON.parse(screen.getByTestId("selection").textContent!),
  ).toMatchObject({ selectedTaskID: "task_a", projectID: "project_a" });
  await act(async () =>
    resolveNext({ project_id: null } as Awaited<
      ReturnType<typeof projectTasksApi.get>
    >),
  );
  await waitFor(() => expect(localInput).toBeVisible());
  expect(screen.getByLabelText("Local composer state")).toBe(localInput);
  expect(localInput).toHaveValue("keep mounted");
  expect(
    JSON.parse(screen.getByTestId("selection").textContent!),
  ).toMatchObject({ selectedTaskID: "next_task", projectID: null });
});

it("confirms discarding unsaved workbench edits before changing the Task", async () => {
  api.get.mockResolvedValue({ project_id: "project_a" } as Awaited<
    ReturnType<typeof projectTasksApi.get>
  >);
  const user = userEvent.setup();
  mount("/conversations/task_a");
  await screen.findByTestId("selection");
  await user.click(screen.getByRole("button", { name: "Unsaved workbench" }));
  await user.click(screen.getByRole("button", { name: "Open next" }));
  let dialog = await screen.findByRole("dialog", {
    name: "projects.tasks.workbench.unsaved",
  });
  expect(api.get).not.toHaveBeenCalledWith("next_task");
  await user.click(
    within(dialog).getByRole("button", { name: "common.cancel" }),
  );
  expect(
    JSON.parse(screen.getByTestId("selection").textContent!),
  ).toMatchObject({ selectedTaskID: "task_a" });
  await user.click(screen.getByRole("button", { name: "Open next" }));
  dialog = await screen.findByRole("dialog", {
    name: "projects.tasks.workbench.unsaved",
  });
  await user.click(
    within(dialog).getByRole("button", {
      name: "projects.tasks.workbench.discard",
    }),
  );
  await waitFor(() =>
    expect(
      JSON.parse(screen.getByTestId("selection").textContent!),
    ).toMatchObject({ selectedTaskID: "next_task" }),
  );
});

it("does not ask to leave again after confirmed deletion clears dirty state", async () => {
  api.get.mockResolvedValue({ project_id: "project_a" } as Awaited<
    ReturnType<typeof projectTasksApi.get>
  >);
  const user = userEvent.setup();
  mount("/conversations/task_a");
  await screen.findByTestId("selection");
  await user.click(screen.getByRole("button", { name: "Unsaved workbench" }));
  await user.click(
    screen.getByRole("button", { name: "Confirmed task deletion" }),
  );
  await waitFor(() =>
    expect(
      JSON.parse(screen.getByTestId("selection").textContent!),
    ).toMatchObject({ selectedTaskID: null, projectID: null }),
  );
  expect(
    screen.queryByRole("dialog", { name: "projects.tasks.workbench.unsaved" }),
  ).toBeNull();
});
