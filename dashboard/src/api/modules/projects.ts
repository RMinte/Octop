import { request } from "../request";

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
  active_turn_id: string | null;
  list_revision: number;
  created_at: string;
  updated_at: string;
}
export interface TaskTurn {
  turn_id: string;
  request_id: string;
  prompt: string;
  reply: string;
  status: "queued" | "running" | "completed" | "failed" | "cancelled";
  error_code: string;
  activity: string;
  model_ref: string;
  project_revision: number | null;
  created_at: string;
  updated_at: string;
}
export interface TaskDetail extends TaskSummary {
  turns: TaskTurn[];
}
export interface TaskMessage {
  request_id: string;
  prompt: string;
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
  list: (projectID: string | null, pageCursor?: string | null) =>
    request<TaskListPage>(
      `${taskListPath(projectID)}${query({ page_cursor: pageCursor })}`,
      { cache: "no-store" },
    ),
  sync: (
    projectID: string | null,
    syncCursor: string,
    pageCursor?: string | null,
  ) =>
    request<TaskSyncPage>(
      `${taskListPath(projectID)}/sync${query({
        sync_cursor: syncCursor,
        page_cursor: pageCursor,
      })}`,
      { cache: "no-store" },
    ),
  get: (id: string) => request<TaskDetail>(taskPath(id), { cache: "no-store" }),
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
  cancel: (id: string) =>
    request<TaskDetail>(`${taskPath(id)}/cancel`, { method: "POST" }),
};
