import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ProjectTaskSocket } from "./projectTaskSocket";

const auth = vi.hoisted(() => ({ token: "user-token" }));
vi.mock("../request", () => ({
  getAuthToken: () => auth.token,
  isSessionOnlyAuth: () => false,
  setAuthToken: (token: string) => {
    auth.token = token;
  },
}));
vi.mock("../config", () => ({
  getWsUrl: (path: string) => `ws://localhost/api${path}`,
}));
vi.mock("../../i18n", () => ({
  default: { language: "zh", t: (key: string) => key },
}));

class FakeSocket {
  static OPEN = 1;
  static instances: FakeSocket[] = [];
  readyState = 0;
  sent: Record<string, unknown>[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(readonly url: string) {
    FakeSocket.instances.push(this);
  }
  send(raw: string) {
    this.sent.push(JSON.parse(raw));
  }
  open() {
    this.readyState = 1;
    this.onopen?.();
  }
  receive(frame: unknown) {
    this.onmessage?.({ data: JSON.stringify(frame) });
  }
  close() {
    this.readyState = 3;
    this.onclose?.();
  }
  result(type: string, data: unknown = null) {
    const frame = this.sent.findLast((frame) => frame.type === type);
    expect(frame).toBeDefined();
    this.receive({ type: "result", id: frame!.id, data });
  }
}
const flush = async () => {
  for (let i = 0; i < 8; i++) await Promise.resolve();
};
const ready = async () => {
  await flush();
  const ws = FakeSocket.instances.at(-1)!;
  ws.open();
  ws.receive({ type: "ready", protocol_version: 3 });
  await flush();
  return ws;
};
beforeEach(() => {
  vi.useFakeTimers();
  auth.token = "user-token";
  FakeSocket.instances = [];
  vi.stubGlobal("WebSocket", FakeSocket);
});
afterEach(() => {
  vi.clearAllTimers();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("shared task WebSocket", () => {
  it("authenticates without a URL token and routes Chinese text to separate tasks", async () => {
    const connection = new ProjectTaskSocket();
    const a = new AbortController(),
      b = new AbortController();
    const receiveA = vi.fn(),
      receiveB = vi.fn();
    const first = connection.events("task_a", "cursor_a", receiveA, a.signal);
    const second = connection.events("task_b", "cursor_b", receiveB, b.signal);
    const ws = await ready();
    expect(FakeSocket.instances).toHaveLength(1);
    expect(ws.url).toBe("ws://localhost/api/project-tasks/ws");
    expect(ws.sent[0]).toEqual({
      type: "authenticate",
      token: "user-token",
      locale: "zh",
    });
    for (const frame of ws.sent.filter((f) => f.type === "task.subscribe"))
      ws.receive({ type: "result", id: frame.id, data: null });
    const update = {
      type: "turn",
      data: {
        task: { task_id: "task_a" },
        turn: { reply: "实时中文" },
        cursor: "next_a",
      },
    };
    ws.receive(update);
    expect(receiveA).toHaveBeenCalledExactlyOnceWith(update);
    expect(receiveB).not.toHaveBeenCalled();
    a.abort();
    await first;
    await flush();
    ws.result("task.unsubscribe");
    await vi.advanceTimersByTimeAsync(2000);
    expect(ws.readyState).toBe(1);
    b.abort();
    await second;
    await flush();
    ws.result("task.unsubscribe");
    await vi.advanceTimersByTimeAsync(1000);
    expect(ws.readyState).toBe(3);
  });

  it("matches receipts to commands and never resends mutations after disconnect", async () => {
    const connection = new ProjectTaskSocket();
    const mutation = connection.command("task.create", {
      data: { request_id: "stable-request", prompt: "Hello" },
    });
    const rejected = expect(mutation).rejects.toThrow();
    const ws = await ready();
    expect(ws.sent.filter((f) => f.type === "task.create")).toHaveLength(1);
    ws.close();
    await rejected;
    await vi.advanceTimersByTimeAsync(20000);
    expect(FakeSocket.instances).toHaveLength(1);
    const retry = connection.command("task.create", {
      data: { request_id: "stable-request", prompt: "Hello" },
    });
    const newer = await ready();
    newer.result("task.create", { task_id: "task_a" });
    expect(await retry).toEqual({ task_id: "task_a" });
    expect(newer.sent.at(-1)?.data).toEqual({
      request_id: "stable-request",
      prompt: "Hello",
    });
  });

  it("preserves cursor errors per task and keeps the other subscription", async () => {
    const connection = new ProjectTaskSocket();
    const a = new AbortController(),
      b = new AbortController();
    const first = connection.events("task_a", "old_a", vi.fn(), a.signal);
    const failed = expect(first).rejects.toThrow("PROJECT_SYNC_RESET");
    const receiveB = vi.fn();
    const second = connection.events("task_b", "old_b", receiveB, b.signal);
    const ws = await ready();
    for (const frame of ws.sent.filter((f) => f.type === "task.subscribe"))
      ws.receive({ type: "result", id: frame.id, data: null });
    ws.receive({
      type: "error",
      task_id: "task_a",
      status: 409,
      error: { code: "PROJECT_SYNC_RESET" },
    });
    await failed;
    ws.receive({ type: "snapshot", data: { task_id: "task_b" } });
    expect(receiveB).toHaveBeenCalledTimes(1);
    expect(ws.readyState).toBe(1);
    b.abort();
    await second;
  });

  it("renews tokens and drops an old account connection before delivering data", async () => {
    const connection = new ProjectTaskSocket();
    const controller = new AbortController();
    const receive = vi.fn();
    const stream = connection.events(
      "task_a",
      "cursor_a",
      receive,
      controller.signal,
    );
    const failed = expect(stream).rejects.toThrow();
    const ws = await ready();
    ws.result("task.subscribe");
    ws.receive({ type: "session", access_token: "renewed-token" });
    expect(auth.token).toBe("renewed-token");
    auth.token = "another-account-token";
    ws.receive({ type: "snapshot", data: { task_id: "task_a" } });
    await failed;
    expect(receive).not.toHaveBeenCalled();
    expect(ws.readyState).toBe(3);
    expect(FakeSocket.instances).toHaveLength(1);
  });

  it("ignores a late command error after timeout without dropping task streams", async () => {
    const connection = new ProjectTaskSocket();
    const controller = new AbortController();
    const receive = vi.fn();
    const stream = connection.events(
      "task_a",
      "cursor_a",
      receive,
      controller.signal,
    );
    const ws = await ready();
    ws.result("task.subscribe");
    const mutation = connection.command("task.message", {
      task_id: "task_a",
      data: { request_id: "stable-message", prompt: "Hello" },
    });
    const failed = expect(mutation).rejects.toThrow();
    await flush();
    const sent = ws.sent.findLast((frame) => frame.type === "task.message")!;
    await vi.advanceTimersByTimeAsync(20000);
    ws.receive({ type: "heartbeat" });
    await vi.advanceTimersByTimeAsync(10000);
    await failed;
    ws.receive({
      type: "error",
      id: sent.id,
      status: 409,
      error: { code: "TASK_BUSY" },
    });
    ws.receive({ type: "snapshot", data: { task_id: "task_a" } });
    expect(receive).toHaveBeenCalledTimes(1);
    expect(ws.readyState).toBe(1);
    expect(
      ws.sent.filter((frame) => frame.type === "task.message"),
    ).toHaveLength(1);
    controller.abort();
    await stream;
  });
});

it("rejects incompatible task protocol versions before sending commands", async () => {
  const connection = new ProjectTaskSocket();
  const pending = connection.command("task.open", { task_id: "a" });
  const rejected = expect(pending).rejects.toThrow("protocolMismatch");
  await flush();
  const ws = FakeSocket.instances.at(-1)!;
  ws.open();
  ws.receive({ type: "ready", protocol_version: 1 });
  await rejected;
  expect(ws.sent.some((frame) => frame.type === "task.open")).toBe(false);
});
