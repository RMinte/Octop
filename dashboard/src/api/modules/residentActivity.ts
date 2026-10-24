import { request } from "../request";
import type { ResidentScope } from "./resident";
import type { TaskMessage } from "./projects";

export interface ResidentTaskReport {
  report_id: string;
  message_id: string;
  task_id: string;
  task_turn_id: string;
  status: string;
  text: string;
  error_code: string | null;
  artifacts: { artifact_id: string; name: string }[];
  captured_at: string;
  delivery_status: "pending" | "delivered";
}
export interface ResidentDelegation {
  delegation_id: string;
  source_turn_id: string;
  task_id: string;
  status: string;
  task_exists: boolean;
  reports: ResidentTaskReport[];
  created_at: string;
}
export interface FollowupFields {
  purpose: string;
  kind: "note" | "task_watch" | "task";
  schedule: string | null;
  subject_task_id: string | null;
  payload:
    | { text: string }
    | {
        source_turn_id: string;
        project_id: string | null;
        message: TaskMessage;
      };
  capabilities: ("conversation_notify" | "task_create")[];
  notification_target: "conversation";
}
export interface ResidentFollowup extends FollowupFields {
  followup_id: string;
  request_id: string;
  timezone: string;
  status: "enabled" | "paused" | "cancelled" | "completed";
  revision: number;
  next_fire_at: string | null;
  last_task_id: string | null;
  last_error_code: string | null;
}
export interface ResidentNotification {
  notification_id: string;
  message_id: string;
  text: string;
  status: "pending" | "delivered" | "discarded";
  created_at: string;
  timezone: string;
  delivery_error: string | null;
}
export interface ResidentContextUsage {
  max_tokens: number;
  used_tokens: number;
  input_tokens: number;
  output_tokens: number;
  source: "model_request" | "empty";
  segments: { key: string; tokens: number }[];
}
export interface ResidentContextCompact {
  ok: boolean;
  reason: "ok" | "nothing_to_compact" | "unavailable" | "error";
  summarized_count: number;
  preserved_count: number;
  removed_tokens: number;
}
export interface ResidentActivityPage<T> {
  items: T[];
  next_page_cursor: string | null;
}
const base = (scope: ResidentScope) =>
  `/agents/${encodeURIComponent(scope.agent_id)}/threads/${encodeURIComponent(
    scope.thread_id,
  )}/conversation`;
const page = <T>(
  scope: ResidentScope,
  resource: string,
  cursor?: string,
  signal?: AbortSignal,
) => {
  const query = new URLSearchParams({ limit: "30" });
  if (cursor) query.set("page_cursor", cursor);
  return request<ResidentActivityPage<T>>(
    `${base(scope)}/${resource}?${query}`,
    {
      cache: "no-store",
      signal,
    },
  );
};

export function followupReplacement(
  row: ResidentFollowup,
  status: "enabled" | "paused",
) {
  return {
    purpose: row.purpose,
    kind: row.kind,
    schedule: row.schedule,
    subject_task_id: row.subject_task_id,
    payload: row.payload,
    capabilities: row.capabilities,
    notification_target: row.notification_target,
    expected_revision: row.revision,
    status,
  };
}

export const residentActivityApi = {
  delegations: (scope: ResidentScope, cursor?: string, signal?: AbortSignal) =>
    page<ResidentDelegation>(scope, "delegations", cursor, signal),
  followups: (scope: ResidentScope, cursor?: string, signal?: AbortSignal) =>
    page<ResidentFollowup>(scope, "followups", cursor, signal),
  notifications: (
    scope: ResidentScope,
    cursor?: string,
    signal?: AbortSignal,
  ) => page<ResidentNotification>(scope, "notifications", cursor, signal),
  createFollowup: (
    scope: ResidentScope,
    body: FollowupFields & { request_id: string },
  ) =>
    request<ResidentFollowup>(`${base(scope)}/followups`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  updateFollowup: (
    scope: ResidentScope,
    row: ResidentFollowup,
    status: "enabled" | "paused",
  ) =>
    request<ResidentFollowup>(
      `${base(scope)}/followups/${encodeURIComponent(row.followup_id)}`,
      {
        method: "PUT",
        body: JSON.stringify(followupReplacement(row, status)),
      },
    ),
  cancelFollowup: (scope: ResidentScope, row: ResidentFollowup) =>
    request<ResidentFollowup>(
      `${base(scope)}/followups/${encodeURIComponent(row.followup_id)}/cancel`,
      {
        method: "POST",
        body: JSON.stringify({ expected_revision: row.revision }),
      },
    ),
  context: (scope: ResidentScope, signal?: AbortSignal) =>
    request<ResidentContextUsage>(`${base(scope)}/context`, {
      cache: "no-store",
      signal,
    }),
  compact: (scope: ResidentScope) =>
    request<ResidentContextCompact>(`${base(scope)}/context:compact`, {
      method: "POST",
    }),
};
