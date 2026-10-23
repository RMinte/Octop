import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { Modal } from "antd";
import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import {
  taskWorkbenchApi,
  type TaskWorkbenchScope,
} from "../../api/modules/taskWorkbench";
import TaskTerminal from "./TaskTerminal";
vi.mock("../../api/modules/taskWorkbench", () => ({
  taskWorkbenchApi: {
    terminalUrl: vi.fn(
      (id, shell, scope) =>
        `wss://test/api/project-tasks/${id}/terminal/ws?session_id=${shell}&expected_scope_revision=${scope}`,
    ),
    closeTerminal: vi.fn().mockResolvedValue({}),
  },
}));
vi.mock("../Control/Terminal/components/TerminalView", () => ({
  default: ({ onData }: { onData: (data: string) => void }) => (
    <button onClick={() => onData("pwd\n")}>type command</button>
  ),
}));
vi.mock("react-i18next", () => {
  const t = (key: string) => key;
  return { useTranslation: () => ({ t }) };
});
let sockets: FakeSocket[] = [];
class FakeSocket {
  static OPEN = 1;
  readyState = 1;
  onopen: (() => void) | null = null;
  onmessage = null;
  onclose = null;
  onerror = null;
  send = vi.fn();
  close = vi.fn();
  constructor(public url: string) {
    sockets.push(this);
  }
}
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
beforeEach(() => {
  sessionStorage.clear();
  sockets = [];
  vi.clearAllMocks();
  vi.stubGlobal("WebSocket", FakeSocket);
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});
describe("Task terminal lifecycle", () => {
  it("detaches on panel close and reconnects the same shell only within the same Task scope", async () => {
    const view = render(<TaskTerminal scope={scope} />);
    fireEvent.click(
      screen.getByRole("button", {
        name: "projects.tasks.workbench.openTerminal",
      }),
    );
    await waitFor(() => expect(sockets).toHaveLength(1));
    act(() => sockets[0].onopen?.());
    fireEvent.click(screen.getByRole("button", { name: "type command" }));
    expect(sockets[0].send).toHaveBeenCalledWith(
      JSON.stringify({ type: "input", data: "pwd\n" }),
    );
    view.unmount();
    expect(sockets[0].close).toHaveBeenCalled();
    expect(taskWorkbenchApi.closeTerminal).not.toHaveBeenCalled();
    render(<TaskTerminal scope={scope} />);
    await waitFor(() => expect(sockets).toHaveLength(2));
    expect(sockets[1].url).toBe(sockets[0].url);
    expect(sockets[1].url).toContain("/project-tasks/task_a/");
  });
  it("requires explicit terminal termination and sends the current directory revision", async () => {
    vi.spyOn(Modal, "confirm").mockImplementation((options) => {
      void options.onOk?.();
      return { destroy: vi.fn(), update: vi.fn() };
    });
    render(<TaskTerminal scope={scope} />);
    fireEvent.click(
      screen.getByRole("button", {
        name: "projects.tasks.workbench.openTerminal",
      }),
    );
    await waitFor(() => expect(sockets).toHaveLength(1));
    const sessionID = new URL(sockets[0].url).searchParams.get("session_id");
    fireEvent.click(
      screen.getByRole("button", {
        name: "projects.tasks.workbench.endTerminal",
      }),
    );
    await waitFor(() =>
      expect(taskWorkbenchApi.closeTerminal).toHaveBeenCalledWith(
        "task_a",
        sessionID,
        "scope-3",
      ),
    );
  });
});
