import { beforeEach, expect, it, vi } from "vitest";
import { request, requestBlob, requestUpload } from "./request";
import {
  taskApiPath,
  taskRefKey,
  parseTaskRefKey,
  taskTransport,
} from "./taskTransport";
import { createProjectTasksApi } from "./modules/projects";
import { getProjectTaskSocket } from "./modules/projectTaskSocket";
import { createTaskWorkbenchApi } from "./modules/taskWorkbench";
vi.mock("./request", () => ({
  request: vi.fn().mockResolvedValue({}),
  requestBlob: vi.fn().mockResolvedValue(new Blob()),
  requestUpload: vi.fn().mockResolvedValue({}),
  getAuthToken: () => "local-token",
}));
vi.mock("./config", () => ({
  getApiUrl: (path: string) => `/api${path}`,
  getWsUrl: (path: string) => `ws://localhost/api${path}`,
}));
beforeEach(() => vi.clearAllMocks());
it("isolates identical Task IDs by host and keeps wire IDs peer-local", async () => {
  const a = taskRefKey({ host_id: "peer:a", task_id: "same:id" });
  expect(a).not.toBe(taskRefKey({ host_id: "peer:b", task_id: "same:id" }));
  expect(parseTaskRefKey(a)).toEqual({ host_id: "peer:a", task_id: "same:id" });
  await createProjectTasksApi("peer:a").get("same:id");
  expect(request).toHaveBeenCalledWith(
    "/bridge/connections/peer%3Aa/task-api/project-tasks/same%3Aid",
    expect.anything(),
  );
  const command = vi
    .spyOn(getProjectTaskSocket("peer:a"), "command")
    .mockResolvedValue({});
  const body = {
    request_id: "message-0001",
    prompt: "hello",
    expected_configuration_revision: 7,
  };
  await createProjectTasksApi("peer:a").send("same:id", body);
  expect(command).toHaveBeenCalledWith("task.message", {
    task_id: "same:id",
    data: body,
  });
  command.mockRestore();
});
it("routes blob, upload and native workbench sockets through the same host", async () => {
  const api = createTaskWorkbenchApi("peer");
  await api.download("task", "clip.mp4", "scope");
  expect(requestBlob).toHaveBeenCalledWith(
    expect.stringContaining(
      "/bridge/connections/peer/task-api/project-tasks/task/workspace/download-file",
    ),
    undefined,
    undefined,
  );
  const file = new File(["proof"], "sample.txt");
  await api.upload("task", ".", file, "scope");
  expect(requestUpload).toHaveBeenCalledWith(
    expect.stringContaining(
      "/bridge/connections/peer/task-api/project-tasks/task/workspace/upload?path=sample.txt",
    ),
    expect.any(FormData),
    undefined,
    undefined,
  );
  expect(api.terminalUrl("task", "session", "scope")).toContain(
    "/bridge/connections/peer/project-tasks/task/terminal/ws",
  );
  expect(api.mediaPath("task", "clip.mp4", "scope")).toMatch(
    /^\/bridge\/connections\/peer\/task-api\//,
  );
  expect(api.mediaUrl("task", "clip.mp4", "scope")).toContain(
    "access_token=local-token",
  );
});
it("never accepts arbitrary origins as remote API targets", () => {
  expect(() => taskApiPath("peer", "https://outside.invalid/secret")).toThrow();
  expect(taskApiPath("peer", "/api/project-tasks/task")).toBe(
    "/bridge/connections/peer/task-api/project-tasks/task",
  );
  expect(taskTransport(null)).toBeDefined();
});
it("inspects engine capabilities and patches selections only on the selected box", async () => {
  const api = createProjectTasksApi("engine-box");
  const engine = {
    engine_id: "acp" as const,
    connection_id: "codex",
    model_id: null,
  };
  await api.inspectEngine(engine);
  expect(request).toHaveBeenCalledWith(
    "/bridge/connections/engine-box/task-api/project-tasks/engines/inspect",
    expect.objectContaining({ method: "POST", body: JSON.stringify(engine) }),
  );
  await api.update("same-task", {
    engine,
    expected_revision: 2,
    expected_configuration_revision: 4,
  });
  expect(request).toHaveBeenCalledWith(
    "/bridge/connections/engine-box/task-api/project-tasks/same-task",
    expect.objectContaining({ method: "PATCH" }),
  );
});
