import {
  render,
  screen,
  fireEvent,
  waitFor,
  cleanup,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import ResidentActivityPanel from "./ResidentActivityPanel";
import {
  residentActivityApi as api,
  followupReplacement,
  type ResidentFollowup,
} from "../../api/modules/residentActivity";

const { translate } = vi.hoisted(() => ({ translate: (key: string) => key }));
vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: translate }),
}));
vi.mock("../../hooks/useCurrentUser", () => ({
  useCurrentUser: () => ({ id: 7 }),
}));
vi.mock("../../hooks/useServerTimezone", () => ({
  useServerTimezone: () => "Asia/Shanghai",
}));
vi.mock("../../api/modules/residentActivity", async (original) => ({
  ...(await original<typeof import("../../api/modules/residentActivity")>()),
  residentActivityApi: {
    delegations: vi.fn(),
    followups: vi.fn(),
    notifications: vi.fn(),
    context: vi.fn(),
    createFollowup: vi.fn(),
    updateFollowup: vi.fn(),
    cancelFollowup: vi.fn(),
    compact: vi.fn(),
  },
}));
const scope = { agent_id: "private-agent", thread_id: "fixed-thread" };
const mocked = vi.mocked(api, true);
const page = { items: [], next_page_cursor: null };
beforeEach(() => {
  vi.clearAllMocks();
  const values = new Map<string, string>();
  vi.stubGlobal("localStorage", {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => values.set(key, value),
    removeItem: (key: string) => values.delete(key),
    clear: () => values.clear(),
  });
  mocked.delegations.mockResolvedValue(page);
  mocked.followups.mockResolvedValue(page);
  mocked.notifications.mockResolvedValue(page);
  mocked.context.mockResolvedValue({
    max_tokens: 1000,
    used_tokens: 50,
    input_tokens: 40,
    output_tokens: 10,
    source: "model_request",
    segments: [],
  });
});
afterEach(cleanup);
function open(active = false) {
  return render(
    <MemoryRouter>
      <ResidentActivityPanel scope={scope} active={active} />
    </MemoryRouter>,
  );
}

it("opening activity only reads the selected resource; compaction requires an idle conversation and an explicit click", async () => {
  open(true);
  await waitFor(() =>
    expect(mocked.delegations).toHaveBeenCalledWith(
      scope,
      undefined,
      expect.any(AbortSignal),
    ),
  );
  expect(mocked.createFollowup).not.toHaveBeenCalled();
  expect(mocked.compact).not.toHaveBeenCalled();
  await userEvent.click(
    screen.getByRole("tab", { name: "resident.activity.context" }),
  );
  const compact = await screen.findByRole("button", {
    name: "resident.activity.compact",
  });
  await waitFor(() => expect(compact).toBeDisabled());
  expect(mocked.compact).not.toHaveBeenCalled();
});

it("a lost follow-up response survives reload and retries its exact original authorization", async () => {
  mocked.createFollowup.mockRejectedValueOnce(
    new Error("network response lost"),
  );
  const first = open();
  await userEvent.click(
    screen.getByRole("tab", { name: "resident.activity.followups" }),
  );
  await screen.findByRole("textbox", { name: "resident.activity.purpose" });
  fireEvent.change(
    screen.getByRole("textbox", { name: "resident.activity.purpose" }),
    { target: { value: "UI acceptance" } },
  );
  fireEvent.change(
    screen.getByRole("textbox", { name: "resident.activity.schedule" }),
    { target: { value: "interval:3600" } },
  );
  fireEvent.change(
    screen.getByRole("textbox", { name: "resident.activity.reminderText" }),
    { target: { value: "test reminder" } },
  );
  await userEvent.click(
    screen.getByRole("button", { name: "resident.activity.createReminder" }),
  );
  await screen.findByRole("button", {
    name: "resident.activity.retryAuthorization",
  });
  const original = mocked.createFollowup.mock.calls[0][1];
  expect(
    JSON.parse(
      localStorage.getItem(
        "octop:resident-followup:v1:7:private-agent:fixed-thread",
      )!,
    ),
  ).toEqual(original);
  first.unmount();
  mocked.createFollowup.mockResolvedValue({
    ...original,
    followup_id: "followup",
    revision: 1,
  } as ResidentFollowup);
  open();
  await userEvent.click(
    screen.getByRole("tab", { name: "resident.activity.followups" }),
  );
  await userEvent.click(
    await screen.findByRole("button", {
      name: "resident.activity.retryAuthorization",
    }),
  );
  await waitFor(() => expect(mocked.createFollowup).toHaveBeenCalledTimes(2));
  expect(mocked.createFollowup.mock.calls[1]).toEqual([scope, original]);
  await waitFor(() =>
    expect(
      localStorage.getItem(
        "octop:resident-followup:v1:7:private-agent:fixed-thread",
      ),
    ).toBeNull(),
  );
});

it("pause replacements preserve the full authorization and current revision without leaking response fields", () => {
  const row = {
    purpose: "watch",
    kind: "task_watch",
    schedule: null,
    subject_task_id: "task1",
    payload: { text: "" },
    capabilities: ["conversation_notify"],
    notification_target: "conversation",
    revision: 6,
    status: "enabled",
    followup_id: "f1",
    request_id: "initial",
    timezone: "Asia/Shanghai",
  } as ResidentFollowup;
  const replacement = followupReplacement(row, "paused");
  expect(replacement).toEqual({
    purpose: "watch",
    kind: "task_watch",
    schedule: null,
    subject_task_id: "task1",
    payload: { text: "" },
    capabilities: ["conversation_notify"],
    notification_target: "conversation",
    expected_revision: 6,
    status: "paused",
  });
});
