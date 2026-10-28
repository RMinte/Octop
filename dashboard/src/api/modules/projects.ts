import { taskTransport } from "../taskTransport";
import { getProjectTaskSocket } from "./projectTaskSocket";
import type { ReasoningCapability } from "../types/provider";
import type { FileTreeNode } from "../types";

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
export function createProjectsApi(hostID: string | null = null) {
  const { request } = taskTransport(hostID);
  return {
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
    workspaceTree: (
      id: string,
      directory: string,
      revision: number,
      signal?: AbortSignal,
    ) =>
      request<FileTreeNode[]>(
        `/projects/${encodeURIComponent(id)}/workspace/tree-children?${query({
          path: directory,
          expected_project_revision: String(revision),
        })}`,
        { cache: "no-store", signal },
      ),
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
      request<void>(`/projects/${encodeURIComponent(id)}`, {
        method: "DELETE",
      }),
  };
}
export const projectsApi = createProjectsApi();

export type TaskStatus =
  | "queued"
  | "running"
  | "waiting_approval"
  | "waiting_answer"
  | "waiting_client_tool"
  | "paused"
  | "interrupted"
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
export interface EngineSelection {
  engine_id: "octop" | "acp";
  connection_id: string | null;
  model_id: string | null;
}
export interface EngineCapabilities {
  action_modes: TaskActionMode[];
  images: boolean;
  permissions: boolean;
  native_resume: boolean;
  pause: boolean;
  cancel: boolean;
  skills: boolean;
  mcp: boolean;
  plugins: boolean;
  subagents: boolean;
  knowledge: boolean;
  memory: boolean;
  compact: boolean;
  model_selection: boolean;
}
export interface EngineCatalogEntry {
  engine: EngineSelection;
  label: string;
  available: boolean;
  unavailable_reason: string | null;
  capabilities: EngineCapabilities;
  models: { model_id: string; label: string }[];
}
export interface TaskEngineSnapshot extends EngineSelection {
  label: string;
  capabilities: EngineCapabilities;
  actual_model_id?: string | null;
  resumed?: boolean | null;
}
export interface TaskSummary {
  task_id: string;
  project_id: string | null;
  title: string;
  archived: boolean;
  status: TaskStatus;
  active_turn_id: string | null;
  list_revision: number;
  selected_expert_id?: string | null;
  engine?: EngineSelection;
  configuration_revision?: number;
  action_mode?: TaskActionMode;
  pinned?: boolean;
  approval_policy: TaskApprovalPolicy;
  web_search_enabled: boolean;
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
  workspace_path?: string | null;
  created_at: string;
}
export interface TaskPlanStep {
  content: string;
  status: "pending" | "in_progress" | "completed" | "blocked" | "skipped";
  // Historic records may omit these fields; new responses use explicit nulls.
  step_id?: string | null;
  result?: string;
  started_at?: string | null;
  finished_at?: string | null;
  removed_at?: string | null;
}
export interface TaskPlan {
  source: "octop_planning" | "harness_todos";
  revision: number;
  plan_id?: string | null;
  title?: string;
  active_step_id?: string | null;
  steps: TaskPlanStep[];
  retired_steps?: TaskPlanStep[];
}
export interface TaskContentBlock {
  block_id: string;
  type:
    | "text"
    | "reasoning"
    | "reasoning_summary"
    | "tool"
    | "plan"
    | "progress";
  text: string;
  status: "streaming" | "completed" | "failed" | "interrupted";
  tool_call_id: string | null;
  tool_name: string | null;
  tool_label?: string | null;
  speaker_agent_id?: string | null;
  speaker_name?: string | null;
  brief?: string | null;
  plan?: TaskPlan | null;
  plan_id?: string | null;
  step_id?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  duration_ms?: number | null;
  arguments?: Record<string, unknown> | null;
  output?: unknown | null;
}
export interface TaskInteraction {
  interaction_id: string;
  task_id: string;
  turn_id: string;
  tool_call_id: string;
  supersedes_interaction_id?: string | null;
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
export interface TaskRecovery {
  can_resume: boolean;
  needs_reconciliation: boolean;
  phase: string;
  reason_code: string;
  cause_type: string;
  diagnostic_id: string;
  resource_type: string | null;
  resource_id: string | null;
  status_code: number | null;
}
export interface TaskFileChange {
  path: string;
  additions: number | null;
  deletions: number | null;
  diff: string | null;
}
export interface TaskSourceEvent {
  source_event_id?: string | number;
  source_sequence?: number;
  source_turn_id?: string | null;
  source_agent_id?: string | null;
  kind: string;
  created_at?: string | number | null;
  summary?: string;
  payload?: unknown;
  speaker?: unknown;
  source_status: "recorded" | "incomplete" | "redacted";
  mapping?: "tool_call" | "message" | "source_thread";
  history_only: true;
  restorable: false;
}
export interface TaskTurn {
  execution_engine?: TaskEngineSnapshot;
  source_trajectory?: TaskSourceEvent[];
  history_only?: boolean;
  source_task_id?: string | null;
  source_turn_id?: string | null;
  source_status?: TaskStatus | null;
  sequence: number;
  turn_id: string;
  request_id: string;
  prompt: string;
  attachments: TaskInputAttachment[];
  reply: string;
  reasoning: string;
  content_blocks: TaskContentBlock[];
  content_revision: number;
  plan: TaskPlan | null;
  plan_revision: number;
  status: TaskStatus;
  error_code: string;
  recovery?: TaskRecovery | null;
  activity: string;
  artifacts: TaskArtifact[];
  file_changes?: TaskFileChange[];
  model_ref: string;
  input_tokens?: number;
  output_tokens?: number;
  options: TaskExecutionOptions;
  presentation?: TaskPresentationSettings;
  web_search_enabled: boolean;
  project_revision: number | null;
  server_directory?: string | null;
  client_directory?: string | null;
  selected_expert_id?: string | null;
  expert_name?: string | null;
  expert_config_revision?: number | null;
  action_mode?: TaskActionMode;
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
  configuration_events?: {
    event_id: string;
    previous_expert_id: string | null;
    selected_expert_id: string | null;
    engine?: EngineSelection;
    previous_engine?: EngineSelection;
    previous_action_mode: TaskActionMode;
    action_mode: TaskActionMode;
    configuration_revision: number;
    created_at: string;
  }[];
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
  action_timeline_enabled?: boolean | null;
  reasoning_mode?: "auto" | "enabled" | "disabled";
  reasoning_effort?: string;
  model_ref?: string;
  routing?: {
    mode: "box" | "hybrid";
    level?: 0 | 20 | 40 | 60 | 80 | 100 | null;
  } | null;
  skills?: ProjectSkill[];
  mcp_servers?: string[];
  plugins?: string[];
  subagents?: string[];
  knowledge_base_ids?: string[] | null;
  memory_enabled?: boolean;
  expert_memory_enabled?: boolean;
  team_enabled?: boolean;
}
export interface TaskResources extends ProjectResources {
  execution_engines?: EngineCatalogEntry[];
  experts?: {
    expert_id: string;
    name: string;
    description: string;
    config_revision: number;
    kind?: "expert" | "team";
    is_shared?: boolean;
    is_owner?: boolean;
    member_ids?: string[];
    default_skills?: ProjectSkill[];
    has_workspace_skills?: boolean;
  }[];
  approval: TaskApprovalCapabilities;
  models: {
    model_ref: string;
    provider_kind?: string;
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
  expected_configuration_revision?: number;
  expected_project_revision?: number | null;
}
export type TaskActionMode = "ask" | "plan" | "craft";
export interface TaskCreate extends TaskMessage {
  engine?: EngineSelection;
  web_search_enabled?: boolean;
  selected_expert_id?: string | null;
  action_mode?: TaskActionMode;
}
export interface TaskPatch {
  engine?: EngineSelection;
  expected_revision: number;
  title?: string;
  archived?: boolean;
  web_search_enabled?: boolean;
  selected_expert_id?: string | null;
  action_mode?: TaskActionMode;
  pinned?: boolean;
  expected_configuration_revision?: number;
}
export interface TaskListFilters {
  scope?: "all" | "standalone";
  expert_id?: string;
  status?: TaskStatus;
  q?: string;
}
export interface TaskFork {
  engine?: EngineSelection;
  action_mode?: TaskActionMode;
  request_id: string;
  turn_id?: string;
  operation: "fork" | "edit" | "regenerate";
  prompt?: string;
  project_id?: string | null;
  selected_expert_id?: string | null;
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
export interface TaskPresentationSettings {
  action_timeline_enabled: boolean;
  step_panel_enabled: boolean;
}
export interface TaskContextUsage {
  max_tokens: number;
  used_tokens: number;
  input_tokens: number;
  output_tokens: number;
  segments: { key: string; tokens: number }[];
  source: "model_request" | "empty";
  estimated: boolean;
}
export function createProjectTasksApi(hostID: string | null = null) {
  const { request, requestBlob, requestUpload } = taskTransport(hostID);
  const projectTaskSocket = getProjectTaskSocket(hostID);
  return {
    fromThread: (body: {
      request_id: string;
      agent_id: string;
      thread_id: string;
      project_id: string | null;
      selected_expert_id?: string | null;
    }) =>
      request<TaskDetail>("/project-tasks/from-thread", {
        method: "POST",
        body: JSON.stringify(body),
      }),
    fork: (id: string, body: TaskFork) =>
      request<TaskDetail>(`${taskPath(id)}/forks`, {
        method: "POST",
        body: JSON.stringify(body),
      }),
    approvalPolicy: () =>
      request<TaskApprovalPolicy>("/project-tasks/approval-policy", {
        cache: "no-store",
      }),
    updateApprovalPolicy: (body: TaskApprovalUpdate) =>
      request<TaskApprovalPolicy>("/project-tasks/approval-policy", {
        method: "PUT",
        body: JSON.stringify(body),
      }),
    approvalEvents: projectTaskSocket.approvalEvents.bind(projectTaskSocket),
    settings: () =>
      request<TaskPresentationSettings>("/project-tasks/settings", {
        cache: "no-store",
      }),
    updateSettings: (body: TaskPresentationSettings) =>
      request<TaskPresentationSettings>("/project-tasks/settings", {
        method: "PUT",
        body: JSON.stringify(body),
      }),
    contextUsage: (id: string, signal?: AbortSignal) =>
      request<TaskContextUsage>(`${taskPath(id)}/context-usage`, {
        signal,
        cache: "no-store",
      }),
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
    downloadArtifact: (
      taskID: string,
      artifactID: string,
      signal?: AbortSignal,
    ) =>
      requestBlob(
        `${taskPath(taskID)}/artifacts/${encodeURIComponent(
          artifactID,
        )}/download`,
        { signal },
      ),
    resources: () =>
      request<TaskResources>("/project-tasks/resources", { cache: "no-store" }),
    engines: () =>
      request<EngineCatalogEntry[]>("/project-tasks/engines", {
        cache: "no-store",
      }),
    inspectEngine: (engine: EngineSelection) =>
      request<EngineCatalogEntry>("/project-tasks/engines/inspect", {
        method: "POST",
        body: JSON.stringify(engine),
      }),
    update: (id: string, body: TaskPatch) =>
      request<TaskDetail>(taskPath(id), {
        method: "PATCH",
        body: JSON.stringify(body),
      }),
    list: (
      projectID: string | null,
      pageCursor?: string | null,
      archived = false,
      filters?: TaskListFilters,
    ) =>
      request<TaskListPage>(
        `${taskListPath(projectID)}${query({
          page_cursor: pageCursor,
          archived: archived ? "true" : undefined,
          ...filters,
        })}`,
        { cache: "no-store" },
      ),
    sync: (
      projectID: string | null,
      syncCursor: string,
      pageCursor?: string | null,
      archived = false,
      filters?: TaskListFilters,
    ) =>
      request<TaskSyncPage>(
        `${taskListPath(projectID)}/sync${query({
          sync_cursor: syncCursor,
          page_cursor: pageCursor,
          archived: archived ? "true" : undefined,
          ...filters,
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
      revisions?: Pick<
        TaskMessage,
        "expected_configuration_revision" | "expected_project_revision"
      >,
    ) =>
      projectTaskSocket.command<TaskDetail>("task.retry", {
        task_id: id,
        turn_id: turnID,
        data: { request_id: requestID, options, ...revisions },
      }),
  };
}
export const projectTasksApi = createProjectTasksApi();
