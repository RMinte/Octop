import { getApiUrl, getWsUrl } from "./config";
import { request, requestBlob, requestUpload } from "./request";

export interface TaskRef {
  host_id: string | null;
  task_id: string;
}
export const taskRefKey = ({ host_id, task_id }: TaskRef) =>
  host_id
    ? `remote:${encodeURIComponent(host_id)}:${encodeURIComponent(task_id)}`
    : task_id;
export function parseTaskRefKey(key: string): TaskRef {
  if (!key.startsWith("remote:")) return { host_id: null, task_id: key };
  const split = key.indexOf(":", 7);
  if (split < 0) throw new Error("Invalid Task reference");
  return {
    host_id: decodeURIComponent(key.slice(7, split)),
    task_id: decodeURIComponent(key.slice(split + 1)),
  };
}
export function taskApiPath(hostID: string | null, path: string): string {
  if (!hostID) return path;
  if (/^[a-z]+:/i.test(path)) throw new Error("Task API path must be relative");
  const peer = path.replace(/^\/api(?=\/)/, "").replace(/^\/+/, "");
  return `/bridge/connections/${encodeURIComponent(hostID)}/task-api/${peer}`;
}
export const taskSocketPath = (
  hostID: string | null,
  path = "/project-tasks/ws",
) =>
  hostID ? `/bridge/connections/${encodeURIComponent(hostID)}${path}` : path;
export function taskTransport(hostID: string | null = null) {
  return {
    request: (<T>(path: string, init?: RequestInit) =>
      request<T>(taskApiPath(hostID, path), init)) as typeof request,
    requestBlob: ((
      path: string,
      init?: RequestInit,
      progress?: Parameters<typeof requestBlob>[2],
    ) =>
      requestBlob(
        taskApiPath(hostID, path),
        init,
        progress,
      )) as typeof requestBlob,
    requestUpload: ((
      path: string,
      body: FormData,
      init?: RequestInit,
      progress?: Parameters<typeof requestUpload>[3],
    ) =>
      requestUpload(
        taskApiPath(hostID, path),
        body,
        init,
        progress,
      )) as typeof requestUpload,
    apiUrl: (path: string) => getApiUrl(taskApiPath(hostID, path)),
    wsUrl: (path: string) => getWsUrl(taskSocketPath(hostID, path)),
  };
}
export const conversationUrl = (
  ref: TaskRef | null,
  hostID: string | null = null,
) => {
  const host = ref?.host_id ?? hostID;
  return `${
    ref ? `/conversations/${encodeURIComponent(ref.task_id)}` : "/conversations"
  }${host ? `?host_id=${encodeURIComponent(host)}` : ""}`;
};
