import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, afterEach, expect, it, vi } from "vitest";
import {
  residentApi,
  type ResidentBinding,
  type ResidentHistoryPage,
  type ResidentSnapshot,
  type ResidentTurn,
} from "../../api/modules/resident";
import {
  residentStorageKey,
  useResidentConversation,
} from "./useResidentConversation";

vi.mock("../../i18n", () => ({ default: { language: "zh" } }));
vi.mock("../../api/config", () => ({
  getWsUrl: (path: string) => `ws://octop.test/api${path}`,
}));
vi.mock("../../api/request", () => ({
  getAuthToken: () => "owner-token",
  isSessionOnlyAuth: () => false,
  setAuthToken: vi.fn(),
}));
vi.mock("../../api/modules/resident", () => ({
  residentApi: Object.fromEntries(
    [
      "binding",
      "capabilities",
      "repair",
      "snapshot",
      "history",
      "inbox",
      "request",
      "send",
      "cancel",
      "searchHistory",
      "historyDays",
      "locateHistory",
      "markRead",
    ].map((key) => [key, vi.fn()]),
  ),
}));
const scope = { agent_id: "private-a", thread_id: "private-t" };
const binding: ResidentBinding = {
  status: "ready",
  ...scope,
  name: "Steward",
  timezone: "Asia/Shanghai",
  error_code: null,
};
const turn = (values: Partial<ResidentTurn> = {}): ResidentTurn => ({
  turn_id: "turn-1",
  revision: 1,
  request_id: "send-1",
  user_message_id: "user-1",
  assistant_message_id: "assistant-1",
  prompt: "hello",
  reply: "",
  status: "running",
  created_at: "2026-10-24T00:00:00Z",
  updated_at: "2026-10-24T00:00:00Z",
  error_code: null,
  error_message: null,
  authorization: { read_tools: false },
  ...values,
});
const snapshot = (
  values: Partial<ResidentSnapshot> = {},
): ResidentSnapshot => ({
  ...scope,
  revision: 1,
  active_turn_id: null,
  has_earlier: false,
  turns: [],
  ...values,
});
const history: ResidentHistoryPage = {
  ...scope,
  history_revision: 1,
  items: [],
  next_cursor: null,
};
const notFound = () => new Error("Request failed: 404 Not Found");
class Socket {
  static instances: Socket[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  sent: Record<string, unknown>[] = [];
  closed = false;
  constructor(readonly url: string) {
    Socket.instances.push(this);
  }
  send(value: string) {
    this.sent.push(JSON.parse(value));
  }
  close() {
    this.closed = true;
  }
  open() {
    this.onopen?.();
  }
  message(value: unknown) {
    this.onmessage?.({ data: JSON.stringify(value) });
  }
}
class MemoryStorage implements Storage {
  private values = new Map<string, string>();
  get length() {
    return this.values.size;
  }
  clear() {
    this.values.clear();
  }
  getItem(key: string) {
    return this.values.get(key) ?? null;
  }
  setItem(key: string, value: string) {
    this.values.set(key, value);
  }
  removeItem(key: string) {
    this.values.delete(key);
  }
  key(index: number) {
    return [...this.values.keys()][index] ?? null;
  }
}
const api = vi.mocked(residentApi);
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}
async function ready() {
  const hook = renderHook(() => useResidentConversation(1));
  await waitFor(() => expect(hook.result.current.snapshot).not.toBeNull());
  return hook;
}
beforeEach(() => {
  vi.clearAllMocks();
  Object.values(api).forEach((method) => method.mockReset());
  vi.stubGlobal("localStorage", new MemoryStorage());
  vi.stubGlobal("sessionStorage", new MemoryStorage());
  localStorage.clear();
  sessionStorage.clear();
  Socket.instances = [];
  vi.stubGlobal("WebSocket", Socket);
  api.binding.mockResolvedValue(binding);
  api.capabilities.mockResolvedValue({
    api_version: 1,
    conversation_stream: {
      transport: "websocket",
      path: "/api/project-tasks/ws",
      protocol_version: 3,
    },
  } as Awaited<ReturnType<typeof residentApi.capabilities>>);
  api.snapshot.mockResolvedValue(snapshot());
  api.history.mockResolvedValue(history);
  api.inbox.mockResolvedValue({
    ...scope,
    revision: 1,
    unread_count: 0,
    last_message_id: null,
    last_read_message_id: null,
  });
  api.request.mockRejectedValue(notFound());
  api.send.mockImplementation(async (_scope, body) =>
    turn({
      request_id: body.request_id,
      prompt: body.prompt,
      authorization: { read_tools: body.read_tools },
    }),
  );
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

it("loads only the server-managed identity and subscribes through real resident frames", async () => {
  const hook = await ready();
  const ws = Socket.instances[0];
  expect(api.repair).not.toHaveBeenCalled();
  expect(api.markRead).not.toHaveBeenCalled();
  expect(ws.url).toBe("ws://octop.test/api/project-tasks/ws");
  act(() => {
    ws.open();
    ws.message({ type: "ready", protocol_version: 3 });
  });
  expect(ws.sent[0]).toEqual({
    type: "authenticate",
    token: "owner-token",
    locale: "zh",
  });
  expect(ws.sent[1]).toMatchObject({ type: "resident.subscribe", ...scope });
  await act(async () =>
    ws.message({
      type: "resident.snapshot",
      data: snapshot({ revision: 2, turns: [turn({ reply: "partial" })] }),
    }),
  );
  expect(hook.result.current.connectionState).toBe("connected");
  expect(hook.result.current.turns[0].reply).toBe("partial");
});
it("rejects late snapshot and per-turn revisions instead of appending cumulative text", async () => {
  const hook = await ready();
  const ws = Socket.instances[0];
  await act(async () =>
    ws.message({
      type: "resident.snapshot",
      data: snapshot({
        revision: 5,
        turns: [turn({ revision: 4, reply: "complete", status: "completed" })],
      }),
    }),
  );
  await act(async () =>
    ws.message({
      type: "resident.snapshot",
      data: snapshot({
        revision: 4,
        turns: [turn({ revision: 3, reply: "late" })],
      }),
    }),
  );
  await act(async () =>
    ws.message({
      type: "resident.snapshot",
      data: snapshot({
        revision: 6,
        turns: [turn({ revision: 2, reply: "older turn" })],
      }),
    }),
  );
  expect(hook.result.current.turns).toHaveLength(1);
  expect(hook.result.current.turns[0]).toMatchObject({
    reply: "complete",
    status: "completed",
    revision: 4,
  });
});
it("persists a send before POST and recovers a lost receipt without a second acceptance", async () => {
  const hook = await ready();
  let savedAtPost: unknown;
  api.send.mockImplementation(async (_scope, body) => {
    savedAtPost = JSON.parse(
      localStorage.getItem(residentStorageKey(1, scope))!,
    ).pending;
    api.request.mockResolvedValue(
      turn({ request_id: body.request_id, prompt: body.prompt }),
    );
    throw new Error("lost response");
  });
  act(() => {
    hook.result.current.setDraft(" original text ");
    hook.result.current.setReadTools(true);
  });
  await act(() => hook.result.current.send());
  expect(savedAtPost).toEqual(api.send.mock.calls[0][1]);
  expect(api.send).toHaveBeenCalledTimes(1);
  expect(hook.result.current.pendingIntent).toBeNull();
  expect(hook.result.current.draft).toBe("");
  expect(hook.result.current.turns[0].prompt).toBe(" original text ");
});
it("keeps the original ID, prompt and authorization after uncertain delivery and draft edits", async () => {
  const hook = await ready();
  api.send.mockRejectedValue(new Error("network unavailable"));
  act(() => {
    hook.result.current.setDraft("original");
    hook.result.current.setReadTools(true);
  });
  await act(() => hook.result.current.send());
  const original = api.send.mock.calls[0][1];
  act(() => {
    hook.result.current.setDraft("new unsent draft");
    hook.result.current.setReadTools(false);
  });
  api.send.mockImplementation(async (_scope, body) =>
    turn({ request_id: body.request_id, prompt: body.prompt }),
  );
  await act(() => hook.result.current.retryPending());
  expect(api.send.mock.calls[1][1]).toEqual(original);
  expect(original).toMatchObject({ prompt: "original", read_tools: true });
  expect(hook.result.current.draft).toBe("new unsent draft");
});
it("reconciles a persisted pending intent by request lookup on remount", async () => {
  const pending = {
    request_id: "persisted",
    prompt: "unchanged",
    read_tools: false,
  };
  localStorage.setItem(
    residentStorageKey(1, scope),
    JSON.stringify({
      draft: "unchanged",
      readTools: true,
      pending,
      cancellations: {},
    }),
  );
  api.request.mockResolvedValue(
    turn({ request_id: "persisted", prompt: "unchanged", status: "completed" }),
  );
  const hook = await ready();
  await waitFor(() => expect(hook.result.current.pendingIntent).toBeNull());
  expect(api.request).toHaveBeenCalledWith(scope, "persisted");
  expect(api.send).not.toHaveBeenCalled();
});
it("uses the same persisted intent after explicit 404 and never upgrades read access", async () => {
  const pending = {
    request_id: "persisted",
    prompt: "unchanged",
    read_tools: false,
    knowledge_base_ids: [],
    task_context: null,
  };
  localStorage.setItem(
    residentStorageKey(1, scope),
    JSON.stringify({
      draft: "unchanged",
      readTools: true,
      pending,
      cancellations: {},
    }),
  );
  await ready();
  await waitFor(() => expect(api.send).toHaveBeenCalled());
  expect(api.send).toHaveBeenCalledWith(scope, pending);
});
it("does not replay after a forbidden or unavailable original receipt lookup", async () => {
  const pending = {
    request_id: "persisted",
    prompt: "unchanged",
    read_tools: false,
  };
  localStorage.setItem(
    residentStorageKey(1, scope),
    JSON.stringify({
      draft: "unchanged",
      readTools: false,
      pending,
      cancellations: {},
    }),
  );
  api.request.mockRejectedValue(new Error("Request failed: 403 Forbidden"));
  const hook = await ready();
  await waitFor(() => expect(hook.result.current.error).not.toBeNull());
  expect(api.send).not.toHaveBeenCalled();
  expect(hook.result.current.pendingIntent).toEqual(pending);
});
it("isolates account, agent and thread state and ignores old in-flight reads", async () => {
  const late = deferred<ResidentSnapshot>();
  api.snapshot.mockReturnValueOnce(late.promise);
  const hook = renderHook(({ id }) => useResidentConversation(id), {
    initialProps: { id: 1 },
  });
  await waitFor(() => expect(api.snapshot).toHaveBeenCalled());
  act(() => hook.result.current.setDraft("private account one"));
  const scope2 = { agent_id: "private-b", thread_id: "private-u" };
  api.binding.mockResolvedValue({ ...binding, ...scope2 });
  api.snapshot.mockResolvedValue(snapshot({ ...scope2, revision: 10 }));
  api.history.mockResolvedValue({ ...history, ...scope2 });
  hook.rerender({ id: 2 });
  await waitFor(() =>
    expect(hook.result.current.snapshot?.agent_id).toBe("private-b"),
  );
  expect(hook.result.current.draft).toBe("");
  expect(hook.result.current.pendingIntent).toBeNull();
  await act(async () =>
    late.resolve(snapshot({ turns: [turn({ reply: "private old reply" })] })),
  );
  expect(hook.result.current.turns).toEqual([]);
  expect(residentStorageKey(1, scope)).not.toBe(residentStorageKey(2, scope));
  expect(residentStorageKey(1, scope)).not.toBe(residentStorageKey(1, scope2));
  expect(Socket.instances[0].closed).toBe(true);
});
it("cancels only the chosen active turn and reuses its cancellation intent after a lost response", async () => {
  api.snapshot.mockResolvedValue(
    snapshot({ active_turn_id: "turn-1", turns: [turn()] }),
  );
  const hook = await ready();
  api.cancel.mockRejectedValueOnce(new Error("lost cancel receipt"));
  await act(() => hook.result.current.cancel(hook.result.current.turns[0]));
  api.cancel.mockResolvedValue(
    turn({ revision: 2, status: "cancelled", reply: "partial" }),
  );
  await act(() => hook.result.current.cancel(hook.result.current.turns[0]));
  expect(api.cancel.mock.calls[0][1]).toEqual(api.cancel.mock.calls[1][1]);
  expect(api.cancel.mock.calls[0][1]).toMatchObject({ turn_id: "turn-1" });
  expect(api.cancel.mock.calls[0][1].request_id).not.toBe("send-1");
  expect(hook.result.current.turns[0].status).toBe("cancelled");
});
it("keeps search pages distinct from live turns and rejects out-of-order search responses", async () => {
  const hook = await ready();
  const older = deferred<ResidentHistoryPage>();
  api.searchHistory.mockReturnValueOnce(older.promise).mockResolvedValueOnce({
    ...history,
    history_revision: 3,
    next_cursor: "new-query",
  });
  let pending!: Promise<ResidentHistoryPage | null>;
  act(() => {
    pending = hook.result.current.searchHistory("old");
  });
  await act(() => hook.result.current.searchHistory("new"));
  await act(async () => {
    older.resolve({ ...history, next_cursor: "old-query" });
    await pending;
  });
  expect(hook.result.current.history?.next_cursor).toBe("new-query");
  expect(hook.result.current.timeline?.next_cursor).toBeNull();
  expect(hook.result.current.turns).toEqual([]);
});
it("leaves failed or missing bindings visible and repairs only on explicit request", async () => {
  api.binding.mockResolvedValue({
    ...binding,
    status: "failed",
    error_code: "INTERNAL_ERROR",
  });
  const hook = renderHook(() => useResidentConversation(1));
  await waitFor(() =>
    expect(hook.result.current.binding?.status).toBe("failed"),
  );
  expect(api.repair).not.toHaveBeenCalled();
  expect(api.snapshot).not.toHaveBeenCalled();
  expect(Socket.instances).toHaveLength(0);
  api.repair.mockResolvedValue(binding);
  await act(() => hook.result.current.repair());
  await waitFor(() => expect(hook.result.current.snapshot).not.toBeNull());
  expect(api.repair).toHaveBeenCalledTimes(1);
});
it("does not mark unseen messages read and sends only the visible message anchor", async () => {
  const hook = await ready();
  expect(api.markRead).not.toHaveBeenCalled();
  api.markRead.mockResolvedValue({
    ...scope,
    revision: 2,
    unread_count: 0,
    last_message_id: "assistant-1",
    last_read_message_id: "assistant-1",
  });
  await act(() => hook.result.current.markRead("assistant-1"));
  expect(api.markRead).toHaveBeenCalledWith(scope, "assistant-1");
});
it("prevents new sends when the original intent cannot be durably stored", async () => {
  const hook = await ready();
  act(() => hook.result.current.setDraft("hello"));
  vi.spyOn(localStorage, "setItem").mockImplementation(() => {
    throw new Error("Storage full");
  });
  await act(() => hook.result.current.send());
  expect(api.send).not.toHaveBeenCalled();
  expect(hook.result.current.error).toBeInstanceOf(Error);
});
it("falls back to scoped REST observation when WebSocket is unavailable", async () => {
  vi.stubGlobal("WebSocket", undefined);
  const hook = await ready();
  expect(hook.result.current.connectionState).toBe("polling");
  expect(api.snapshot).toHaveBeenCalledWith(scope);
  expect(api.history).toHaveBeenCalledWith(scope, { limit: 50 });
});
it("does not guess a WebSocket protocol without a recognized capability declaration", async () => {
  api.capabilities.mockResolvedValue({ api_version: 1 } as Awaited<
    ReturnType<typeof residentApi.capabilities>
  >);
  const hook = await ready();
  expect(hook.result.current.connectionState).toBe("polling");
  expect(Socket.instances).toHaveLength(0);
});
it("releases a definitively rejected intent only after its receipt is confirmed absent", async () => {
  const hook = await ready();
  api.send.mockRejectedValue(
    new Error("Request failed: 422 Unprocessable Entity"),
  );
  act(() => hook.result.current.setDraft("invalid original"));
  await act(() => hook.result.current.send());
  expect(hook.result.current.pendingIntent).toBeNull();
  expect(hook.result.current.draft).toBe("invalid original");
  const id = api.send.mock.calls[0][1].request_id;
  api.send.mockImplementation(async (_scope, body) =>
    turn({ request_id: body.request_id, prompt: body.prompt }),
  );
  act(() => hook.result.current.setDraft("corrected"));
  await act(() => hook.result.current.send());
  expect(api.send.mock.calls[1][1].request_id).not.toBe(id);
});
it("looks up the original receipt when a WebSocket reconnects and never submits a new ID", async () => {
  const hook = await ready();
  api.send.mockRejectedValue(new Error("connection lost"));
  act(() => hook.result.current.setDraft("recover me"));
  await act(() => hook.result.current.send());
  const original = api.send.mock.calls[0][1];
  vi.useFakeTimers();
  act(() => Socket.instances[0].onclose?.());
  await act(() => vi.advanceTimersByTimeAsync(3000));
  const ws = Socket.instances.at(-1)!;
  api.request.mockResolvedValue(
    turn({
      request_id: original.request_id,
      prompt: original.prompt,
      status: "completed",
    }),
  );
  await act(async () => {
    ws.open();
    ws.message({ type: "ready", protocol_version: 3 });
  });
  expect(api.request).toHaveBeenLastCalledWith(scope, original.request_id);
  expect(api.send).toHaveBeenCalledTimes(1);
  expect(hook.result.current.pendingIntent).toBeNull();
});
it("clears an observation error after a successful scoped REST recovery", async () => {
  vi.useFakeTimers();
  const hook = renderHook(() => useResidentConversation(1));
  await act(async () => {});
  expect(hook.result.current.snapshot).not.toBeNull();
  await act(async () =>
    Socket.instances[0].message({
      type: "error",
      status: 503,
      error: { code: "INTERNAL_ERROR", message: "temporarily unavailable" },
    }),
  );
  expect(hook.result.current.error).not.toBeNull();
  await act(() => vi.advanceTimersByTimeAsync(5000));
  expect(hook.result.current.error).toBeNull();
  expect(hook.result.current.connectionState).toBe("polling");
});
it("continues with REST when the optional capability probe is unavailable", async () => {
  api.capabilities.mockRejectedValue(
    new Error("Request failed: 404 Not Found"),
  );
  const hook = await ready();
  expect(hook.result.current.binding?.status).toBe("ready");
  expect(hook.result.current.connectionState).toBe("polling");
  expect(Socket.instances).toHaveLength(0);
});

it.each(["image-original", "file-original"])(
  "freezes %s before delivery and replays it after draft changes",
  async (attachmentID) => {
    const hook = await ready();
    api.send.mockRejectedValue(new Error("network unavailable"));
    const selected = [attachmentID];
    await act(() => hook.result.current.send(selected));
    const original = api.send.mock.calls[0][1];
    expect(original).toMatchObject({
      prompt: "",
      attachment_ids: [attachmentID],
    });
    selected[0] = "changed-outside";
    act(() => hook.result.current.setDraft("next draft"));
    expect(hook.result.current.pendingIntent?.attachment_ids).toEqual([
      attachmentID,
    ]);
    expect(
      JSON.parse(localStorage.getItem(residentStorageKey(1, scope))!).pending,
    ).toEqual(original);
    hook.unmount();
    api.send.mockImplementation(async (_scope, body) =>
      turn({ request_id: body.request_id, prompt: body.prompt }),
    );
    const recovered = await ready();
    await waitFor(() => expect(api.send).toHaveBeenCalledTimes(2));
    expect(api.send.mock.calls[1][1]).toEqual(original);
    expect(recovered.result.current.draft).toBe("next draft");
    expect(recovered.result.current.acceptedIntent).toEqual(original);
  },
);
