import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  projectTasksApi,
  type TaskDetail,
  type TaskEvent,
  type TaskInputAttachment,
  type TaskMessage,
  type TaskStatus,
} from "../../api/modules/projects";
import {
  TASK_QUEUE_MAX_ITEMS,
  useTaskMessageQueue,
} from "./useTaskMessageQueue";

vi.mock("../../api/modules/projects", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../api/modules/projects")>()),
  projectTasksApi: { get: vi.fn(), events: vi.fn(), send: vi.fn() },
}));
vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

const api = vi.mocked(projectTasksApi, true);
const subscriptions = new Map<
  string,
  { receive: (event: TaskEvent) => void; signal: AbortSignal }
>();
const approval = {
  mode: "ask_for_approval" as const,
  revision: 1,
  updated_at: "2026-10-16T00:00:00Z",
};
function detail(
  revision = 1,
  activeID: string | null = "turn_first",
  status: TaskStatus = activeID ? "running" : "completed",
  taskID = "task_a",
  projectID: string | null = "project_a",
  requestID = "request_first",
): TaskDetail {
  return {
    task_id: taskID,
    project_id: projectID,
    title: taskID,
    archived: false,
    status,
    active_turn_id: activeID,
    list_revision: revision,
    web_search_enabled: false,
    approval_policy: approval,
    created_at: "2026-10-16T00:00:00Z",
    updated_at: "2026-10-16T00:00:00Z",
    next_page_cursor: null,
    event_cursor: `cursor_${revision}`,
    turns: [
      {
        sequence: revision,
        turn_id: activeID ?? `turn_${requestID}`,
        request_id: requestID,
        prompt: requestID,
        attachments: [],
        reply: "",
        reasoning: "",
        content_blocks: [],
        content_revision: 0,
        plan: null,
        plan_revision: 0,
        status,
        error_code: "",
        activity: "",
        artifacts: [],
        model_ref: "provider/model",
        options: {},
        web_search_enabled: false,
        retry_of_turn_id: null,
        cancel_requested: false,
        pause_requested: false,
        interactions: [],
        interaction_revision: 0,
        project_revision: 1,
        approval_policy_at_start: approval,
        created_at: "2026-10-16T00:00:00Z",
        updated_at: "2026-10-16T00:00:00Z",
      },
    ],
  };
}
const body = (id: string): TaskMessage => ({ request_id: id, prompt: id });
const apiError = (code: string) =>
  new Error(JSON.stringify({ error: { code } }));
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}
async function snapshot(value: TaskDetail) {
  api.get.mockResolvedValue(value);
  await act(async () => {
    subscriptions
      .get(value.task_id)
      ?.receive({ type: "snapshot", data: value });
  });
}
async function enqueue(
  queue: ReturnType<
    typeof renderHook<ReturnType<typeof useTaskMessageQueue>, void>
  >,
  id = "next_1",
  taskID = "task_a",
  projectID: string | null = "project_a",
) {
  await act(async () => {
    queue.result.current.enqueue(taskID, projectID, body(id), []);
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  subscriptions.clear();
  api.get.mockResolvedValue(detail());
  api.send.mockImplementation((taskID, message) =>
    Promise.resolve(
      detail(
        3,
        `turn_${message.request_id}`,
        "running",
        taskID,
        "project_a",
        message.request_id,
      ),
    ),
  );
  api.events.mockImplementation((taskID, _cursor, receive, signal) => {
    subscriptions.set(taskID, { receive, signal });
    return new Promise<void>((resolve) => {
      signal.addEventListener("abort", () => resolve(), { once: true });
    });
  });
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("任务消息队列", () => {
  it("保存任务配置期间暂停队列派发，释放后核对最新任务再发送", async () => {
    const hook = renderHook(() => useTaskMessageQueue());
    await enqueue(hook);
    act(() => hook.result.current.hold("task_a", true));
    act(() => hook.result.current.hold("task_a", true));
    await snapshot(detail(2, null));
    expect(api.send).not.toHaveBeenCalled();
    act(() => hook.result.current.hold("task_a", false));
    expect(hook.result.current.isHeld("task_a")).toBe(true);
    expect(api.send).not.toHaveBeenCalled();
    api.get.mockResolvedValue({ ...detail(3, null), web_search_enabled: true });
    await act(async () => hook.result.current.hold("task_a", false));
    expect(hook.result.current.isHeld("task_a")).toBe(false);
    await waitFor(() =>
      expect(api.send).toHaveBeenCalledWith("task_a", body("next_1")),
    );
    expect(api.send.mock.calls[0][1]).not.toHaveProperty("web_search_enabled");
  });

  it("逐轮 FIFO 派发，并在服务端确认接收前保留队首", async () => {
    const sent = deferred<TaskDetail>();
    api.send.mockReturnValueOnce(sent.promise);
    const hook = renderHook(() => useTaskMessageQueue());
    await enqueue(hook, "next_1");
    await enqueue(hook, "next_2");
    expect(api.send).not.toHaveBeenCalled();

    await snapshot(detail(2, null));
    expect(api.send).toHaveBeenCalledWith("task_a", body("next_1"));
    expect(hook.result.current.queues.task_a.sendingID).toBe("next_1");
    expect(hook.result.current.queues.task_a.items).toHaveLength(2);
    await act(async () => {
      sent.resolve(
        detail(3, "turn_next_1", "running", "task_a", "project_a", "next_1"),
      );
    });
    expect(
      hook.result.current.queues.task_a.items.map((item) => item.id),
    ).toEqual(["next_2"]);
    expect(hook.result.current.delivered.task_a.active_turn_id).toBe(
      "turn_next_1",
    );
    expect(api.send).toHaveBeenCalledTimes(1);

    await snapshot(
      detail(4, null, "completed", "task_a", "project_a", "next_1"),
    );
    expect(api.send).toHaveBeenCalledTimes(2);
    expect(api.send).toHaveBeenLastCalledWith("task_a", body("next_2"));
    expect(hook.result.current.queues.task_a).toBeUndefined();
    expect(subscriptions.get("task_a")?.signal.aborted).toBe(true);
  });

  it.each<TaskStatus>([
    "queued",
    "running",
    "waiting_approval",
    "waiting_answer",
    "waiting_client_tool",
    "paused",
  ])("%s 状态有活动轮次时保留队列", async (status) => {
    api.get.mockResolvedValue(detail(1, "turn_first", status));
    const hook = renderHook(() => useTaskMessageQueue());
    await enqueue(hook);
    expect(api.send).not.toHaveBeenCalled();
    await snapshot(detail(2, null, "cancelled"));
    expect(api.send).toHaveBeenCalledTimes(1);
  });

  it("TASK_BUSY 竞争后等待较新的 idle，迟到快照不能覆盖 busy", async () => {
    api.get
      .mockResolvedValueOnce(detail(1, null))
      .mockResolvedValueOnce(detail(2));
    api.send.mockRejectedValueOnce(apiError("TASK_BUSY"));
    const hook = renderHook(() => useTaskMessageQueue());
    await enqueue(hook);
    await waitFor(() => expect(api.get).toHaveBeenCalledTimes(2));
    expect(hook.result.current.queues.task_a.items).toHaveLength(1);
    expect(hook.result.current.queues.task_a.error).toBeNull();
    await snapshot(detail(1, null));
    expect(api.send).toHaveBeenCalledTimes(1);
    await snapshot(detail(3, null));
    expect(api.send).toHaveBeenCalledTimes(2);
    expect(api.send).toHaveBeenLastCalledWith("task_a", body("next_1"));
  });

  it("派发前的 GET 迟到时，较新的 busy 事件仍阻止发送", async () => {
    const hook = renderHook(() => useTaskMessageQueue());
    await enqueue(hook);
    const read = deferred<TaskDetail>();
    api.get.mockReturnValueOnce(read.promise);
    await snapshot(detail(2, null));
    expect(api.get).toHaveBeenCalledTimes(2);
    await snapshot(detail(3, "turn_competing"));
    await act(async () => read.resolve(detail(2, null)));
    expect(api.send).not.toHaveBeenCalled();
    await snapshot(detail(4, null));
    expect(api.send).toHaveBeenCalledTimes(1);
  });

  it("GET 期间收到较新的 idle 时追加核对，不依赖另一个事件才能发送", async () => {
    const hook = renderHook(() => useTaskMessageQueue());
    await enqueue(hook);
    const read = deferred<TaskDetail>();
    api.get.mockReturnValueOnce(read.promise);
    await snapshot(detail(2, null));
    await snapshot(detail(4, null));
    await act(async () => read.resolve(detail(2, null)));
    expect(api.get).toHaveBeenCalledTimes(3);
    expect(api.send).toHaveBeenCalledTimes(1);
  });

  it("同一 idle revision 连续收到旧 GET 时只追加核对一次", async () => {
    const hook = renderHook(() => useTaskMessageQueue());
    await enqueue(hook);
    const read = deferred<TaskDetail>();
    api.get.mockReturnValueOnce(read.promise);
    await snapshot(detail(2, null));
    await snapshot(detail(4, null));
    api.get.mockResolvedValue(detail(2, null));
    await act(async () => read.resolve(detail(2, null)));
    expect(api.get).toHaveBeenCalledTimes(3);
    expect(api.send).not.toHaveBeenCalled();
    expect(hook.result.current.queues.task_a.items).toHaveLength(1);
    await snapshot(detail(4, null));
    expect(api.send).toHaveBeenCalledTimes(1);
  });

  it("TASK_BUSY 后同 revision 的 idle 不会循环重试", async () => {
    api.get.mockResolvedValue(detail(1, null));
    api.send.mockRejectedValueOnce(apiError("TASK_BUSY"));
    const hook = renderHook(() => useTaskMessageQueue());
    await enqueue(hook);
    expect(api.get).toHaveBeenCalledTimes(2);
    await snapshot(detail(1, null));
    expect(api.send).toHaveBeenCalledTimes(1);
    await snapshot(detail(2, null));
    expect(api.send).toHaveBeenCalledTimes(2);
  });

  it("未知发送结果保留队首；显式 retry 确认原请求已接收后不重复发送", async () => {
    api.get.mockResolvedValueOnce(detail(1, null));
    api.send.mockRejectedValueOnce(new Error("connection lost"));
    const hook = renderHook(() => useTaskMessageQueue());
    await enqueue(hook);
    await waitFor(() =>
      expect(hook.result.current.queues.task_a.error?.message).toBe(
        "connection lost",
      ),
    );
    await snapshot(
      detail(4, null, "completed", "task_a", "project_a", "next_1"),
    );
    expect(hook.result.current.queues.task_a.items).toHaveLength(1);
    expect(api.send).toHaveBeenCalledTimes(1);
    api.get.mockResolvedValue(
      detail(4, null, "completed", "task_a", "project_a", "next_1"),
    );
    await act(async () => hook.result.current.retry("task_a"));
    expect(api.send).toHaveBeenCalledTimes(1);
    expect(hook.result.current.queues.task_a).toBeUndefined();
    expect(
      hook.result.current.delivered.task_a.turns.some(
        (turn) => turn.request_id === "next_1",
      ),
    ).toBe(true);
  });

  it("显式 retry 在确认未接收后复用冻结的 request ID", async () => {
    api.get.mockResolvedValue(detail(1, null));
    api.send.mockRejectedValueOnce(new Error("connection lost"));
    const hook = renderHook(() => useTaskMessageQueue());
    await enqueue(hook);
    await waitFor(() =>
      expect(hook.result.current.queues.task_a.error).not.toBeNull(),
    );
    await act(async () => hook.result.current.retry("task_a"));
    expect(api.send).toHaveBeenCalledTimes(2);
    expect(api.send.mock.calls[0]).toEqual(api.send.mock.calls[1]);
  });

  it("断线后自动重建只读订阅，未知发送结果仍需要显式 retry", async () => {
    vi.useFakeTimers();
    api.get.mockResolvedValueOnce(detail(1, null));
    api.send.mockRejectedValueOnce(new Error("lost receipt"));
    api.events.mockRejectedValueOnce(new Error("disconnected"));
    const hook = renderHook(() => useTaskMessageQueue());
    await enqueue(hook);
    expect(hook.result.current.queues.task_a.error?.message).toBe(
      "lost receipt",
    );
    api.get.mockResolvedValue(
      detail(3, null, "completed", "task_a", "project_a", "next_1"),
    );
    await act(async () => vi.advanceTimersByTimeAsync(2000));
    expect(api.events).toHaveBeenCalledTimes(2);
    expect(api.send).toHaveBeenCalledTimes(1);
    expect(hook.result.current.queues.task_a.items).toHaveLength(1);
    await act(async () => hook.result.current.retry("task_a"));
    expect(hook.result.current.queues.task_a).toBeUndefined();
    expect(api.send).toHaveBeenCalledTimes(1);
  });

  it("任务和项目切换后按原任务 ID 后台发送，包括无项目任务", async () => {
    api.get.mockImplementation((taskID) =>
      Promise.resolve(
        detail(
          1,
          "turn_first",
          "running",
          taskID,
          taskID === "task_b" ? null : "project_a",
        ),
      ),
    );
    api.send.mockImplementation((taskID, message) =>
      Promise.resolve(
        detail(
          3,
          "turn_next",
          "running",
          taskID,
          taskID === "task_b" ? null : "project_a",
          message.request_id,
        ),
      ),
    );
    const hook = renderHook(() => useTaskMessageQueue());
    await enqueue(hook, "next_a");
    await enqueue(hook, "next_b", "task_b", null);
    hook.rerender();
    await snapshot(detail(2, null, "completed", "task_b", null));
    expect(api.send).toHaveBeenCalledWith("task_b", body("next_b"));
    expect(hook.result.current.queues.task_a.items[0].id).toBe("next_a");
    await snapshot(detail(2, null));
    expect(api.send).toHaveBeenLastCalledWith("task_a", body("next_a"));
  });

  it.each(["wrong_project", "archived"])("%s 快照阻止派发", async (kind) => {
    api.get.mockResolvedValue({
      ...detail(1, null),
      project_id: kind === "wrong_project" ? "project_other" : "project_a",
      archived: kind === "archived",
    });
    const hook = renderHook(() => useTaskMessageQueue());
    await enqueue(hook);
    expect(api.send).not.toHaveBeenCalled();
    expect(api.events).not.toHaveBeenCalled();
    expect(hook.result.current.queues.task_a.error).toBeInstanceOf(Error);
    expect(hook.result.current.queues.task_a.items).toHaveLength(1);
  });

  it("每个任务最多 10 条，并深拷贝选项和附件元数据", async () => {
    const attachment: TaskInputAttachment = {
      attachment_id: "attachment_a",
      name: "report.pdf",
      media_type: "application/pdf",
      byte_count: 1,
      sha256: "a".repeat(64),
      preview_url: "/api/project-tasks/attachments/attachment_a",
      created_at: "2026-10-16T00:00:00Z",
    };
    const message: TaskMessage = {
      ...body("next_0"),
      attachment_ids: [attachment.attachment_id],
      options: {
        skills: [{ package_id: "package_a", slug: "skill_a" }],
        mcp_servers: ["mcp_a"],
      },
    };
    const hook = renderHook(() => useTaskMessageQueue());
    await act(async () => {
      hook.result.current.enqueue("task_a", "project_a", message, [attachment]);
      message.attachment_ids![0] = "changed";
      message.options!.skills![0].slug = "changed";
      message.options!.mcp_servers![0] = "changed";
      attachment.name = "changed";
      for (let index = 1; index < TASK_QUEUE_MAX_ITEMS; index += 1)
        expect(
          hook.result.current.enqueue(
            "task_a",
            "project_a",
            body(`next_${index}`),
            [],
          ),
        ).toBe("ok");
      expect(
        hook.result.current.enqueue(
          "task_a",
          "project_a",
          body("overflow"),
          [],
        ),
      ).toBe("full");
      expect(
        hook.result.current.enqueue("task_b", null, body("other"), []),
      ).toBe("ok");
    });
    const first = hook.result.current.queues.task_a.items[0];
    expect(first.body.attachment_ids).toEqual(["attachment_a"]);
    expect(first.body.options?.skills?.[0].slug).toBe("skill_a");
    expect(first.body.options?.mcp_servers).toEqual(["mcp_a"]);
    expect(first.attachments[0].name).toBe("report.pdf");
    expect(hook.result.current.queues.task_a.items).toHaveLength(10);
  });

  it("移除或取回未发送消息；发送中的队首不可取回或移除", async () => {
    const sent = deferred<TaskDetail>();
    api.send.mockReturnValueOnce(sent.promise);
    const hook = renderHook(() => useTaskMessageQueue());
    await enqueue(hook, "next_1");
    await enqueue(hook, "next_2");
    await act(async () => hook.result.current.remove("task_a", "next_2"));
    expect(
      hook.result.current.queues.task_a.items.map((item) => item.id),
    ).toEqual(["next_1"]);
    await snapshot(detail(2, null));
    await act(async () => {
      expect(hook.result.current.reclaim("task_a", "next_1")).toBeNull();
      hook.result.current.remove("task_a", "next_1");
    });
    expect(hook.result.current.queues.task_a.items).toHaveLength(1);
    await act(async () => sent.reject(new Error("failed")));
    await act(async () => {
      expect(hook.result.current.reclaim("task_a", "next_1")?.body).toEqual(
        body("next_1"),
      );
    });
    expect(hook.result.current.queues.task_a).toBeUndefined();
    expect(subscriptions.get("task_a")?.signal.aborted).toBe(true);
  });

  it("clear 后屏蔽迟到的发送结果和事件，新队列不被旧结果复活", async () => {
    api.get.mockResolvedValueOnce(detail(1, null));
    const sent = deferred<TaskDetail>();
    api.send.mockReturnValueOnce(sent.promise);
    const hook = renderHook(() => useTaskMessageQueue());
    await enqueue(hook);
    const oldSubscription = subscriptions.get("task_a")!;
    await act(async () => hook.result.current.clear("task_a"));
    expect(oldSubscription.signal.aborted).toBe(true);
    await enqueue(hook, "new_message");
    await act(async () => {
      sent.resolve(
        detail(2, null, "completed", "task_a", "project_a", "next_1"),
      );
      oldSubscription.receive({ type: "snapshot", data: detail(3, null) });
    });
    expect(
      hook.result.current.queues.task_a.items.map((item) => item.id),
    ).toEqual(["new_message"]);
    expect(hook.result.current.delivered.task_a).toBeUndefined();
    expect(api.send).toHaveBeenCalledTimes(1);
  });

  it("成功发送的 receipt 保留到显式 clear，清理后不能恢复已删除任务", async () => {
    api.get.mockResolvedValue(detail(1, null));
    const hook = renderHook(() => useTaskMessageQueue());
    await enqueue(hook);
    expect(hook.result.current.queues.task_a).toBeUndefined();
    expect(hook.result.current.delivered.task_a).toBeDefined();
    await act(async () => hook.result.current.clear("task_a"));
    expect(hook.result.current.delivered.task_a).toBeUndefined();
  });

  it("unmount 清理订阅并屏蔽未完成 GET", async () => {
    const pending = deferred<TaskDetail>();
    api.get.mockReturnValueOnce(pending.promise);
    const hook = renderHook(() => useTaskMessageQueue());
    await enqueue(hook);
    hook.unmount();
    await act(async () => pending.resolve(detail(1, null)));
    expect(api.send).not.toHaveBeenCalled();
    expect(api.events).not.toHaveBeenCalled();
  });

  it("unmount 清理已有订阅并屏蔽未完成发送", async () => {
    api.get.mockResolvedValue(detail(1, null));
    const sent = deferred<TaskDetail>();
    api.send.mockReturnValueOnce(sent.promise);
    const hook = renderHook(() => useTaskMessageQueue());
    await enqueue(hook);
    const subscription = subscriptions.get("task_a")!;
    hook.unmount();
    expect(subscription.signal.aborted).toBe(true);
    await act(async () => sent.resolve(detail(2, null)));
    expect(api.send).toHaveBeenCalledTimes(1);
    expect(api.events).toHaveBeenCalledTimes(1);
  });

  it.each(["hidden", "offline"])(
    "%s 时不新派发，恢复后重新核对状态",
    async (condition) => {
      const visibility = vi.spyOn(document, "hidden", "get");
      const connectivity = vi.spyOn(navigator, "onLine", "get");
      visibility.mockReturnValue(condition === "hidden");
      connectivity.mockReturnValue(condition !== "offline");
      const hook = renderHook(() => useTaskMessageQueue());
      await enqueue(hook);
      expect(api.get).not.toHaveBeenCalled();
      expect(api.send).not.toHaveBeenCalled();
      api.get.mockResolvedValue(detail(1, null));
      visibility.mockReturnValue(false);
      connectivity.mockReturnValue(true);
      await act(async () => {
        if (condition === "hidden")
          document.dispatchEvent(new Event("visibilitychange"));
        else window.dispatchEvent(new Event("online"));
      });
      expect(api.get).toHaveBeenCalledTimes(1);
      expect(api.send).toHaveBeenCalledTimes(1);
    },
  );
});
