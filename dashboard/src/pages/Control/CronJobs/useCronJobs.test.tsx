import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import type { OctopCronRow } from "../../../api/types";
import { useCronJobs } from "./useCronJobs";
import { buildDefaultFormValues } from "./components/constants";

const mocks = vi.hoisted(() => ({ request: vi.fn(), t: (key: string) => key }));
vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: mocks.t }) }));
vi.mock("../../../api/request", () => ({ request: mocks.request }));
vi.mock("@/utils/antdMessage", () => ({
  message: { success: vi.fn(), error: vi.fn() },
}));

const legacy: OctopCronRow = {
  id: "schedule-legacy",
  name: "Legacy schedule",
  agent_id: "expert-1",
  selected_expert_id: "expert-1",
  project_id: null,
  conversation_policy: "task",
  target_task_id: "task-existing",
  trigger: "cron:0 9 * * *",
  prompt: "Summarize progress",
  session_key: "dashboard:owner",
  fresh_thread: false,
  enabled: false,
  task_type: "agent",
  model: null,
  last_run_at: null,
  last_status: null,
  last_error: null,
};

beforeEach(() => {
  mocks.request.mockReset();
  mocks.request.mockImplementation(
    async (path: string, options?: RequestInit) => {
      if (path === "/settings/timezone") return { timezone: "Asia/Shanghai" };
      if (!options?.method) return [legacy];
      const body = JSON.parse(String(options.body));
      return {
        ...legacy,
        ...body,
        session_key: body.session_key ?? legacy.session_key,
      };
    },
  );
});

it("loads all owned schedules without an active expert", async () => {
  const { result } = renderHook(() => useCronJobs());
  await waitFor(() => expect(result.current.loading).toBe(false));
  expect(result.current.jobs[0].id).toBe("schedule-legacy");
  expect(result.current.jobs[0].project_id).toBeNull();
  expect(result.current.jobs[0].skip_confirmation).toBe(false);
  expect(
    result.current.jobToFormValues(result.current.jobs[0], "Asia/Shanghai")
      .skip_confirmation,
  ).toBe(false);
  expect(mocks.request).toHaveBeenCalledWith("/cron", { cache: "no-store" });
  expect(mocks.request).toHaveBeenCalledWith("/settings/timezone");
  expect(
    mocks.request.mock.calls.every(([path]) => !path.includes("/agents/")),
  ).toBe(true);
});

it("creates a project schedule with the general assistant and per-job context", async () => {
  const { result } = renderHook(() => useCronJobs());
  await waitFor(() => expect(result.current.loading).toBe(false));
  await act(async () => {
    const saved = await result.current.createJob({
      ...buildDefaultFormValues("Asia/Shanghai"),
      name: "Daily summary",
      prompt: "Summarize progress",
      project_id: "project-1",
    });
    expect(saved).toBe(true);
  });
  const call = mocks.request.mock.calls.find(
    ([, options]) => options?.method === "POST",
  );
  expect(call?.[0]).toBe("/cron");
  expect(JSON.parse(String(call?.[1].body))).toMatchObject({
    project_id: "project-1",
    selected_expert_id: null,
    conversation_policy: "per_job",
    target_task_id: null,
    skip_confirmation: false,
  });
});

it("preserves an existing target conversation when editing project binding", async () => {
  const { result } = renderHook(() => useCronJobs());
  await waitFor(() => expect(result.current.loading).toBe(false));
  const values = result.current.jobToFormValues(
    result.current.jobs[0],
    "Asia/Shanghai",
  );
  await act(async () => {
    const saved = await result.current.updateJob("schedule-legacy", {
      ...values,
      project_id: "project-1",
    });
    expect(saved).toBe(true);
  });
  const call = mocks.request.mock.calls.find(
    ([, options]) => options?.method === "PATCH",
  );
  expect(call?.[0]).toBe("/cron/schedule-legacy");
  expect(JSON.parse(String(call?.[1].body))).toMatchObject({
    project_id: "project-1",
    selected_expert_id: "expert-1",
    conversation_policy: "task",
    target_task_id: "task-existing",
    session_key: "dashboard:owner",
    skip_confirmation: false,
  });
});

it("creates a schedule with skip confirmation and preserves it when editing", async () => {
  const { result } = renderHook(() => useCronJobs());
  await waitFor(() => expect(result.current.loading).toBe(false));
  await act(async () => {
    expect(
      await result.current.createJob({
        ...buildDefaultFormValues("Asia/Shanghai"),
        name: "Publish summary",
        prompt: "Publish the daily summary",
        skip_confirmation: true,
      }),
    ).toBe(true);
  });
  const created = mocks.request.mock.calls.find(
    ([, options]) => options?.method === "POST",
  );
  expect(JSON.parse(String(created?.[1].body))).toMatchObject({
    project_id: null,
    selected_expert_id: null,
    skip_confirmation: true,
  });
  expect(result.current.jobs[0].skip_confirmation).toBe(true);
  const values = result.current.jobToFormValues(
    result.current.jobs[0],
    "Asia/Shanghai",
  );
  expect(values.skip_confirmation).toBe(true);
  await act(async () => {
    expect(
      await result.current.updateJob("schedule-legacy", {
        ...values,
        name: "Renamed summary",
      }),
    ).toBe(true);
  });
  const patched = mocks.request.mock.calls.find(
    ([, options]) => options?.method === "PATCH",
  );
  expect(JSON.parse(String(patched?.[1].body))).toMatchObject({
    name: "Renamed summary",
    skip_confirmation: true,
  });
  await act(async () => {
    expect(
      await result.current.updateJob("schedule-legacy", {
        ...values,
        skip_confirmation: false,
      }),
    ).toBe(true);
  });
  const lastPatched = mocks.request.mock.calls.at(-1);
  expect(JSON.parse(String(lastPatched?.[1].body)).skip_confirmation).toBe(
    false,
  );
  expect(result.current.jobs[0].skip_confirmation).toBe(false);
});
