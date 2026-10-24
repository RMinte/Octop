import { request } from "../request";
import type { TaskMessage } from "./projects";

export interface ResidentScope {
  agent_id: string;
  thread_id: string;
}
export interface ResidentBinding {
  status: "not_created" | "initializing" | "ready" | "failed";
  agent_id: string | null;
  thread_id: string | null;
  name: string | null;
  timezone: string;
  error_code: string | null;
}
export interface ResidentCapabilities {
  api_version: 1;
  identity: "private_agent_and_thread";
  identity_initialization: "server_managed";
  assistant_role: "life_work_steward";
  professional_work: "delegated_tasks";
  conversation_transport: "rest_polling";
  conversation_stream: {
    transport: "websocket";
    path: "/api/project-tasks/ws";
    protocol_version: 3;
  };
  message_types: "text"[];
  history_days_search_location: boolean;
  memory_management: boolean;
  memory_capture: "explicit_opt_in_raw_events";
  memory_candidates: "explicit_extract_and_user_approval";
  task_delegation: "natural_language";
  turn_read_tools: boolean;
  selected_knowledge_search: boolean;
  model_task_delegation: "one_per_turn";
  followup_kinds: ("note" | "task_watch" | "task")[];
  notification_target: "conversation";
  shared_read_watermark: boolean;
  context_compaction: boolean;
}
export interface ResidentAuthorization {
  read_tools?: boolean;
  knowledge_base_ids?: string[];
  task_context?: { project_id: string | null; message: TaskMessage } | null;
}
export interface ResidentMessage extends ResidentAuthorization {
  request_id: string;
  prompt: string;
}
export interface ResidentTurn {
  turn_id: string;
  revision: number;
  request_id: string;
  user_message_id: string;
  assistant_message_id: string;
  prompt: string;
  reply: string;
  status:
    | "queued"
    | "running"
    | "completed"
    | "failed"
    | "cancelled"
    | "interrupted";
  created_at: string;
  updated_at: string;
  error_code: string | null;
  error_message: string | null;
  authorization: ResidentAuthorization;
}
export interface ResidentSnapshot extends ResidentScope {
  revision: number;
  active_turn_id: string | null;
  has_earlier: boolean;
  turns: ResidentTurn[];
}
export interface ResidentTaskReport {
  report_id: string;
  message_id: string;
  task_id: string;
  task_turn_id: string;
  status: "completed" | "failed" | "cancelled";
  text: string;
  error_code: string | null;
  error_message: string | null;
  artifacts: Record<string, unknown>[];
  delivery_status: "pending" | "delivered";
}
export interface ResidentHistoryNotification {
  notification_id: string;
  message_id: string;
  followup_id: string;
  kind: "note" | "task_watch" | "task";
  text: string;
  payload: Record<string, unknown>;
  status: "delivered";
}
export interface ResidentHistoryItem {
  item_id: string;
  kind: "turn" | "task_report" | "notification";
  day_key: string;
  timezone: string;
  created_at: string;
  updated_at: string;
  turn: ResidentTurn | null;
  report: ResidentTaskReport | null;
  notification: ResidentHistoryNotification | null;
}
export interface ResidentHistoryPage extends ResidentScope {
  history_revision: number;
  items: ResidentHistoryItem[];
  next_cursor: string | null;
}
export interface ResidentHistoryDays extends ResidentScope {
  history_revision: number;
  days: { day_key: string; item_count: number; timezones: string[] }[];
  next_cursor: string | null;
}
export interface ResidentHistoryLocation extends ResidentScope {
  history_revision: number;
  anchor_item_id: string;
  items: ResidentHistoryItem[];
  has_before: boolean;
  has_after: boolean;
}
export interface ResidentInbox extends ResidentScope {
  unread_count: number;
  last_message_id: string | null;
  last_read_message_id: string | null;
  revision: number;
}
export type ResidentMemoryKind = "Fact" | "Decision" | "Task" | "Preference";
export type ResidentMemoryCandidateStatus =
  | "pending"
  | "promoted"
  | "rejected"
  | "needs_review"
  | "conflict"
  | "extractor_failed";
export interface ResidentMemoryWrite {
  assertion: string;
  kind?: ResidentMemoryKind;
  source_turn_id?: string | null;
}
export interface ResidentMemoryItem {
  atom_id: string;
  assertion: string;
  kind: string;
  created_at: string;
  deprecated_at: string | null;
  superseded_by: string | null;
  source_event_ids: string[];
  quote_event_id: string;
}
export interface ResidentMemoryPage {
  items: ResidentMemoryItem[];
  has_more: boolean;
}
export interface ResidentMemoryCandidate {
  candidate_id: string;
  assertion: string;
  kind: string;
  status: ResidentMemoryCandidateStatus;
  quote: string;
  source_event_ids: string[];
  quote_event_id: string;
  created_at: string;
  decided_at: string | null;
  decided_by: string | null;
}
export interface ResidentMemoryCandidatePage {
  items: ResidentMemoryCandidate[];
  has_more: boolean;
}
export interface ResidentMemorySource {
  event_id: string;
  event_type: string;
  content: string;
  timestamp: string;
  source_turn_id: string | null;
  source_message_id: string | null;
  history_item_id: string | null;
}
export interface ResidentMemorySources {
  items: ResidentMemorySource[];
}
export interface ResidentMemoryPolicy {
  capture_enabled: boolean;
  revision: number;
  mode: "disabled" | "raw_capture_only";
}
export interface ResidentMemoryExtraction {
  status: "completed" | "failed" | "empty";
  candidates: number;
  events_extracted: number;
  pending_events: number;
  has_earlier: boolean;
  truncated_events: number;
  scope: "recent_user_messages";
  error_code: string | null;
}
export interface ResidentHistoryQuery {
  day_key?: string;
  cursor?: string;
  limit?: number;
}

export const residentScopePath = (scope: ResidentScope) =>
  `/agents/${encodeURIComponent(scope.agent_id)}/threads/${encodeURIComponent(
    scope.thread_id,
  )}`;
const query = (values: object = {}) => {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(values))
    if (value !== undefined && value !== null) params.set(key, String(value));
  return params.size ? `?${params}` : "";
};
const post = <T>(path: string, body?: unknown) =>
  request<T>(path, {
    method: "POST",
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
const memoryPath = (scope: ResidentScope) =>
  `${residentScopePath(scope)}/memory`;

/** All scope is explicit; these calls never depend on the selected ordinary expert. */
export const residentApi = {
  binding: () => request<ResidentBinding>("/agents/resident-conversation"),
  capabilities: () =>
    request<ResidentCapabilities>("/agents/resident-capabilities"),
  repair: () =>
    request<ResidentBinding>("/agents/resident-conversation", {
      method: "PUT",
    }),
  snapshot: (scope: ResidentScope, limit = 50) =>
    request<ResidentSnapshot>(
      `${residentScopePath(scope)}/conversation${query({ limit })}`,
    ),
  send: (scope: ResidentScope, body: ResidentMessage) =>
    post<ResidentTurn>(`${residentScopePath(scope)}/messages`, body),
  request: (scope: ResidentScope, id: string) =>
    request<ResidentTurn>(
      `${residentScopePath(scope)}/requests/${encodeURIComponent(id)}`,
    ),
  cancel: (
    scope: ResidentScope,
    body: { request_id: string; turn_id: string },
  ) => post<ResidentTurn>(`${residentScopePath(scope)}/cancel`, body),
  history: (scope: ResidentScope, values: ResidentHistoryQuery = {}) =>
    request<ResidentHistoryPage>(
      `${residentScopePath(scope)}/conversation/history${query(values)}`,
    ),
  searchHistory: (
    scope: ResidentScope,
    q: string,
    cursor?: string,
    limit = 40,
  ) =>
    request<ResidentHistoryPage>(
      `${residentScopePath(scope)}/conversation/history/search${query({
        q,
        cursor,
        limit,
      })}`,
    ),
  historyDays: (
    scope: ResidentScope,
    values: { cursor?: string; limit?: number; at?: string } = {},
  ) =>
    request<ResidentHistoryDays>(
      `${residentScopePath(scope)}/conversation/history/days${query(values)}`,
    ),
  locateHistory: (scope: ResidentScope, id: string, before = 20, after = 20) =>
    request<ResidentHistoryLocation>(
      `${residentScopePath(
        scope,
      )}/conversation/history/items/${encodeURIComponent(id)}${query({
        before,
        after,
      })}`,
    ),
  inbox: (scope: ResidentScope) =>
    request<ResidentInbox>(`${residentScopePath(scope)}/conversation/inbox`),
  markRead: (scope: ResidentScope, message_id: string) =>
    post<ResidentInbox>(`${residentScopePath(scope)}/conversation/read`, {
      message_id,
    }),
  memory: (
    scope: ResidentScope,
    values: { limit?: number; include_deprecated?: boolean } = {},
  ) => request<ResidentMemoryPage>(`${memoryPath(scope)}${query(values)}`),
  saveMemory: (scope: ResidentScope, body: ResidentMemoryWrite) =>
    post<ResidentMemoryItem>(memoryPath(scope), body),
  replaceMemory: (scope: ResidentScope, id: string, assertion: string) =>
    post<ResidentMemoryItem>(
      `${memoryPath(scope)}/${encodeURIComponent(id)}:replace`,
      { assertion },
    ),
  deprecateMemory: (scope: ResidentScope, id: string, reason = "") =>
    post<ResidentMemoryItem>(
      `${memoryPath(scope)}/${encodeURIComponent(id)}:deprecate`,
      { reason },
    ),
  memoryPolicy: (scope: ResidentScope) =>
    request<ResidentMemoryPolicy>(`${memoryPath(scope)}/policy`),
  updateMemoryPolicy: (
    scope: ResidentScope,
    body: { capture_enabled: boolean; expected_revision: number },
  ) =>
    request<ResidentMemoryPolicy>(`${memoryPath(scope)}/policy`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  candidates: (
    scope: ResidentScope,
    values: { limit?: number; status?: ResidentMemoryCandidateStatus } = {},
  ) =>
    request<ResidentMemoryCandidatePage>(
      `${memoryPath(scope)}/candidates${query(values)}`,
    ),
  extractCandidates: (scope: ResidentScope) =>
    post<ResidentMemoryExtraction>(`${memoryPath(scope)}/candidates:extract`),
  candidate: (scope: ResidentScope, id: string) =>
    request<ResidentMemoryCandidate>(
      `${memoryPath(scope)}/candidates/${encodeURIComponent(id)}`,
    ),
  approveCandidate: (scope: ResidentScope, id: string) =>
    post<ResidentMemoryCandidate>(
      `${memoryPath(scope)}/candidates/${encodeURIComponent(id)}:approve`,
    ),
  rejectCandidate: (scope: ResidentScope, id: string, reason = "") =>
    post<ResidentMemoryCandidate>(
      `${memoryPath(scope)}/candidates/${encodeURIComponent(id)}:reject`,
      { reason },
    ),
  memorySources: (scope: ResidentScope, id: string) =>
    request<ResidentMemorySources>(
      `${memoryPath(scope)}/${encodeURIComponent(id)}/sources`,
    ),
  candidateSources: (scope: ResidentScope, id: string) =>
    request<ResidentMemorySources>(
      `${memoryPath(scope)}/candidates/${encodeURIComponent(id)}/sources`,
    ),
};
