import { taskTransport, taskApiPath } from "../taskTransport";
import { getAuthToken } from "../request";
import type { FileTreeNode } from "../types";
import type {
  BrowserSessionsResponse,
  BrowserSession,
  BrowserRecordReplayStatus,
  BrowserRecordStartResponse,
  BrowserRecordStopAndGenerateSkillResponse,
  BrowserSkillContentResponse,
} from "../types/browser";

export interface TaskWorkspaceFile {
  path: string;
  content: string;
  size: number;
  revision: string;
}

export interface TaskWorkbenchScope {
  task_id: string;
  scope_revision: string;
  server_directory: string | null;
  client_directory: string | null;
  capabilities: {
    files: boolean;
    browser: boolean;
    terminal: boolean;
    recording: boolean;
    tool_ui: boolean;
  };
  read_only: boolean;
}
const path = (id: string) => `/project-tasks/${encodeURIComponent(id)}`;
const query = (values: Record<string, string | undefined>) =>
  new URLSearchParams(
    Object.entries(values).filter(
      (row): row is [string, string] => row[1] !== undefined,
    ),
  ).toString();
const mediaPath = (id: string, file: string, revision: string) =>
  `${path(id)}/workspace/media?${query({
    path: file,
    expected_scope_revision: revision,
  })}`;
export function createTaskWorkbenchApi(hostID: string | null = null) {
  const {
    request,
    requestBlob,
    requestUpload,
    apiUrl: getApiUrl,
    wsUrl: getWsUrl,
  } = taskTransport(hostID);
  return {
    mediaPath: (id: string, file: string, revision: string) =>
      taskApiPath(hostID, mediaPath(id, file, revision)),
    scope: (id: string, signal?: AbortSignal) =>
      request<TaskWorkbenchScope>(`${path(id)}/workbench`, {
        cache: "no-store",
        signal,
      }),
    tree: (
      id: string,
      directory: string,
      revision: string,
      signal?: AbortSignal,
    ) =>
      request<FileTreeNode[]>(
        `${path(id)}/workspace/tree-children?${query({
          path: directory,
          expected_scope_revision: revision,
        })}`,
        { cache: "no-store", signal },
      ),
    file: (id: string, file: string, revision: string, signal?: AbortSignal) =>
      request<TaskWorkspaceFile>(
        `${path(id)}/workspace/file?${query({
          path: file,
          expected_scope_revision: revision,
        })}`,
        { cache: "no-store", signal },
      ),
    save: (
      id: string,
      file: string,
      content: string,
      revision: string,
      expectedFileRevision?: string,
    ) =>
      request<TaskWorkspaceFile>(`${path(id)}/workspace/file`, {
        method: "PUT",
        body: JSON.stringify({
          path: file,
          content,
          expected_scope_revision: revision,
          ...(expectedFileRevision
            ? { expected_revision: expectedFileRevision }
            : {}),
        }),
      }),
    mkdir: (id: string, directory: string, revision: string) =>
      request(`${path(id)}/workspace/mkdir`, {
        method: "POST",
        body: JSON.stringify({
          path: directory,
          expected_scope_revision: revision,
        }),
      }),
    move: (id: string, file: string, destination: string, revision: string) =>
      request(`${path(id)}/workspace/move`, {
        method: "POST",
        body: JSON.stringify({
          path: file,
          destination,
          expected_scope_revision: revision,
        }),
      }),
    delete: (id: string, file: string, revision: string) =>
      request(
        `${path(id)}/workspace/file?${query({
          path: file,
          expected_scope_revision: revision,
        })}`,
        { method: "DELETE" },
      ),
    upload: (id: string, directory: string, file: File, revision: string) => {
      const body = new FormData();
      body.append("file", file);
      body.append("expected_scope_revision", revision);
      return requestUpload(
        `${path(id)}/workspace/upload?${query({
          path: directory === "." ? file.name : `${directory}/${file.name}`,
        })}`,
        body,
      );
    },
    download: (id: string, file: string, revision: string) =>
      requestBlob(
        `${path(id)}/workspace/download-file?${query({
          path: file,
          expected_scope_revision: revision,
        })}`,
      ),
    media: (id: string, file: string, revision: string) =>
      requestBlob(
        `${path(id)}/workspace/media?${query({
          path: file,
          expected_scope_revision: revision,
        })}`,
      ),
    mediaUrl: (id: string, file: string, revision: string) =>
      getApiUrl(
        `${path(id)}/workspace/media?${query({
          path: file,
          expected_scope_revision: revision,
          access_token: getAuthToken() ?? undefined,
        })}`,
      ),
    browserSessions: (id: string, revision: string) =>
      request<BrowserSessionsResponse>(
        `${path(id)}/browser/sessions?${query({
          expected_scope_revision: revision,
        })}`,
        { cache: "no-store" },
      ),
    browserHandoff: (
      id: string,
      target: "agent" | "user",
      reason: string,
      revision: string,
    ) =>
      request<{ ok: boolean; session: BrowserSession }>(
        `${path(id)}/browser/handoff`,
        {
          method: "POST",
          body: JSON.stringify({
            target,
            reason,
            expected_scope_revision: revision,
          }),
        },
      ),
    browserStreamUrl: (
      id: string,
      width: number,
      height: number,
      revision: string,
    ) =>
      `${getWsUrl(`${path(id)}/browser-stream/ws`)}?${query({
        width: String(width),
        height: String(height),
        expected_scope_revision: revision,
        token: getAuthToken() ?? undefined,
      })}`,
    terminalUrl: (id: string, sessionID: string, revision: string) =>
      `${getWsUrl(`${path(id)}/terminal/ws`)}?${query({
        session_id: sessionID,
        expected_scope_revision: revision,
        token: getAuthToken() ?? undefined,
      })}`,
    closeTerminal: (id: string, sessionID: string, revision: string) =>
      request(
        `${path(id)}/terminal/sessions/${encodeURIComponent(sessionID)}?${query(
          {
            expected_scope_revision: revision,
          },
        )}`,
        { method: "DELETE" },
      ),
    recordingStatus: (id: string, revision: string) =>
      request<BrowserRecordReplayStatus>(
        `${path(id)}/browser/record-replay/status?${query({
          expected_scope_revision: revision,
        })}`,
        { cache: "no-store" },
      ),
    recordingSkill: (id: string, recordingID: string, revision: string) =>
      request<BrowserSkillContentResponse>(
        `${path(id)}/browser/record-replay/skill-content?${query({
          recordingId: recordingID,
          expected_scope_revision: revision,
        })}`,
        { cache: "no-store" },
      ),
    startRecording: (id: string, name: string, revision: string) =>
      request<BrowserRecordStartResponse>(
        `${path(id)}/browser/record-replay/start`,
        {
          method: "POST",
          body: JSON.stringify({
            name,
            objective: name,
            expected_scope_revision: revision,
          }),
        },
      ),
    stopRecording: (
      id: string,
      recordingID: string,
      name: string,
      revision: string,
    ) =>
      request<BrowserRecordStopAndGenerateSkillResponse>(
        `${path(id)}/browser/record-replay/stop-and-generate-skill`,
        {
          method: "POST",
          body: JSON.stringify({
            recordingId: recordingID,
            name,
            generateSteps: true,
            expected_scope_revision: revision,
          }),
        },
      ),
    applyRecording: (
      id: string,
      recordingID: string,
      body: {
        request_id: string;
        name: string;
        content: string;
        expected_scope_revision: string;
      },
    ) =>
      request(
        `${path(id)}/recordings/${encodeURIComponent(recordingID)}/apply`,
        {
          method: "POST",
          body: JSON.stringify(body),
        },
      ),
  };
}
export const taskWorkbenchApi = createTaskWorkbenchApi();

export interface TaskToolRequestContext {
  taskID: string;
  turnID: string;
  callID: string;
  scopeRevision: string;
  hostID?: string | null;
  readOnly: boolean;
}
export function taskToolRequest<T>(
  context: TaskToolRequestContext,
  requestedPath: string,
  init: RequestInit = {},
): Promise<T> {
  const taskPrefix = path(context.taskID);
  const scopedPath = requestedPath.startsWith("/workspace/")
    ? `${taskPrefix}${requestedPath}`
    : requestedPath;
  if (
    !context.turnID ||
    !context.callID ||
    !scopedPath.startsWith(`${taskPrefix}/`) ||
    scopedPath.includes("\\")
  )
    return Promise.reject(new Error("TASK_TOOL_REQUEST_UNSUPPORTED"));
  const url = new URL(scopedPath, "https://task.invalid");
  const relative = url.pathname.slice(taskPrefix.length);
  const method = (init.method ?? "GET").toUpperCase();
  const allowed =
    (method === "GET" &&
      ["/workspace/tree-children", "/workspace/file"].includes(relative)) ||
    (method === "PUT" && relative === "/workspace/file");
  if (
    !url.pathname.startsWith(`${taskPrefix}/`) ||
    !allowed ||
    (context.readOnly && method !== "GET")
  )
    return Promise.reject(new Error("TASK_TOOL_REQUEST_UNSUPPORTED"));
  let body: unknown;
  try {
    body = typeof init.body === "string" ? JSON.parse(init.body) : {};
  } catch {
    return Promise.reject(new Error("TASK_TOOL_REQUEST_UNSUPPORTED"));
  }
  if (!body || typeof body !== "object" || Array.isArray(body))
    return Promise.reject(new Error("TASK_TOOL_REQUEST_UNSUPPORTED"));
  url.searchParams.delete("expected_scope_revision");
  return taskTransport(context.hostID ?? null).request<T>(
    `${taskPrefix}/tool-ui/${encodeURIComponent(
      context.turnID,
    )}/${encodeURIComponent(context.callID)}/request`,
    {
      method: "POST",
      signal: init.signal,
      body: JSON.stringify({
        path: `${relative}${url.search}`,
        method,
        body,
        expected_scope_revision: context.scopeRevision,
      }),
    },
  );
}
