import { useEffect } from "react";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Modal } from "antd";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import {
  taskWorkbenchApi,
  type TaskWorkbenchScope,
} from "../../api/modules/taskWorkbench";
import type { TaskDetail } from "../../api/modules/projects";
import TaskWorkbench from "./TaskWorkbench";

const mounted = vi.hoisted(() => ({ files: 0, terminal: 0 }));
vi.mock("../../api/modules/taskWorkbench", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../api/modules/taskWorkbench")>()),
  taskWorkbenchApi: { scope: vi.fn() },
}));
vi.mock("./TaskFiles", () => ({
  default: function MockTaskFiles({
    onDirtyChange,
  }: {
    onDirtyChange: (value: boolean) => void;
  }) {
    useEffect(() => {
      mounted.files += 1;
      return () => onDirtyChange(false);
    }, [onDirtyChange]);
    return <button onClick={() => onDirtyChange(true)}>Edit file</button>;
  },
}));
vi.mock("./TaskTerminal", () => ({
  default: function MockTaskTerminal() {
    useEffect(() => {
      mounted.terminal += 1;
    }, []);
    return <div>Task PTY</div>;
  },
}));
vi.mock("../../components/BrowserWorkspace", () => ({
  default: () => <div>Task browser</div>,
}));
vi.mock("./TaskSkillRecording", () => ({
  default: () => <div>Task recording</div>,
}));
vi.mock("./TaskToolUi", () => ({ default: () => <div>Task tools</div> }));
vi.mock("react-i18next", () => {
  const t = (key: string) => key;
  return { useTranslation: () => ({ t }) };
});
const task = {
  task_id: "task",
  project_id: "project",
  configuration_revision: 1,
  archived: false,
} as TaskDetail;
const scope: TaskWorkbenchScope = {
  task_id: "task",
  scope_revision: "scope-1",
  read_only: false,
  server_directory: "/project",
  client_directory: null,
  capabilities: {
    files: true,
    browser: true,
    terminal: true,
    recording: true,
    tool_ui: true,
  },
};
let confirm: ReturnType<typeof vi.spyOn<typeof Modal, "confirm">>;
beforeEach(() => {
  vi.clearAllMocks();
  mounted.files = 0;
  mounted.terminal = 0;
  vi.mocked(taskWorkbenchApi.scope).mockResolvedValue(scope);
  confirm = vi
    .spyOn(Modal, "confirm")
    .mockReturnValue({ destroy: vi.fn(), update: vi.fn() });
});
afterEach(() => confirm.mockRestore());

async function open() {
  fireEvent.click(
    screen.getByRole("button", { name: "projects.tasks.workbench.title" }),
  );
  await screen.findByRole("button", { name: "Edit file" });
}

it("docks the real capability tabs and applies launcher styling only to its button", async () => {
  const view = render(
    <TaskWorkbench task={task} docked launcherClassName="launcher" />,
  );
  expect(
    view.container.querySelector("[data-task-workbench-docked]"),
  ).not.toBeVisible();
  await open();
  expect(
    view.container.querySelector("[data-task-workbench-docked]"),
  ).toBeVisible();
  expect(
    screen.getByRole("button", { name: "projects.tasks.workbench.title" }),
  ).toHaveClass("launcher");
  for (const name of [
    "files",
    "browser",
    "terminal",
    "skillRecording",
    "tools",
  ]) {
    expect(
      screen.getByRole("tab", { name: `projects.tasks.workbench.${name}` }),
    ).toBeInTheDocument();
  }
  expect(
    screen.getByRole("button", { name: "projects.tasks.workbench.refresh" }),
  ).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "common.close" }));
  expect(
    view.container.querySelector("[data-task-workbench-docked]"),
  ).not.toBeVisible();
});

it("preserves the mounted PTY on same-scope refresh and rebuilds only after a scope revision change", async () => {
  const user = userEvent.setup();
  const view = render(<TaskWorkbench task={task} docked projectRevision={1} />);
  await open();
  await user.click(
    screen.getByRole("tab", { name: "projects.tasks.workbench.terminal" }),
  );
  await screen.findByText("Task PTY");
  expect(mounted.terminal).toBe(1);
  fireEvent.click(
    screen.getByRole("button", { name: "projects.tasks.workbench.refresh" }),
  );
  await waitFor(() => expect(taskWorkbenchApi.scope).toHaveBeenCalledTimes(2));
  expect(mounted.terminal).toBe(1);
  vi.mocked(taskWorkbenchApi.scope).mockResolvedValue({
    ...scope,
    scope_revision: "scope-2",
  });
  view.rerender(<TaskWorkbench task={task} docked projectRevision={2} />);
  await waitFor(() => expect(mounted.terminal).toBe(2));
});

it("keeps dirty files when configuration changes and waits for explicit discard before refreshing", async () => {
  const view = render(<TaskWorkbench task={task} docked projectRevision={1} />);
  await open();
  fireEvent.click(screen.getByRole("button", { name: "Edit file" }));
  view.rerender(<TaskWorkbench task={task} docked projectRevision={2} />);
  expect(
    await screen.findByText("projects.workspace.workbenchScopeChanged"),
  ).toBeVisible();
  expect(taskWorkbenchApi.scope).toHaveBeenCalledTimes(1);
  fireEvent.click(
    screen.getAllByRole("button", {
      name: "projects.tasks.workbench.refresh",
    })[0],
  );
  expect(confirm).toHaveBeenCalledTimes(1);
  expect(taskWorkbenchApi.scope).toHaveBeenCalledTimes(1);
  await act(async () => {
    await confirm.mock.calls[0][0].onOk?.();
  });
  await waitFor(() => expect(taskWorkbenchApi.scope).toHaveBeenCalledTimes(2));
  expect(mounted.files).toBe(2);
});

it("keeps the default Drawer and guards dirty-file tab changes", async () => {
  const user = userEvent.setup();
  const view = render(<TaskWorkbench task={task} />);
  await open();
  expect(
    view.container.querySelector("[data-task-workbench-docked]"),
  ).toBeNull();
  expect(
    screen.getByRole("dialog", { name: "projects.tasks.workbench.title" }),
  ).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Edit file" }));
  await user.click(
    screen.getByRole("tab", { name: "projects.tasks.workbench.terminal" }),
  );
  expect(confirm).toHaveBeenCalledTimes(1);
  expect(screen.queryByText("Task PTY")).not.toBeInTheDocument();
  await act(async () => {
    await confirm.mock.calls[0][0].onOk?.();
  });
  expect(await screen.findByText("Task PTY")).toBeVisible();
});
