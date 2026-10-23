import i18n from "../../i18n";
import { taskSocketPath } from "../taskTransport";
import { getWsUrl } from "../config";
import { getAuthToken, isSessionOnlyAuth, setAuthToken } from "../request";
import type { TaskApprovalPolicy, TaskEvent } from "./projects";

type Pending = {
  resolve: (data: unknown) => void;
  reject: (error: Error) => void;
  timer: ReturnType<typeof setTimeout>;
};
type Listener = {
  receive: (event: TaskEvent) => void;
  reject: (error: Error) => void;
};
type ApprovalListener = {
  receive: (policy: TaskApprovalPolicy) => void;
  reject: (error: Error) => void;
};
type Frame = {
  type: string;
  protocol_version?: number;
  id?: string;
  task_id?: string;
  data?: unknown;
  access_token?: string;
  status?: number;
  error?: { code: string; message: string };
};
const disconnected = () => new Error(i18n.t("projects.tasks.reconnecting"));

/** One connection per browser tab/account, shared by task commands and readers. */
export class ProjectTaskSocket {
  private readonly hostID: string | null;
  constructor(hostID: string | null = null) {
    this.hostID = hostID;
  }
  private socket: WebSocket | null = null;
  private ready: Promise<void> | null = null;
  private token = "";
  private pending = new Map<string, Pending>();
  private listeners = new Map<string, Set<Listener>>();
  private approvalListeners = new Set<ApprovalListener>();
  private rejectReady: ((error: Error) => void) | null = null;
  private idle: ReturnType<typeof setTimeout> | undefined;
  private watchdog: ReturnType<typeof setTimeout> | undefined;

  private fail(error: Error, socket: WebSocket) {
    if (this.socket !== socket) return;
    this.socket = null;
    this.ready = null;
    clearTimeout(this.idle);
    clearTimeout(this.watchdog);
    this.rejectReady?.(error);
    this.rejectReady = null;
    for (const command of this.pending.values()) {
      clearTimeout(command.timer);
      command.reject(error);
    }
    this.pending.clear();
    const listeners = [...this.listeners.values()];
    this.listeners.clear();
    const approvalListeners = [...this.approvalListeners];
    this.approvalListeners.clear();
    for (const reader of approvalListeners) reader.reject(error);
    for (const readers of listeners)
      for (const reader of readers) reader.reject(error);
    socket.close();
  }

  private release() {
    if (this.pending.size || this.listeners.size || this.approvalListeners.size)
      return;
    clearTimeout(this.idle);
    this.idle = setTimeout(() => {
      if (
        this.socket &&
        !this.pending.size &&
        !this.listeners.size &&
        !this.approvalListeners.size
      )
        this.fail(disconnected(), this.socket);
    }, 1000);
  }

  private connect(): Promise<void> {
    clearTimeout(this.idle);
    const token = getAuthToken();
    if (this.socket && this.token !== token)
      this.fail(disconnected(), this.socket);
    if (this.ready) return this.ready;
    if (!token) return Promise.reject(new Error("Unauthorized"));
    const socket = new WebSocket(getWsUrl(taskSocketPath(this.hostID)));
    this.socket = socket;
    this.token = token;
    this.ready = new Promise<void>((resolve, reject) => {
      this.rejectReady = reject;
      this.watchdog = setTimeout(
        () => this.fail(disconnected(), socket),
        10000,
      );
      socket.onopen = () => {
        if (this.socket === socket)
          socket.send(
            JSON.stringify({
              type: "authenticate",
              token,
              locale: i18n.language?.startsWith("zh") ? "zh" : "en",
            }),
          );
      };
      socket.onmessage = (event) => {
        if (this.socket !== socket) return;
        if (getAuthToken() !== this.token) {
          this.fail(disconnected(), socket);
          return;
        }
        clearTimeout(this.watchdog);
        this.watchdog = setTimeout(
          () => this.fail(disconnected(), socket),
          30000,
        );
        let frame: Frame;
        try {
          frame = JSON.parse(String(event.data)) as Frame;
        } catch {
          this.fail(disconnected(), socket);
          return;
        }
        if (frame.type === "ready") {
          if (frame.protocol_version !== 3) {
            this.fail(
              new Error(i18n.t("projects.tasks.protocolMismatch")),
              socket,
            );
            return;
          }
          this.rejectReady = null;
          resolve();
        } else if (
          frame.type === "session" &&
          frame.access_token &&
          !this.hostID
        ) {
          setAuthToken(frame.access_token, !isSessionOnlyAuth());
          this.token = frame.access_token;
        } else if (frame.type === "result" && frame.id) {
          const command = this.pending.get(frame.id);
          if (command) {
            clearTimeout(command.timer);
            this.pending.delete(frame.id);
            command.resolve(frame.data);
            this.release();
          }
        } else if (frame.type === "error") {
          const error = new Error(
            `${frame.status ?? 500} - ${JSON.stringify({
              error: frame.error,
            })}`,
          );
          if (frame.id) {
            const command = this.pending.get(frame.id);
            if (!command) return;
            clearTimeout(command.timer);
            this.pending.delete(frame.id);
            command.reject(error);
            this.release();
          } else if (frame.task_id) {
            const readers = this.listeners.get(frame.task_id);
            this.listeners.delete(frame.task_id);
            for (const reader of readers ?? []) reader.reject(error);
            this.release();
          } else this.fail(error, socket);
        } else if (frame.type === "approval.policy") {
          const policy = frame.data as TaskApprovalPolicy;
          if (
            !policy ||
            !["ask_for_approval", "approve_for_me", "full_access"].includes(
              policy.mode,
            ) ||
            !Number.isInteger(policy.revision) ||
            policy.revision < 1 ||
            typeof policy.updated_at !== "string"
          ) {
            this.fail(disconnected(), socket);
            return;
          }
          for (const reader of this.approvalListeners) reader.receive(policy);
        } else if (frame.type === "turn" || frame.type === "snapshot") {
          const update = frame as TaskEvent;
          const taskID =
            update.type === "turn"
              ? update.data.task.task_id
              : update.data.task_id;
          for (const reader of this.listeners.get(taskID) ?? [])
            reader.receive(update);
        }
      };
      socket.onerror = () => this.fail(disconnected(), socket);
      socket.onclose = () => this.fail(disconnected(), socket);
    });
    return this.ready;
  }

  async command<T>(type: string, fields: Record<string, unknown>): Promise<T> {
    await this.connect();
    const socket = this.socket;
    if (!socket || socket.readyState !== WebSocket.OPEN) throw disconnected();
    const id = crypto.randomUUID();
    // A lost receipt is ambiguous. Never replay a mutating command automatically.
    return new Promise<T>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(disconnected());
        this.release();
      }, 30000);
      this.pending.set(id, {
        resolve: (data) => resolve(data as T),
        reject,
        timer,
      });
      try {
        socket.send(JSON.stringify({ type, id, ...fields }));
      } catch {
        this.fail(disconnected(), socket);
      }
    });
  }

  async approvalEvents(
    receive: (policy: TaskApprovalPolicy) => void,
    signal: AbortSignal,
    onOpen?: () => void,
  ): Promise<void> {
    if (signal.aborted) return;
    await this.connect();
    if (signal.aborted) {
      this.release();
      return;
    }
    return new Promise<void>((resolve, reject) => {
      let finished = false;
      const finish = (error?: Error) => {
        if (finished) return;
        finished = true;
        signal.removeEventListener("abort", abort);
        this.approvalListeners.delete(listener);
        this.release();
        if (error) reject(error);
        else resolve();
      };
      const abort = () => finish();
      const listener: ApprovalListener = {
        receive,
        reject: (error) => finish(error),
      };
      this.approvalListeners.add(listener);
      signal.addEventListener("abort", abort, { once: true });
      onOpen?.();
    });
  }

  async events(
    taskID: string,
    cursor: string,
    receive: (event: TaskEvent) => void,
    signal: AbortSignal,
    onOpen?: () => void,
  ): Promise<void> {
    if (signal.aborted) return;
    await this.connect();
    if (signal.aborted) {
      this.release();
      return;
    }
    return new Promise<void>((resolve, reject) => {
      let finished = false;
      const cleanup = () => {
        signal.removeEventListener("abort", abort);
        const readers = this.listeners.get(taskID);
        readers?.delete(listener);
        if (readers && !readers.size) {
          this.listeners.delete(taskID);
          void this.command("task.unsubscribe", { task_id: taskID }).catch(
            () => undefined,
          );
        }
        this.release();
      };
      const finish = (error?: Error) => {
        if (finished) return;
        finished = true;
        cleanup();
        if (error) reject(error);
        else resolve();
      };
      const abort = () => finish();
      const listener: Listener = { receive, reject: (error) => finish(error) };
      const readers = this.listeners.get(taskID) ?? new Set<Listener>();
      readers.add(listener);
      this.listeners.set(taskID, readers);
      signal.addEventListener("abort", abort, { once: true });
      void this.command("task.subscribe", { task_id: taskID, cursor }).then(
        () => {
          if (!finished) onOpen?.();
        },
        (error: Error) => finish(error),
      );
    });
  }
}

export const projectTaskSocket = new ProjectTaskSocket();
const remoteSockets = new Map<string, ProjectTaskSocket>();
export function getProjectTaskSocket(hostID: string | null = null) {
  if (!hostID) return projectTaskSocket;
  let socket = remoteSockets.get(hostID);
  if (!socket) {
    socket = new ProjectTaskSocket(hostID);
    remoteSockets.set(hostID, socket);
  }
  return socket;
}
