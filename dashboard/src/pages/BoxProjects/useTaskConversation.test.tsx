import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  projectTasksApi,
  type TaskDetail,
  type TaskPlan,
} from "../../api/modules/projects";
import { useTaskConversation } from "./useTaskConversation";

vi.mock("../../api/modules/projects", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../api/modules/projects")>()),
  projectTasksApi: { open: vi.fn(), get: vi.fn(), events: vi.fn() },
}));
vi.mock("react-i18next", () => {
  const t = (key: string) => key;
  return { useTranslation: () => ({ t }) };
});
const api = vi.mocked(projectTasksApi, true);
const plan: TaskPlan = {
  source: "harness_todos",
  revision: 2,
  steps: [{ content: "最新计划", status: "in_progress" }],
};
const fixture: TaskDetail = {
  task_id: "task_a",
  project_id: null,
  title: "接入计划",
  archived: false,
  status: "running",
  active_turn_id: "turn_a",
  list_revision: 2,
  approval_policy: {
    mode: "ask_for_approval",
    revision: 1,
    updated_at: "2026-10-17T00:00:00Z",
  },
  web_search_enabled: false,
  created_at: "2026-10-17T00:00:00Z",
  updated_at: "2026-10-17T00:00:00Z",
  next_page_cursor: null,
  event_cursor: "cursor_start",
  turns: [
    {
      sequence: 1,
      turn_id: "turn_a",
      request_id: "request_a",
      prompt: "接入计划",
      attachments: [],
      reply: "",
      reasoning: "",
      content_blocks: [],
      content_revision: 2,
      plan,
      plan_revision: 2,
      status: "running",
      error_code: "",
      activity: "",
      artifacts: [],
      model_ref: "provider/model",
      options: {},
      web_search_enabled: false,
      project_revision: null,
      approval_policy_at_start: {
        mode: "ask_for_approval",
        revision: 1,
        updated_at: "2026-10-17T00:00:00Z",
      },
      retry_of_turn_id: null,
      cancel_requested: false,
      pause_requested: false,
      interactions: [],
      interaction_revision: 0,
      created_at: "2026-10-17T00:00:00Z",
      updated_at: "2026-10-17T00:00:00Z",
    },
  ],
};
beforeEach(() => {
  vi.clearAllMocks();
  api.open.mockResolvedValue(fixture);
  api.events.mockImplementation(
    (_id, _cursor, _receive, signal) =>
      new Promise<void>((resolve) => {
        signal.addEventListener("abort", () => resolve(), { once: true });
      }),
  );
});

describe("计划版本合并", () => {
  it.each(["turn", "snapshot", "write"] as const)(
    "%s 为已完成的旧缓存补齐展示快照，迟到无快照记录不清除它",
    async (kind) => {
      const cached: TaskDetail = {
        ...fixture,
        status: "completed",
        active_turn_id: null,
        turns: [{ ...fixture.turns[0], status: "completed" }],
      };
      api.open.mockResolvedValue(cached);
      const hook = renderHook(() => useTaskConversation("task_a", null));
      await waitFor(() => expect(api.events).toHaveBeenCalledTimes(1));
      const receive = api.events.mock.calls[0][2];
      const presentation = {
        action_timeline_enabled: false,
        step_panel_enabled: false,
      };
      for (const detail of [
        {
          ...cached,
          turns: [{ ...cached.turns[0], presentation }],
        },
        cached,
      ]) {
        await act(async () => {
          if (kind === "write") hook.result.current.applyWrite(detail);
          else if (kind === "snapshot")
            receive({ type: "snapshot", data: detail });
          else
            receive({
              type: "turn",
              data: { task: detail, turn: detail.turns[0], cursor: "next" },
            });
        });
        expect(hook.result.current.detail?.turns[0].presentation).toEqual(
          presentation,
        );
      }
    },
  );

  it.each(["turn", "snapshot", "write"] as const)(
    "%s 的计划独立于正文版本，迟到计划不会覆盖最新或已清空的计划",
    async (kind) => {
      const hook = renderHook(() => useTaskConversation("task_a", null));
      await waitFor(() => expect(api.events).toHaveBeenCalledTimes(1));
      const receive = api.events.mock.calls[0][2];
      const update = async (detail: TaskDetail) => {
        await act(async () => {
          if (kind === "write") hook.result.current.applyWrite(detail);
          else if (kind === "snapshot")
            receive({ type: "snapshot", data: detail });
          else
            receive({
              type: "turn",
              data: { task: detail, turn: detail.turns[0], cursor: "next" },
            });
        });
      };
      // A newer body may arrive with an older plan snapshot.
      await update({
        ...fixture,
        list_revision: 3,
        turns: [
          {
            ...fixture.turns[0],
            content_revision: 5,
            plan_revision: 1,
            plan: {
              ...plan,
              revision: 1,
              steps: [{ content: "旧计划", status: "pending" }],
            },
          },
        ],
      });
      expect(hook.result.current.detail?.turns[0]).toMatchObject({
        content_revision: 5,
        plan_revision: 2,
        plan,
      });
      // Clearing the plan is a new revision, even when its body snapshot is older.
      const cleared = { ...plan, revision: 3, steps: [] };
      await update({
        ...fixture,
        list_revision: 4,
        turns: [
          {
            ...fixture.turns[0],
            content_revision: 3,
            plan_revision: 3,
            plan: cleared,
          },
        ],
      });
      expect(hook.result.current.detail?.turns[0]).toMatchObject({
        content_revision: 5,
        plan_revision: 3,
        plan: cleared,
      });
      await update({ ...fixture, list_revision: 5 });
      expect(hook.result.current.detail?.turns[0]).toMatchObject({
        content_revision: 5,
        plan_revision: 3,
        plan: cleared,
      });
    },
  );
});
