import { taskTransport } from "../taskTransport";

export interface TaskMemoryEntry {
  id: string;
  content: string;
  layer: "atom" | "raw";
  editable: boolean;
  created_at?: string | null;
  updated_at?: string | null;
}
export function createTaskExtrasApi(hostID: string | null = null) {
  const { request, requestBlob } = taskTransport(hostID);
  const path = (id: string) => `/project-tasks/${encodeURIComponent(id)}`;
  return {
    export: (id: string, format: "json" | "markdown") =>
      requestBlob(`${path(id)}/export?format=${format}`),
    polish: (body: {
      text: string;
      task_id?: string;
      selected_expert_id?: string | null;
      model_ref?: string;
    }) =>
      request<{ text: string }>("/project-tasks/polish", {
        method: "POST",
        body: JSON.stringify(body),
      }),
    memory: (id: string, q: string, signal?: AbortSignal) =>
      request<{
        task_id: string;
        enabled: boolean;
        entries: TaskMemoryEntry[];
      }>(`${path(id)}/memory?${new URLSearchParams({ q, limit: "100" })}`, {
        signal,
        cache: "no-store",
      }),
    saveMemory: (
      id: string,
      body: { request_id: string; content: string; id?: string },
    ) =>
      request<TaskMemoryEntry>(`${path(id)}/memory`, {
        method: "POST",
        body: JSON.stringify(body),
      }),
    deleteMemory: (id: string, entryID: string, requestID: string) =>
      request(
        `${path(id)}/memory?${new URLSearchParams({
          id: entryID,
          request_id: requestID,
        })}`,
        { method: "DELETE" },
      ),
  };
}
export const taskExtrasApi = createTaskExtrasApi();
