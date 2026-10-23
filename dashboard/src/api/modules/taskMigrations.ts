import { request } from "../request";
export interface MigrationRequest {
  request_id: string;
  thread_ids: string[];
  project_id?: string | null;
}
export interface MigrationPreflight {
  candidates: {
    thread_id: string;
    agent_id: string;
    channel_type: string;
    disposition: string;
  }[];
  total: number;
  eligible: number;
  requires_model: false;
}
export interface MigrationBatch {
  batch_id: string;
  status: "queued" | "running" | "waiting" | "paused" | "completed";
  created_at: number;
  updated_at: number;
  items: {
    item_id: string;
    source_agent_id: string;
    source_thread_id: string;
    target_task_id: string;
    status:
      | "pending"
      | "running"
      | "completed"
      | "blocked"
      | "failed"
      | "skipped";
    phase: string;
    error_code: string;
    manifest: Record<string, unknown>;
  }[];
}
const path = "/project-task-migrations";
export const taskMigrationsApi = {
  resolve: (id: string, signal?: AbortSignal) =>
    request<{
      thread_id: string;
      task_id: string | null;
      state: "legacy" | "draining" | "task" | "target_deleted";
      url: string | null;
    }>(`${path}/legacy/${encodeURIComponent(id)}`, {
      signal,
      cache: "no-store",
    }),
  list: () => request<MigrationBatch[]>(path, { cache: "no-store" }),
  preflight: (body: MigrationRequest) =>
    request<MigrationPreflight>(`${path}/preflight`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  create: (body: MigrationRequest) =>
    request<MigrationBatch>(path, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  control: (id: string, action: "pause" | "resume") =>
    request<MigrationBatch>(`${path}/${encodeURIComponent(id)}/control`, {
      method: "POST",
      body: JSON.stringify({ action }),
    }),
  automatic: () =>
    request<{ enabled: boolean }>(`${path}/automatic`, { cache: "no-store" }),
  setAutomatic: (enabled: boolean) =>
    request<{ enabled: boolean }>(`${path}/automatic`, {
      method: "PUT",
      body: JSON.stringify({ enabled }),
    }),
};
