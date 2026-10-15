import { request, requestStream } from "../request";

export interface PackageProjectSkill {
  package_id: string;
  slug: string;
}
export interface SkillHubProjectSkill {
  source: "skillhub";
  slug: string;
  name: string;
  description: string;
}
export type ProjectSkill = PackageProjectSkill | SkillHubProjectSkill;
export interface ProjectFields {
  name: string;
  description: string;
  directory: string | null;
  client_directory: string | null;
  instructions: string;
  skills: ProjectSkill[];
  mcp_servers: string[];
}
export interface Project extends ProjectFields {
  project_id: string;
  revision: number;
  created_at: string;
  updated_at: string;
}
export interface ProjectResources {
  skills: (PackageProjectSkill & { name: string; description: string })[];
  mcp_servers: {
    server_name: string;
    name: string;
    state: "configured" | "disabled" | "authorization_required";
  }[];
  directory_roots: string[];
}
export interface ProjectSyncPage {
  mode: "snapshot" | "delta";
  base_sync_cursor: string | null;
  next_sync_cursor: string | null;
  next_page_cursor: string | null;
  changes: { operation: "upsert"; project_id: string; data: Project }[];
}
const query = (values: Record<string, string | null | undefined>) => {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) {
    if (value != null) params.set(key, value);
  }
  const text = params.toString();
  return text ? `?${text}` : "";
};
export const projectsApi = {
  sync: (syncCursor?: string | null, pageCursor?: string | null) =>
    request<ProjectSyncPage>(
      `/projects/sync${query({
        sync_cursor: syncCursor,
        page_cursor: pageCursor,
      })}`,
      { cache: "no-store" },
    ),
  list: () => request<Project[]>("/projects", { cache: "no-store" }),
  get: (id: string) =>
    request<Project>(`/projects/${encodeURIComponent(id)}`, {
      cache: "no-store",
    }),
  resources: () =>
    request<ProjectResources>("/projects/resources", { cache: "no-store" }),
  create: (body: ProjectFields, idempotencyKey: string) =>
    request<Project>("/projects", {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: JSON.stringify(body),
    }),
  update: (
    id: string,
    body: Partial<ProjectFields> & { expected_revision: number },
  ) =>
    request<Project>(`/projects/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
};

export interface TaskSummary {
  task_id: string;
  project_id: string | null;
  title: string;
  archived: boolean;
  active_turn_id: string | null;
  list_revision: number;
  created_at: string;
  updated_at: string;
}
export interface TaskTurn {
  sequence: number;
  turn_id: string;
  request_id: string;
  prompt: string;
  reply: string;
  status: "queued" | "running" | "completed" | "failed" | "cancelled";
  error_code: string;
  activity: string;
  model_ref: string;
  options: TaskExecutionOptions;
  project_revision: number | null;
  retry_of_turn_id: string | null;
  cancel_requested: boolean;
  created_at: string;
  updated_at: string;
}
export interface TaskDetail extends TaskSummary {
  turns: TaskTurn[];
  next_page_cursor: string | null;
  event_cursor: string | null;
}
export interface TaskUpdate {
  task: TaskSummary;
  turn: TaskTurn;
  cursor: string;
}
export type TaskEvent =
  | { type: "turn"; data: TaskUpdate }
  | { type: "snapshot"; data: TaskDetail };

async function readTaskEvents(
  id: string,
  cursor: string,
  onEvent: (event: TaskEvent) => void,
  signal: AbortSignal,
  onOpen?: () => void,
): Promise<void> {
  const { body } = await requestStream(
    `${taskPath(id)}/events${query({ cursor })}`,
    { signal, cache: "no-store", headers: { Accept: "text/event-stream" } },
  );
  onOpen?.();
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      // Normalize complete SSE lines, including CRLF split across network chunks.
      buffer = buffer.replace(/\r\n/g, "\n");
      let boundary: number;
      while ((boundary = buffer.indexOf("\n\n")) >= 0) {
        const frame = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const lines = frame.split("\n");
        const type = lines
          .find((line) => line.startsWith("event:"))
          ?.slice(6)
          .trim();
        const data = lines
          .filter((line) => line.startsWith("data:"))
          .map((line) => line.slice(5).trimStart())
          .join("\n");
        if (type === "error") throw new Error(data);
        if (type === "turn")
          onEvent({ type, data: JSON.parse(data) as TaskUpdate });
        if (type === "snapshot")
          onEvent({ type, data: JSON.parse(data) as TaskDetail });
      }
    }
  } finally {
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}

export interface TaskExecutionOptions {
  model_ref?: string;
  skills?: ProjectSkill[];
  mcp_servers?: string[];
}
export interface TaskResources extends ProjectResources {
  models: { model_ref: string; name: string }[];
  default_model_ref: string | null;
}
export interface TaskMessage {
  request_id: string;
  prompt: string;
  options?: TaskExecutionOptions;
}
const taskPath = (id: string) => `/project-tasks/${encodeURIComponent(id)}`;
export interface TaskListPage {
  items: TaskSummary[];
  next_page_cursor: string | null;
  sync_cursor: string | null;
}
export interface TaskSyncPage {
  base_sync_cursor: string;
  next_sync_cursor: string | null;
  next_page_cursor: string | null;
  changes: {
    operation: "upsert";
    task_id: string;
    list_revision: number;
    data: TaskSummary;
  }[];
}
const taskListPath = (projectID: string | null) =>
  projectID
    ? `/projects/${encodeURIComponent(projectID)}/tasks`
    : "/project-tasks";
export const projectTasksApi = {
  resources: () =>
    request<TaskResources>("/project-tasks/resources", { cache: "no-store" }),
  update: (
    id: string,
    body: { expected_revision: number; title?: string; archived?: boolean },
  ) =>
    request<TaskDetail>(taskPath(id), {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  list: (
    projectID: string | null,
    pageCursor?: string | null,
    archived = false,
  ) =>
    request<TaskListPage>(
      `${taskListPath(projectID)}${query({
        page_cursor: pageCursor,
        archived: archived ? "true" : undefined,
      })}`,
      { cache: "no-store" },
    ),
  sync: (
    projectID: string | null,
    syncCursor: string,
    pageCursor?: string | null,
    archived = false,
  ) =>
    request<TaskSyncPage>(
      `${taskListPath(projectID)}/sync${query({
        sync_cursor: syncCursor,
        page_cursor: pageCursor,
        archived: archived ? "true" : undefined,
      })}`,
      { cache: "no-store" },
    ),
  get: (id: string, pageCursor?: string | null) =>
    request<TaskDetail>(
      `${taskPath(id)}${query({ page_cursor: pageCursor })}`,
      { cache: "no-store" },
    ),
  events: readTaskEvents,
  create: (projectID: string | null, body: TaskMessage) =>
    request<TaskDetail>("/project-tasks", {
      method: "POST",
      body: JSON.stringify({ ...body, project_id: projectID }),
    }),
  send: (id: string, body: TaskMessage) =>
    request<TaskDetail>(`${taskPath(id)}/messages`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  cancel: (id: string, turnID: string) =>
    request<TaskDetail>(`${taskPath(id)}/cancel`, {
      method: "POST",
      body: JSON.stringify({ turn_id: turnID }),
    }),
  retry: (
    id: string,
    turnID: string,
    requestID: string,
    options?: TaskExecutionOptions,
  ) =>
    request<TaskDetail>(
      `${taskPath(id)}/turns/${encodeURIComponent(turnID)}/retry`,
      {
        method: "POST",
        body: JSON.stringify({ request_id: requestID, options }),
      },
    ),
};
