import { TaskHostProvider } from "../../context/TaskHostContext";
import { act, render, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import {
  projectTasksApi,
  type TaskDetail,
  type TaskEvent,
} from "../../api/modules/projects";
import {
  TaskMessageQueueProvider,
  useTaskMessageQueue,
  type TaskMessageQueue,
} from "./useTaskMessageQueue";

const remoteApi = vi.hoisted(() => ({
  get: vi.fn(),
  events: vi.fn(),
  send: vi.fn(),
}));
vi.mock("../../api/modules/projects", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../api/modules/projects")>()),
  projectTasksApi: { get: vi.fn(), events: vi.fn(), send: vi.fn() },
  createProjectTasksApi: () => remoteApi,
}));
vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));
const api = vi.mocked(projectTasksApi, true);
const task: TaskDetail = {
  task_id: "task_a",
  project_id: null,
  title: "A",
  archived: false,
  status: "running",
  active_turn_id: "turn_a",
  list_revision: 1,
  approval_policy: {
    mode: "ask_for_approval",
    revision: 1,
    updated_at: "2026-10-23T00:00:00Z",
  },
  web_search_enabled: false,
  created_at: "2026-10-23T00:00:00Z",
  updated_at: "2026-10-23T00:00:00Z",
  turns: [],
  next_page_cursor: null,
  event_cursor: "cursor_a",
};
beforeEach(() => vi.clearAllMocks());

it("retains task drafts and pending messages when the page consumer changes", async () => {
  let current!: TaskMessageQueue;
  let receive!: (event: TaskEvent) => void;
  let eventSignal!: AbortSignal;
  api.get.mockResolvedValue(task);
  api.events.mockImplementation((_id, _cursor, callback, signal) => {
    receive = callback;
    eventSignal = signal;
    return new Promise<void>((resolve) =>
      signal.addEventListener("abort", () => resolve(), { once: true }),
    );
  });
  api.send.mockResolvedValue({
    ...task,
    list_revision: 3,
    active_turn_id: "turn_b",
  });
  function Consumer() {
    current = useTaskMessageQueue();
    return null;
  }
  const page = (key: string) => (
    <TaskMessageQueueProvider>
      <Consumer key={key} />
    </TaskMessageQueueProvider>
  );
  const view = render(page("project"));
  act(() => {
    current.drafts.current.set("task_a", "unsent draft");
    current.enqueue(
      "task_a",
      null,
      { request_id: "request_b", prompt: "next" },
      [],
    );
  });
  await waitFor(() => expect(api.events).toHaveBeenCalledOnce());
  view.rerender(page("detail"));
  expect(current.drafts.current.get("task_a")).toBe("unsent draft");
  expect(current.queues.task_a.items).toHaveLength(1);
  expect(eventSignal.aborted).toBe(false);
  api.get.mockResolvedValue({
    ...task,
    list_revision: 2,
    status: "completed",
    active_turn_id: null,
  });
  await act(async () =>
    receive({
      type: "snapshot",
      data: {
        ...task,
        list_revision: 2,
        status: "completed",
        active_turn_id: null,
      },
    }),
  );
  await waitFor(() =>
    expect(api.send).toHaveBeenCalledWith("task_a", {
      request_id: "request_b",
      prompt: "next",
    }),
  );
});

it("keeps peer drafts, queue IDs and delivery separate from a same-ID local task", async () => {
  let local!: TaskMessageQueue, remote!: TaskMessageQueue;
  api.get.mockResolvedValue(task);
  remoteApi.get.mockResolvedValue(task);
  const forever = (
    _id: unknown,
    _cursor: unknown,
    _cb: unknown,
    signal: AbortSignal,
  ) =>
    new Promise<void>((resolve) =>
      signal.addEventListener("abort", () => resolve(), { once: true }),
    );
  api.events.mockImplementation(forever);
  remoteApi.events.mockImplementation(forever);
  function Local() {
    local = useTaskMessageQueue();
    return null;
  }
  function Remote() {
    remote = useTaskMessageQueue();
    return null;
  }
  render(
    <TaskMessageQueueProvider>
      <Local />
      <TaskHostProvider hostID="peer">
        <Remote />
      </TaskHostProvider>
    </TaskMessageQueueProvider>,
  );
  act(() => {
    local.drafts.current.set("task_a", "local draft");
    remote.drafts.current.set("task_a", "remote draft");
    remote.enqueue(
      "task_a",
      null,
      { request_id: "remote-msg", prompt: "remote prompt" },
      [],
    );
  });
  await waitFor(() => expect(remoteApi.events).toHaveBeenCalledOnce());
  expect(remoteApi.get).toHaveBeenCalledWith("task_a");
  expect(local.drafts.current.get("task_a")).toBe("local draft");
  expect(remote.drafts.current.get("task_a")).toBe("remote draft");
  expect(Object.keys(local.queues)).toEqual([]);
  expect(remote.queues.task_a.items[0].id).toBe("remote-msg");
  expect(api.get).not.toHaveBeenCalled();
  expect(api.send).not.toHaveBeenCalled();
});
