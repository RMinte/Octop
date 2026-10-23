import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  taskWorkbenchApi,
  type TaskWorkbenchScope,
} from "../../api/modules/taskWorkbench";
import TaskFiles from "./TaskFiles";
vi.mock("../../api/modules/taskWorkbench", () => ({
  taskWorkbenchApi: {
    tree: vi.fn(),
    file: vi.fn(),
    save: vi.fn(),
    mediaUrl: () => "/api/project-tasks/task_a/workspace/media",
    mediaPath: (_id: string, file: string) =>
      `/project-tasks/task_a/workspace/media?path=${encodeURIComponent(
        file,
      )}&expected_scope_revision=scope-3`,
    mkdir: vi.fn(),
    move: vi.fn(),
    delete: vi.fn(),
  },
}));
vi.mock("../Agent/Workspace/components/FileViewer", () => ({
  default: ({
    value,
    onChange,
    editMode,
    fileLoading,
    fetchUrl,
  }: {
    value: string;
    onChange: (value: string) => void;
    editMode: boolean;
    fileLoading: boolean;
    fetchUrl: string;
  }) => (
    <textarea
      aria-label="file-editor"
      data-preview-url={fetchUrl}
      disabled={!editMode || fileLoading}
      value={value}
      onChange={(event) => onChange(event.target.value)}
    />
  ),
}));
vi.mock("react-i18next", () => {
  const t = (key: string) => key;
  return { useTranslation: () => ({ t }) };
});
const api = vi.mocked(taskWorkbenchApi);
const scope: TaskWorkbenchScope = {
  task_id: "task_a",
  scope_revision: "scope-3",
  server_directory: "/project",
  client_directory: null,
  capabilities: {
    files: true,
    browser: true,
    terminal: true,
    recording: true,
    tool_ui: true,
  },
  read_only: false,
};
beforeEach(() => {
  vi.clearAllMocks();
  api.tree.mockResolvedValue([
    {
      name: "notes.txt",
      path: "notes.txt",
      is_dir: false,
      size: 3,
      children: [],
      modified_time: "",
    },
  ]);
  api.file.mockResolvedValue({
    name: "notes.txt",
    path: "notes.txt",
    content: "old",
    size: 3,
    revision: "file-1",
  });
  api.save.mockResolvedValue({
    name: "notes.txt",
    path: "notes.txt",
    content: "changed",
    size: 7,
    revision: "file-2",
  });
});
describe("Task files", () => {
  it.each(["photo.png", "report.pdf"])(
    "forwards the unprefixed Task preview API path for %s",
    async (file) => {
      api.tree.mockResolvedValue([
        {
          path: file,
          name: file,
          is_dir: false,
          size: 3,
          children: [],
          modified_time: "",
        },
      ]);
      render(<TaskFiles scope={scope} />);
      fireEvent.click(await screen.findByRole("button", { name: file }));
      expect(
        screen.getByRole("textbox", { name: "file-editor" }),
      ).toHaveAttribute(
        "data-preview-url",
        `/project-tasks/task_a/workspace/media?path=${file}&expected_scope_revision=scope-3`,
      );
      expect(api.file).not.toHaveBeenCalled();
    },
  );
  it("sends the read content revision and refreshes it after each save", async () => {
    render(<TaskFiles scope={scope} />);
    fireEvent.click(await screen.findByRole("button", { name: "notes.txt" }));
    await waitFor(() =>
      expect(screen.getByRole("textbox", { name: "file-editor" })).toHaveValue(
        "old",
      ),
    );
    fireEvent.click(
      screen.getByRole("button", { name: "projects.tasks.workbench.edit" }),
    );
    fireEvent.change(screen.getByRole("textbox", { name: "file-editor" }), {
      target: { value: "changed" },
    });
    fireEvent.click(screen.getByRole("button", { name: "projects.save" }));
    await waitFor(() =>
      expect(api.save).toHaveBeenCalledWith(
        "task_a",
        "notes.txt",
        "changed",
        "scope-3",
        "file-1",
      ),
    );
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "projects.save" }),
      ).toBeDisabled(),
    );
    fireEvent.change(screen.getByRole("textbox", { name: "file-editor" }), {
      target: { value: "changed again" },
    });
    fireEvent.click(screen.getByRole("button", { name: "projects.save" }));
    await waitFor(() =>
      expect(api.save).toHaveBeenLastCalledWith(
        "task_a",
        "notes.txt",
        "changed again",
        "scope-3",
        "file-2",
      ),
    );
  });
  it("keeps the user's draft when a model changed the file meanwhile", async () => {
    api.save.mockRejectedValue(
      new Error(
        'Request failed: 409 - {"code":"TASK_REVISION_CONFLICT","message":"file changed"}',
      ),
    );
    render(<TaskFiles scope={scope} />);
    fireEvent.click(await screen.findByRole("button", { name: "notes.txt" }));
    await waitFor(() =>
      expect(screen.getByRole("textbox", { name: "file-editor" })).toHaveValue(
        "old",
      ),
    );
    fireEvent.click(
      screen.getByRole("button", { name: "projects.tasks.workbench.edit" }),
    );
    fireEvent.change(screen.getByRole("textbox", { name: "file-editor" }), {
      target: { value: "my draft" },
    });
    fireEvent.click(screen.getByRole("button", { name: "projects.save" }));
    await screen.findByText("file changed");
    expect(screen.getByRole("textbox", { name: "file-editor" })).toHaveValue(
      "my draft",
    );
  });
});
