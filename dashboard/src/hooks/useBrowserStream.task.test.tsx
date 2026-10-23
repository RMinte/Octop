import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useBrowserStream } from "./useBrowserStream";
import { taskWorkbenchApi } from "../api/modules/taskWorkbench";
vi.mock("../api/modules/taskWorkbench", () => ({
  taskWorkbenchApi: {
    browserStreamUrl: vi.fn(
      () =>
        "wss://octop.test/api/project-tasks/task_a/browser-stream/ws?expected_scope_revision=scope-3",
    ),
  },
}));
vi.mock("../api/request", () => ({ getAuthToken: () => "token" }));
const sockets: FakeSocket[] = [];
class FakeSocket {
  static CLOSING = 2;
  static OPEN = 1;
  readyState = 0;
  url: string;
  onopen: (() => void) | null = null;
  onmessage = null;
  onerror = null;
  onclose = null;
  send = vi.fn();
  close = vi.fn();
  constructor(url: string) {
    this.url = url;
    sockets.push(this);
  }
}
afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
  sockets.length = 0;
});
describe("Task browser transport", () => {
  it("ignores a legacy agent session/profile when explicitly attached to a Task", () => {
    vi.stubGlobal("WebSocket", FakeSocket);
    const { result, unmount } = renderHook(useBrowserStream);
    act(() =>
      result.current.connect(
        "",
        1280,
        800,
        { onFrame: vi.fn() },
        {
          taskID: "task_a",
          scopeRevision: "scope-3",
          sessionId: "old-agent-session",
          bridgeConnectionId: "old-bridge",
        },
      ),
    );
    expect(taskWorkbenchApi.browserStreamUrl).toHaveBeenCalledWith(
      "task_a",
      1280,
      800,
      "scope-3",
    );
    act(() => sockets[0].onopen?.());
    const start = JSON.parse(sockets[0].send.mock.calls[0][0]);
    expect(start).not.toHaveProperty("session_id");
    expect(start).not.toHaveProperty("profile");
    expect(sockets[0].url).toContain("/project-tasks/task_a/");
    unmount();
    expect(sockets[0].close).toHaveBeenCalled();
  });
});
