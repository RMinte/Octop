import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import TaskToolUi from "./TaskToolUi";
import { pluginsApi, type InstalledPlugin } from "../../api/modules/plugins";
import {
  taskToolRequest,
  type TaskWorkbenchScope,
} from "../../api/modules/taskWorkbench";
import type { TaskDetail } from "../../api/modules/projects";
import type {
  OctopPluginUIHost,
  ToolRenderProps,
} from "../../plugins/toolRenderers/types";
const capture = vi.hoisted(() => ({ host: null as OctopPluginUIHost | null }));
vi.mock("../../api/modules/taskWorkbench", () => ({
  taskToolRequest: vi.fn().mockResolvedValue({ content: "file" }),
}));
vi.mock("../../api/modules/plugins", () => ({
  pluginsApi: { list: vi.fn().mockResolvedValue([{ id: "demo" }]) },
}));
vi.mock("../../plugins/toolRenderers/loader", () => ({
  loadScopedPluginToolUis: async (
    plugins: InstalledPlugin[],
    createHost: (id: string) => OctopPluginUIHost,
  ) => {
    for (const plugin of plugins) {
      const host = createHost(plugin.id);
      capture.host = host;
      host.registerRenderer({
        id: "demo-card",
        component: ({ host: ownHost }: ToolRenderProps) => (
          <button
            data-plugin={plugin.id}
            onClick={() =>
              void ownHost.request("/workspace/file?path=notes.txt")
            }
          >
            read task file
          </button>
        ),
      });
    }
  },
}));
vi.mock("react-i18next", () => {
  const t = (key: string) => key;
  return { useTranslation: () => ({ t, i18n: { language: "zh" } }) };
});
const scope: TaskWorkbenchScope = {
  task_id: "task_a",
  scope_revision: "scope-3",
  server_directory: null,
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
const task = {
  task_id: "task_a",
  turns: [
    {
      turn_id: "turn_a",
      content_blocks: [
        {
          block_id: "block_a",
          type: "tool",
          text: "",
          status: "completed",
          tool_call_id: "call_a",
          tool_name: "demo",
          arguments: {},
          output: { octop_ui: { renderer: "demo-card" }, data: { value: 1 } },
        },
      ],
    },
  ],
} as TaskDetail;
beforeEach(() => {
  vi.clearAllMocks();
  capture.host = null;
  vi.mocked(pluginsApi.list).mockResolvedValue([
    { id: "demo", tools: [{ name: "demo" }] },
  ]);
});
describe("Task tool UI host", () => {
  it("resolves plugin-local renderer ids only within the current tool's owning plugin", async () => {
    vi.mocked(pluginsApi.list).mockResolvedValue([
      { id: "wrong", tools: [{ name: "wrong_tool" }] },
      { id: "demo", tools: [{ name: "demo" }] },
    ]);
    render(<TaskToolUi scope={scope} task={task} onSelectCall={vi.fn()} />);
    expect(
      await screen.findByRole("button", { name: "read task file" }),
    ).toHaveAttribute("data-plugin", "demo");
  });
  it("does not attach another plugin's renderer when the tool has no matching owner", async () => {
    vi.mocked(pluginsApi.list).mockResolvedValue([
      { id: "wrong", tools: [{ name: "wrong_tool" }] },
    ]);
    render(<TaskToolUi scope={scope} task={task} onSelectCall={vi.fn()} />);
    await waitFor(() => expect(pluginsApi.list).toHaveBeenCalled());
    expect(
      screen.queryByRole("button", { name: "read task file" }),
    ).not.toBeInTheDocument();
  });
  it("provides Task and turn context with no implicit expert workspace", async () => {
    render(<TaskToolUi scope={scope} task={task} onSelectCall={vi.fn()} />);
    fireEvent.click(
      await screen.findByRole("button", { name: "read task file" }),
    );
    expect(capture.host?.getToolContext()).toEqual(
      expect.objectContaining({
        agentId: null,
        threadId: null,
        taskId: "task_a",
        turnId: "turn_a",
      }),
    );
    await waitFor(() =>
      expect(taskToolRequest).toHaveBeenCalledWith(
        {
          hostID: null,
          taskID: "task_a",
          turnID: "turn_a",
          callID: "call_a",
          scopeRevision: "scope-3",
          readOnly: false,
        },
        "/workspace/file?path=notes.txt",
        undefined,
      ),
    );
  });
  it("keeps a copied source turn's tool interface read-only", async () => {
    const copied = {
      ...task,
      turns: task.turns.map((turn) => ({ ...turn, history_only: true })),
    };
    render(<TaskToolUi scope={scope} task={copied} onSelectCall={vi.fn()} />);
    fireEvent.click(
      await screen.findByRole("button", { name: "read task file" }),
    );
    await waitFor(() =>
      expect(taskToolRequest).toHaveBeenCalledWith(
        expect.objectContaining({ readOnly: true }),
        "/workspace/file?path=notes.txt",
        undefined,
      ),
    );
  });
});
