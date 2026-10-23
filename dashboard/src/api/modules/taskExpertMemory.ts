import { taskTransport } from "../taskTransport";

export type ExpertMemorySource = "scope" | "general" | "legacy";
export interface ExpertMemoryEntry {
  path: string;
  content: string;
  layer: string;
  occurred_at?: string | null;
  source_id?: string | null;
}
export interface ExpertMemoryPolicy {
  expert_id: string;
  expert_name: string;
  config_revision: number;
  enabled: boolean;
  capture_enabled: boolean;
  legacy_recall_enabled: boolean;
  auto_extract_enabled?: boolean;
  general_promotion_enabled?: boolean;
  policy_editable?: boolean;
}
export interface ExpertMemoryList {
  expert_id: string;
  source: ExpertMemorySource;
  entries: ExpertMemoryEntry[];
  enabled: boolean;
  extraction_jobs?: {
    job_id: string;
    turn_id: string;
    status:
      | "pending"
      | "running"
      | "completed"
      | "cancelled"
      | "failed"
      | "uncertain";
    phase: "queued" | "extracting" | "ready" | "applying" | "done";
    error_code?: string | null;
  }[];
}
export interface ExpertMemorySave {
  expert_id: string;
  source: ExpertMemorySource;
  status: "completed" | "uncertain";
  entry?: ExpertMemoryEntry | null;
  replayed: boolean;
}
const path = (id: string) =>
  `/project-tasks/${encodeURIComponent(id)}/expert-memory`;
export function createTaskExpertMemoryApi(hostID: string | null = null) {
  const { request } = taskTransport(hostID);
  return {
    list: (
      id: string,
      source: ExpertMemorySource,
      q: string,
      signal?: AbortSignal,
    ) =>
      request<ExpertMemoryList>(
        `${path(id)}?${new URLSearchParams({ source, q })}`,
        { cache: "no-store", signal },
      ),
    policy: (id: string, signal?: AbortSignal) =>
      request<ExpertMemoryPolicy>(`${path(id)}/policy`, {
        cache: "no-store",
        signal,
      }),
    updatePolicy: (
      id: string,
      body: {
        expected_expert_id: string;
        expected_revision: number;
        legacy_recall_enabled: boolean;
        capture_enabled?: boolean;
        auto_extract_enabled?: boolean;
        general_promotion_enabled?: boolean;
      },
    ) =>
      request<ExpertMemoryPolicy>(`${path(id)}/policy`, {
        method: "PATCH",
        body: JSON.stringify(body),
      }),
    save: (
      id: string,
      body: {
        expected_expert_id: string;
        request_id: string;
        source: "scope" | "general";
        content: string;
        topic?: string;
      },
    ) =>
      request<ExpertMemorySave>(path(id), {
        method: "POST",
        body: JSON.stringify(body),
      }),
  };
}
export const taskExpertMemoryApi = createTaskExpertMemoryApi();
