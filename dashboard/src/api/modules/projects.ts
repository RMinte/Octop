import { request, requestBlob, requestUpload } from "../request";
import { projectTaskSocket } from "./projectTaskSocket";
import type { ReasoningCapability } from "../types/provider";

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
  plugins: string[];
  subagents: string[];
  memory_enabled: boolean;
  team_enabled: boolean;
}
export interface Project extends ProjectFields {
  project_id: string;
  archived: boolean;
  revision: number;
  created_at: string;
  updated_at: string;
}
export interface ProjectResources {
  plugins: {
    plugin_id: string;
    name: string;
    description: string;
    available: boolean;
  }[];
  subagents: { slug: string; name: string; description: string }[];
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
  changes: (
    | { operation: "upsert"; project_id: string; data: Project }
    | { operation: "delete"; project_id: string; data: null }
  )[];
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
  archive: (id: string, archived: boolean, expectedRevision: number) =>
    request<Project>(`/projects/${encodeURIComponent(id)}/archive`, {
      method: "PATCH",
      body: JSON.stringify({ archived, expected_revision: expectedRevision }),
    }),
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
  delete: (id: string) =>
    request<void>(`/projects/${encodeURIComponent(id)}`, { method: "DELETE" }),
};

export type TaskStatus =
  | "queued"
  | "running"
  | "waiting_approval"
  | "waiting_answer"
  | "waiting_client_tool"
  | "paused"
  | "completed"
  | "failed"
  | "cancelled";
export type TaskApprovalMode =
  | "ask_for_approval"
  | "approve_for_me"
  | "full_access";
export interface TaskApprovalPolicy {
  mode: TaskApprovalMode;
  revision: number;
  updated_at: string;
}
export interface TaskApprovalCapabilities {
  allowed_modes: TaskApprovalMode[];
  reviewer_ready: boolean;
  execution_profile: "host_process";
}
export interface TaskSummary {
  task_id: string;
  project_id: string | null;
  title: string;
  archived: boolean;
  status: TaskStatus;
  active_turn_id: string | null;
  list_revision: number;
  approval_policy: TaskApprovalPolicy;
  created_at: string;
  updated_at: string;
}
export interface TaskArtifact {
  artifact_id: string;
  task_id: string;
  turn_id: string;
  name: string;
  media_type: string;
  byte_count: number;
  sha256: string;
  source_path: string | null;
  download_url: string;
  created_at: string;
}
export interface TaskInputAttachment {
  attachment_id: string;
  name: string;
  media_type: string;
  byte_count: number;
  sha256: string;
  preview_url: string;
  created_at: string;
}
export interface TaskContentBlock {
  block_id: string;
  type: "text" | "reasoning" | "reasoning_summary" | "tool";
  text: string;
  status: "streaming" | "completed" | "failed" | "interrupted";
  tool_call_id: string | null;
  tool_name: string | null;
}
export interface TaskInteraction {
  interaction_id: string;
  task_id: string;
  turn_id: string;
  tool_call_id: string;
  revision: number;
  status: "pending" | "reviewing" | "resolved" | "cancelled" | "expired";
  kind: "approval" | "question";
  payload: {
    tool_name?: string;
    arguments?: Record<string, unknown>;
    execution_host?: "server";
    reason?: string;
    action_digest?: string;
    allowed_decisions?: ("allow_once" | "deny")[];
    approval_mode?: TaskApprovalMode;
    approval_revision?: number;
    question?: string;
    header?: string | null;
    options?: { label: string; description: string }[];
  };
  resolution: { outcome: string; source: string; answer: string | null } | null;
  review?: {
    review_id: string;
    status: "queued" | "reviewing" | "completed" | "cancelled";
    decision: "allow_once" | "deny" | "requires_user" | null;
    reason_code: string;
    action_digest: string;
    context_digest: string | null;
    model_ref: string;
    profile_version: 1;
    created_at: string;
    updated_at: string;
    expires_at: string;
  } | null;
  created_at: string;
  expires_at: string;
  resolved_at: string | null;
}
export type InteractionResponse = {
  request_id: string;
  expected_revision: number;
  response:
    | {
        kind: "approval";
        decision: "allow_once" | "deny";
        action_digest: string;
      }
    | { kind: "question"; action: "answer"; answer: string }
    | { kind: "question"; action: "skip" | "use_default" };
};
export interface TaskTurn {
  sequence: number;
  turn_id: string;
  request_id: string;
  prompt: string;
  attachments: TaskInputAttachment[];
  reply: string;
  reasoning: string;
  content_blocks: TaskContentBlock[];
  content_revision: number;
  status: TaskStatus;
  error_code: string;
  activity: string;
  artifacts: TaskArtifact[];
  model_ref: string;
  options: TaskExecutionOptions;
  project_revision: number | null;
  approval_policy_at_start: TaskApprovalPolicy;
  retry_of_turn_id: string | null;
  cancel_requested: boolean;
  pause_requested: boolean;
  interactions: TaskInteraction[];
  interaction_revision: number;
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

export interface TaskExecutionOptions {
  reasoning_mode?: "auto" | "enabled" | "disabled";
  reasoning_effort?: string;
  model_ref?: string;
  skills?: ProjectSkill[];
  mcp_servers?: string[];
  plugins?: string[];
  subagents?: string[];
  memory_enabled?: boolean;
  team_enabled?: boolean;
}
export interface TaskResources extends ProjectResources {
  approval: TaskApprovalCapabilities;
  models: {
    model_ref: string;
    name: string;
    supports_images: boolean;
    reasoning_config?: ReasoningCapability | null;
  }[];
  default_model_ref: string | null;
}
export interface TaskMessage {
  request_id: string;
  prompt: string;
  attachment_ids?: string[];
  options?: TaskExecutionOptions;
}
export interface TaskCreate extends TaskMessage {
  approval_mode: TaskApprovalMode;
}
export interface TaskApprovalUpdate {
  request_id: string;
  expected_revision: number;
  mode: TaskApprovalMode;
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
  changes: (
    | {
        operation: "upsert";
        task_id: string;
        list_revision: number;
        data: TaskSummary;
      }
    | {
        operation: "delete";
        task_id: string;
        list_revision: number;
        data: null;
      }
  )[];
}
const taskListPath = (projectID: string | null) =>
  projectID
    ? `/projects/${encodeURIComponent(projectID)}/tasks`
    : "/project-tasks";
export const projectTasksApi = {
  uploadAttachment: (file: File, signal?: AbortSignal) => {
    const body = new FormData();
    body.append("file", file);
    return requestUpload<TaskInputAttachment>(
      "/project-tasks/attachments",
      body,
      { signal },
    );
  },
  attachmentPreview: (id: string, signal?: AbortSignal) =>
    requestBlob(`/project-tasks/attachments/${encodeURIComponent(id)}`, {
      signal,
      cache: "no-store",
    }),
  setApproval: (id: string, body: TaskApprovalUpdate) =>
    projectTaskSocket.command<TaskDetail>("task.approval.set", {
      task_id: id,
      data: body,
    }),
  respond: (
    id: string,
    turnID: string,
    interactionID: string,
    body: InteractionResponse,
  ) =>
    projectTaskSocket.command<TaskDetail>("task.interaction.respond", {
      task_id: id,
      turn_id: turnID,
      interaction_id: interactionID,
      data: body,
    }),
  control: (id: string, turnID: string, paused: boolean, requestID: string) =>
    projectTaskSocket.command<TaskDetail>(
      paused ? "task.pause" : "task.resume",
      {
        task_id: id,
        data: { turn_id: turnID, request_id: requestID },
      },
    ),
  delete: (id: string) =>
    request<void>(taskPath(id), {
      method: "DELETE",
    }),
  downloadArtifact: (taskID: string, artifactID: string) =>
    requestBlob(
      `${taskPath(taskID)}/artifacts/${encodeURIComponent(
        artifactID,
      )}/download`,
    ),
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
  open: (id: string) =>
    projectTaskSocket.command<TaskDetail>("task.open", { task_id: id }),
  events: projectTaskSocket.events.bind(projectTaskSocket),
  create: (projectID: string | null, body: TaskCreate) =>
    projectTaskSocket.command<TaskDetail>("task.create", {
      data: { ...body, project_id: projectID },
    }),
  send: (id: string, body: TaskMessage) =>
    projectTaskSocket.command<TaskDetail>("task.message", {
      task_id: id,
      data: body,
    }),
  cancel: (id: string, turnID: string) =>
    projectTaskSocket.command<TaskDetail>("task.cancel", {
      task_id: id,
      data: { turn_id: turnID },
    }),
  retry: (
    id: string,
    turnID: string,
    requestID: string,
    options?: TaskExecutionOptions,
  ) =>
    projectTaskSocket.command<TaskDetail>("task.retry", {
      task_id: id,
      turn_id: turnID,
      data: { request_id: requestID, options },
    }),
};
