import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Modal } from "antd";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import zh from "../../locales/zh.json";
import {
  residentApi,
  type ResidentMemoryCandidate,
  type ResidentMemoryItem,
  type ResidentMemoryPolicy,
} from "../../api/modules/resident";
import ResidentMemoryPanel from "./ResidentMemoryPanel";

vi.mock("../../api/modules/resident", () => ({
  residentApi: {
    memoryPolicy: vi.fn(),
    updateMemoryPolicy: vi.fn(),
    memory: vi.fn(),
    saveMemory: vi.fn(),
    replaceMemory: vi.fn(),
    deprecateMemory: vi.fn(),
    candidates: vi.fn(),
    extractCandidates: vi.fn(),
    approveCandidate: vi.fn(),
    rejectCandidate: vi.fn(),
    memorySources: vi.fn(),
    candidateSources: vi.fn(),
  },
}));
vi.mock("../../hooks/useServerTimezone", () => ({
  useServerTimezone: () => "Asia/Shanghai",
}));
vi.mock("react-i18next", () => {
  const t = (key: string, options?: Record<string, unknown>) => {
    const value = key
      .split(".")
      .reduce<unknown>(
        (item, part) => (item as Record<string, unknown>)?.[part],
        zh,
      );
    return String(value ?? options?.defaultValue ?? key).replace(
      /\{\{(\w+)\}\}/g,
      (match, name: string) =>
        options && name in options ? String(options[name]) : match,
    );
  };
  return { useTranslation: () => ({ t }) };
});
const api = vi.mocked(residentApi, true);
const scope = { agent_id: "private_a", thread_id: "thread_a" };
const timestamp = "2026-10-24T01:00:00Z";
const atom: ResidentMemoryItem = {
  atom_id: "atom_a",
  assertion: "我偏好下午沟通",
  kind: "Preference",
  created_at: timestamp,
  deprecated_at: null,
  superseded_by: null,
  source_event_ids: ["event_a"],
  quote_event_id: "event_a",
};
const candidate: ResidentMemoryCandidate = {
  candidate_id: "candidate_a",
  assertion: "每周五回顾进展",
  kind: "Preference",
  status: "pending",
  quote: "周五帮我总结一下",
  source_event_ids: ["event_b"],
  quote_event_id: "event_b",
  created_at: timestamp,
  decided_at: null,
  decided_by: null,
};
let policy: ResidentMemoryPolicy;
let memories: ResidentMemoryItem[];
let candidates: ResidentMemoryCandidate[];
beforeEach(() => {
  vi.clearAllMocks();
  policy = { capture_enabled: false, revision: 1, mode: "disabled" };
  memories = [atom];
  candidates = [candidate];
  api.memoryPolicy.mockImplementation(async () => policy);
  api.memory.mockImplementation(async () => ({
    items: memories,
    has_more: false,
  }));
  api.candidates.mockImplementation(async () => ({
    items: candidates,
    has_more: false,
  }));
  api.updateMemoryPolicy.mockImplementation(async (_scope, body) => {
    policy = {
      capture_enabled: body.capture_enabled,
      revision: body.expected_revision + 1,
      mode: body.capture_enabled ? "raw_capture_only" : "disabled",
    };
    return policy;
  });
  api.saveMemory.mockImplementation(async (_scope, body) => {
    const saved = {
      ...atom,
      atom_id: "manual_b",
      assertion: body.assertion,
      kind: body.kind ?? "Fact",
    };
    memories = [...memories, saved];
    return saved;
  });
  api.replaceMemory.mockImplementation(async (_scope, _id, assertion) => {
    const corrected = { ...atom, atom_id: "corrected_b", assertion };
    memories = [corrected];
    return corrected;
  });
  api.deprecateMemory.mockImplementation(async () => {
    const deprecated = { ...memories[0], deprecated_at: timestamp };
    memories = [];
    return deprecated;
  });
  api.extractCandidates.mockResolvedValue({
    status: "completed",
    candidates: 1,
    events_extracted: 2,
    pending_events: 0,
    has_earlier: false,
    truncated_events: 0,
    scope: "recent_user_messages",
    error_code: null,
  });
  api.approveCandidate.mockImplementation(async () => {
    const approved = {
      ...candidate,
      status: "promoted" as const,
      decided_by: "user",
      decided_at: timestamp,
    };
    candidates = [approved];
    return approved;
  });
  api.rejectCandidate.mockImplementation(async () => {
    const rejected = {
      ...candidate,
      status: "rejected" as const,
      decided_by: "user",
      decided_at: timestamp,
    };
    candidates = [rejected];
    return rejected;
  });
  api.memorySources.mockResolvedValue({
    items: [
      {
        event_id: "event_a",
        event_type: "user_message",
        content: "下午联系我就好",
        timestamp,
        source_turn_id: "turn_a",
        source_message_id: "resident:turn_a:user",
        history_item_id: "turn:turn_a",
      },
    ],
  });
  api.candidateSources.mockResolvedValue({ items: [] });
});
afterEach(() => act(() => Modal.destroyAll()));
function mount(
  extra: Partial<React.ComponentProps<typeof ResidentMemoryPanel>> = {},
) {
  return render(
    <ResidentMemoryPanel
      open
      onClose={vi.fn()}
      agentID={scope.agent_id}
      threadID={scope.thread_id}
      {...extra}
    />,
  );
}
async function dialog(title: string) {
  const heading = await screen.findByText(title, {
    selector: ".ant-modal-title",
  });
  return heading.closest<HTMLElement>('[role="dialog"]')!;
}
async function ready() {
  await screen.findByText(atom.assertion);
}

it("opens with read-only requests and never enables capture, extracts, or saves automatically", async () => {
  mount();
  await ready();
  expect(api.memoryPolicy).toHaveBeenCalledWith(scope);
  expect(api.memory).toHaveBeenCalledWith(scope, {
    limit: 50,
    include_deprecated: false,
  });
  expect(
    screen.getByRole("switch", { name: zh.resident.memory.capture }),
  ).not.toBeChecked();
  expect(api.updateMemoryPolicy).not.toHaveBeenCalled();
  expect(api.extractCandidates).not.toHaveBeenCalled();
  expect(api.saveMemory).not.toHaveBeenCalled();
  expect(api.approveCandidate).not.toHaveBeenCalled();
  expect(api.memorySources).not.toHaveBeenCalled();
});

it("requires explicit capture confirmation and keeps the confirmed revision over a stale read", async () => {
  const user = userEvent.setup();
  mount();
  await ready();
  await user.click(
    screen.getByRole("switch", { name: zh.resident.memory.capture }),
  );
  let confirm = await dialog(zh.resident.memory.enableCapture);
  expect(api.updateMemoryPolicy).not.toHaveBeenCalled();
  await user.click(within(confirm).getByRole("button", { name: /取\s*消/ }));
  expect(
    screen.getByRole("switch", { name: zh.resident.memory.capture }),
  ).not.toBeChecked();
  await user.click(
    screen.getByRole("switch", { name: zh.resident.memory.capture }),
  );
  confirm = await dialog(zh.resident.memory.enableCapture);
  const stale = { ...policy };
  api.memoryPolicy.mockResolvedValue(stale);
  await user.click(
    within(confirm).getByRole("button", {
      name: zh.resident.memory.enableCapture,
    }),
  );
  await waitFor(() =>
    expect(
      screen.getByRole("switch", { name: zh.resident.memory.capture }),
    ).toBeChecked(),
  );
  expect(api.updateMemoryPolicy).toHaveBeenCalledWith(scope, {
    capture_enabled: true,
    expected_revision: 1,
  });
  await waitFor(() => expect(api.memoryPolicy).toHaveBeenCalledTimes(2));
  await user.click(
    screen.getByRole("switch", { name: zh.resident.memory.capture }),
  );
  await waitFor(() =>
    expect(api.updateMemoryPolicy).toHaveBeenLastCalledWith(scope, {
      capture_enabled: false,
      expected_revision: 2,
    }),
  );
  expect(api.extractCandidates).not.toHaveBeenCalled();
});

it("manually adds, corrects, and deprecates facts using the private scope and preserves reasons", async () => {
  const user = userEvent.setup();
  mount();
  await ready();
  await user.click(
    screen.getByRole("button", { name: zh.resident.memory.add }),
  );
  let edit = await dialog(zh.resident.memory.add);
  await user.type(
    within(edit).getByRole("textbox", { name: zh.resident.memory.assertion }),
    "周末不安排会议",
  );
  await user.click(
    within(edit).getByRole("button", { name: zh.resident.memory.save }),
  );
  await screen.findByText("周末不安排会议", { selector: "article p" });
  expect(api.saveMemory).toHaveBeenCalledWith(scope, {
    assertion: "周末不安排会议",
    kind: "Fact",
  });
  const article = screen.getByText(atom.assertion).closest("article")!;
  await user.click(
    within(article).getByRole("button", { name: zh.resident.memory.replace }),
  );
  edit = await dialog(zh.resident.memory.replace);
  const input = within(edit).getByRole("textbox", {
    name: zh.resident.memory.assertion,
  });
  await user.clear(input);
  await user.type(input, "我偏好上午沟通");
  await user.click(
    within(edit).getByRole("button", { name: zh.resident.memory.save }),
  );
  await screen.findByText("我偏好上午沟通", { selector: "article p" });
  expect(api.replaceMemory).toHaveBeenCalledWith(
    scope,
    "atom_a",
    "我偏好上午沟通",
  );
  await user.click(
    screen.getByRole("button", { name: zh.resident.memory.deprecate }),
  );
  const remove = await dialog(zh.resident.memory.deprecate);
  await user.type(
    within(remove).getByRole("textbox", { name: zh.resident.memory.reason }),
    "偏好已改变",
  );
  await user.click(within(remove).getByRole("button", { name: /确\s*认/ }));
  await waitFor(() =>
    expect(api.deprecateMemory).toHaveBeenCalledWith(
      scope,
      "corrected_b",
      "偏好已改变",
    ),
  );
});

it("extracts only after model confirmation and requires a separate approval to save a candidate", async () => {
  const user = userEvent.setup();
  mount();
  await ready();
  await user.click(
    screen.getByRole("tab", { name: zh.resident.memory.candidates }),
  );
  await user.click(
    screen.getByRole("button", { name: zh.resident.memory.extract }),
  );
  let confirm = await dialog(zh.resident.memory.extractConfirm);
  expect(api.extractCandidates).not.toHaveBeenCalled();
  await user.click(
    within(confirm).getByRole("button", { name: zh.resident.memory.extract }),
  );
  await waitFor(() =>
    expect(api.extractCandidates).toHaveBeenCalledWith(scope),
  );
  expect(api.approveCandidate).not.toHaveBeenCalled();
  expect(api.saveMemory).not.toHaveBeenCalled();
  await user.click(
    screen.getByRole("button", { name: zh.resident.memory.approve }),
  );
  confirm = await dialog(zh.resident.memory.approveConfirm);
  expect(api.approveCandidate).not.toHaveBeenCalled();
  await user.click(
    within(confirm).getByRole("button", { name: zh.resident.memory.approve }),
  );
  await waitFor(() =>
    expect(api.approveCandidate).toHaveBeenCalledWith(scope, "candidate_a"),
  );
});

it("shows actual source evidence and explicitly rejects candidates with an audit reason", async () => {
  const user = userEvent.setup();
  const onClose = vi.fn();
  const onLocateHistory = vi.fn();
  mount({ onClose, onLocateHistory });
  await ready();
  await user.click(
    screen.getByRole("button", { name: zh.resident.memory.sources }),
  );
  let modal = await dialog(zh.resident.memory.sources);
  await within(modal).findByText("下午联系我就好");
  expect(api.memorySources).toHaveBeenCalledWith(scope, "atom_a");
  expect(within(modal).getByText(/turn:turn_a/)).toBeTruthy();
  await user.click(
    within(modal).getByRole("button", {
      name: zh.resident.memory.openHistory,
    }),
  );
  expect(onClose).toHaveBeenCalledOnce();
  expect(onLocateHistory).toHaveBeenCalledWith("turn:turn_a");
  await user.click(
    screen.getByRole("tab", { name: zh.resident.memory.candidates }),
  );
  await user.click(
    screen.getByRole("button", { name: zh.resident.memory.reject }),
  );
  modal = await dialog(zh.resident.memory.reject);
  await user.type(
    within(modal).getByRole("textbox", { name: zh.resident.memory.reason }),
    "不代表长期偏好",
  );
  await user.click(within(modal).getByRole("button", { name: /确\s*认/ }));
  await waitFor(() =>
    expect(api.rejectCandidate).toHaveBeenCalledWith(
      scope,
      "candidate_a",
      "不代表长期偏好",
    ),
  );
});

it("does not expose late reads or execute an old confirmation after switching private scope", async () => {
  let oldRead!: (value: Awaited<ReturnType<typeof residentApi.memory>>) => void;
  api.memory.mockImplementationOnce(
    () =>
      new Promise((resolve) => {
        oldRead = resolve;
      }),
  );
  const user = userEvent.setup();
  const view = mount();
  await waitFor(() => expect(api.memoryPolicy).toHaveBeenCalled());
  api.memory.mockResolvedValue({
    items: [{ ...atom, atom_id: "other", assertion: "另一个私人实例" }],
    has_more: false,
  });
  view.rerender(
    <ResidentMemoryPanel
      open
      onClose={vi.fn()}
      agentID="private_b"
      threadID="thread_b"
    />,
  );
  await screen.findByText("另一个私人实例");
  await act(async () => oldRead({ items: [atom], has_more: false }));
  expect(screen.queryByText(atom.assertion)).toBeNull();
  await user.click(
    screen.getByRole("switch", { name: zh.resident.memory.capture }),
  );
  const confirm = await dialog(zh.resident.memory.enableCapture);
  view.rerender(
    <ResidentMemoryPanel
      open
      onClose={vi.fn()}
      agentID="private_c"
      threadID="thread_c"
    />,
  );
  await user.click(
    within(confirm).getByRole("button", {
      name: zh.resident.memory.enableCapture,
    }),
  );
  expect(api.updateMemoryPolicy).not.toHaveBeenCalled();
});

it("preserves archived-instance cleanup permissions while forbidding new collection and promotion", async () => {
  policy = { capture_enabled: true, revision: 4, mode: "raw_capture_only" };
  const user = userEvent.setup();
  mount({ archived: true });
  await ready();
  expect(
    screen.getByRole("button", { name: zh.resident.memory.add }),
  ).toBeDisabled();
  expect(
    screen.getByRole("button", { name: zh.resident.memory.replace }),
  ).toBeEnabled();
  expect(
    screen.getByRole("button", { name: zh.resident.memory.deprecate }),
  ).toBeEnabled();
  fireEvent.click(
    screen.getByRole("switch", { name: zh.resident.memory.capture }),
  );
  await waitFor(() =>
    expect(api.updateMemoryPolicy).toHaveBeenCalledWith(scope, {
      capture_enabled: false,
      expected_revision: 4,
    }),
  );
  await user.click(
    screen.getByRole("tab", { name: zh.resident.memory.candidates }),
  );
  expect(
    screen.getByRole("button", { name: zh.resident.memory.extract }),
  ).toBeDisabled();
  expect(
    screen.getByRole("button", { name: zh.resident.memory.approve }),
  ).toBeDisabled();
  expect(
    screen.getByRole("button", { name: zh.resident.memory.reject }),
  ).toBeEnabled();
});
