import { beforeEach, describe, expect, it, vi } from "vitest";
import { request, requestUpload } from "../request";
import { getApiUrl } from "../config";
import { taskToolRequest, taskWorkbenchApi } from "./taskWorkbench";
vi.mock("../config", () => ({
  getApiUrl: (path: string) => `/api${path}`,
  getWsUrl: (path: string) => `wss://octop.test/api${path}`,
}));
vi.mock("../request", () => ({
  getAuthToken: () => "token",
  request: vi.fn().mockResolvedValue({}),
  requestBlob: vi.fn().mockResolvedValue(new Blob()),
  requestUpload: vi.fn().mockResolvedValue({}),
}));
const api = vi.mocked(request);
const context = {
  taskID: "task_a",
  turnID: "turn_a",
  callID: "call_a",
  scopeRevision: "scope-3",
  readOnly: false,
};
beforeEach(() => vi.clearAllMocks());
describe("Task workbench scope", () => {
  it("passes API-relative preview paths so media and document viewers prefix /api exactly once", () => {
    const path = taskWorkbenchApi.mediaPath(
      "task_a",
      "docs/a b.pdf",
      "scope-3",
    );
    expect(path).toBe(
      "/project-tasks/task_a/workspace/media?path=docs%2Fa+b.pdf&expected_scope_revision=scope-3",
    );
    expect(getApiUrl(path)).toBe(
      "/api/project-tasks/task_a/workspace/media?path=docs%2Fa+b.pdf&expected_scope_revision=scope-3",
    );
    expect(taskWorkbenchApi.mediaUrl("task_a", "docs/a b.pdf", "scope-3")).toBe(
      "/api/project-tasks/task_a/workspace/media?path=docs%2Fa+b.pdf&expected_scope_revision=scope-3&access_token=token",
    );
  });
  it("uses the Task WS with scope revision and never a selected expert profile", () => {
    const browser = new URL(
      taskWorkbenchApi.browserStreamUrl("task/a", 1280, 800, "scope-3"),
    );
    expect(browser.pathname).toBe(
      "/api/project-tasks/task%2Fa/browser-stream/ws",
    );
    expect(browser.searchParams.get("expected_scope_revision")).toBe("scope-3");
    expect(browser.searchParams.has("session_id")).toBe(false);
    expect(browser.searchParams.has("profile")).toBe(false);
    const media = new URL(
      taskWorkbenchApi.mediaUrl("task_a", "notes.txt", "scope-3"),
      "https://octop.test",
    );
    expect(media.searchParams.get("access_token")).toBe("token");
    expect(media.searchParams.has("token")).toBe(false);
    const terminal = new URL(
      taskWorkbenchApi.terminalUrl("task_a", "shell_a", "scope-3"),
    );
    expect(terminal.searchParams.get("session_id")).toBe("shell_a");
    expect(terminal.searchParams.get("expected_scope_revision")).toBe(
      "scope-3",
    );
  });
  it("pins file upload and deletion to the current working directory revision", async () => {
    const file = new File(["hello"], "notes.txt");
    await taskWorkbenchApi.upload("task_a", "folder", file, "scope-3");
    expect(vi.mocked(requestUpload).mock.calls[0][0]).toBe(
      "/project-tasks/task_a/workspace/upload?path=folder%2Fnotes.txt",
    );
    expect(
      (vi.mocked(requestUpload).mock.calls[0][1] as FormData).get(
        "expected_scope_revision",
      ),
    ).toBe("scope-3");
    await taskWorkbenchApi.closeTerminal("task_a", "shell_a", "scope-3");
    expect(api).toHaveBeenCalledWith(
      "/project-tasks/task_a/terminal/sessions/shell_a?expected_scope_revision=scope-3",
      { method: "DELETE" },
    );
  });
  it("allows only this Task workbench and replaces caller revision claims", async () => {
    await taskToolRequest(context, "/project-tasks/task_a/workspace/file", {
      method: "PUT",
      body: JSON.stringify({
        path: "notes.txt",
        content: "hello",
        expected_scope_revision: "stale",
      }),
    });
    expect(api).toHaveBeenCalledWith(
      "/project-tasks/task_a/tool-ui/turn_a/call_a/request",
      expect.objectContaining({ method: "POST", body: expect.any(String) }),
    );
    const written = JSON.parse(api.mock.calls[0][1]?.body as string);
    expect(written.expected_scope_revision).toBe("scope-3");
    expect(written.path).toBe("/workspace/file");
    await taskToolRequest(
      context,
      "/workspace/file?path=notes.txt&expected_scope_revision=stale",
    );
    const read = JSON.parse(api.mock.calls[1][1]?.body as string);
    expect(read.path).toBe("/workspace/file?path=notes.txt");
    expect(read.expected_scope_revision).toBe("scope-3");
  });
  it.each([
    "/agents/expert_a/workspace/file",
    "/project-tasks/task_b/workspace/file",
    "/project-tasks/task_a/messages",
    "/project-tasks/task_a/../task_b/workspace/file",
    "/project-tasks/task_a/workspace/%2e%2e/../../agents/a/workspace/file",
  ])("rejects an unscoped plugin request: %s", async (path) => {
    await expect(taskToolRequest(context, path)).rejects.toThrow(
      "TASK_TOOL_REQUEST_UNSUPPORTED",
    );
    expect(api).not.toHaveBeenCalled();
  });
  it("prevents writes from read-only history while allowing file inspection", async () => {
    await expect(
      taskToolRequest(
        { ...context, readOnly: true },
        "/project-tasks/task_a/workspace/file",
        { method: "PUT", body: "{}" },
      ),
    ).rejects.toThrow("TASK_TOOL_REQUEST_UNSUPPORTED");
    await taskToolRequest(
      { ...context, readOnly: true },
      "/project-tasks/task_a/workspace/file?path=notes.txt",
    );
    expect(api).toHaveBeenCalledTimes(1);
  });
});
