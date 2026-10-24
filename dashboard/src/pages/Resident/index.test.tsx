import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type {
  ResidentHistoryItem,
  ResidentHistoryPage,
  ResidentTurn,
} from "../../api/modules/resident";
import { useResidentConversation } from "./useResidentConversation";
import ResidentPage from "./index";

const account = vi.hoisted(() => ({ id: 7 }));
vi.mock("../../hooks/useCurrentUser", () => ({
  useCurrentUser: () => ({ id: account.id }),
}));
vi.mock("../../hooks/useServerTimezone", () => ({
  useServerTimezone: () => "Asia/Shanghai",
}));
vi.mock("./useResidentConversation", () => ({
  useResidentConversation: vi.fn(),
}));
vi.mock("./ResidentMemoryPanel", () => ({
  default: ({
    open,
    onClose,
    agentID,
    threadID,
    onLocateHistory,
  }: {
    open: boolean;
    onClose: () => void;
    agentID: string;
    threadID: string;
    onLocateHistory?: (itemID: string) => void;
  }) =>
    open ? (
      <div role="dialog" aria-label="Memory">
        {agentID}:{threadID}
        <button onClick={onClose}>Close memory</button>
        <button onClick={() => onLocateHistory?.("memory-source")}>
          Locate memory source
        </button>
      </div>
    ) : null,
}));
vi.mock("./ResidentActivityPanel", () => ({
  default: ({ scope }: { scope: { agent_id: string; thread_id: string } }) => (
    <div>
      Activity {scope.agent_id}:{scope.thread_id}
    </div>
  ),
}));
vi.mock("../../components/Markdown/LazyMarkdown", () => ({
  default: (props: { content: string; onRunShellCommand?: unknown }) => (
    <div
      data-testid="markdown"
      data-executable={String(!!props.onRunShellCommand)}
    >
      {props.content}
    </div>
  ),
}));
vi.mock("react-i18next", () => {
  const t = (key: string) => key;
  return { useTranslation: () => ({ t }) };
});

const prefix = "resident.page";
const turn: ResidentTurn = {
  turn_id: "resident-turn",
  revision: 1,
  request_id: "send-1",
  user_message_id: "resident-user",
  assistant_message_id: "resident-assistant",
  prompt: "Private question",
  reply: "**Private reply**",
  status: "completed",
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:01Z",
  error_code: null,
  error_message: null,
  authorization: {},
};
const item: ResidentHistoryItem = {
  item_id: "history-turn",
  kind: "turn",
  day_key: "2026-01-01",
  timezone: "UTC",
  created_at: turn.created_at,
  updated_at: turn.updated_at,
  turn,
  report: null,
  notification: null,
};
const report: ResidentHistoryItem = {
  ...item,
  item_id: "history-report",
  kind: "task_report",
  turn: null,
  created_at: "2026-01-01T00:01:00Z",
  report: {
    report_id: "report-1",
    message_id: "report-message",
    task_id: "delegated-task",
    task_turn_id: "task-turn",
    status: "completed",
    text: "Delegated work completed",
    error_code: null,
    error_message: null,
    artifacts: [],
    delivery_status: "delivered",
  },
};
const notification: ResidentHistoryItem = {
  ...item,
  item_id: "history-note",
  kind: "notification",
  turn: null,
  created_at: "2026-01-01T00:02:00Z",
  notification: {
    notification_id: "note-1",
    message_id: "note-message",
    followup_id: "followup",
    kind: "note",
    text: "Remember your plan",
    payload: {},
    status: "delivered",
  },
};
const page = (
  items: ResidentHistoryItem[],
  next_cursor: string | null = null,
): ResidentHistoryPage => ({
  agent_id: "private-agent",
  thread_id: "private-thread",
  history_revision: 1,
  items,
  next_cursor,
});
let state: ReturnType<typeof useResidentConversation>;
const observers: TestObserver[] = [];
const getComputedStyle = window.getComputedStyle;
class TestObserver {
  targets: Element[] = [];
  root: Element | Document | null;
  rootMargin = "0px";
  thresholds = [0];
  constructor(
    private callback: IntersectionObserverCallback,
    options?: IntersectionObserverInit,
  ) {
    this.root = options?.root ?? null;
    observers.push(this);
  }
  observe(target: Element) {
    this.targets.push(target);
  }
  unobserve() {}
  disconnect() {}
  takeRecords() {
    return [];
  }
  emit(target: Element, ratio = 0.8) {
    this.callback(
      [
        {
          target,
          isIntersecting: ratio > 0,
          intersectionRatio: ratio,
        } as IntersectionObserverEntry,
      ],
      this as unknown as IntersectionObserver,
    );
  }
}

beforeEach(() => {
  vi.clearAllMocks();
  observers.length = 0;
  account.id = 7;
  vi.stubGlobal("IntersectionObserver", TestObserver);
  vi.spyOn(window, "getComputedStyle").mockImplementation((element) =>
    getComputedStyle(element),
  );
  state = {
    binding: {
      status: "ready",
      agent_id: "private-agent",
      thread_id: "private-thread",
      name: "Private",
      timezone: "UTC",
      error_code: null,
    },
    capabilities: { memory_management: true, turn_read_tools: true },
    turns: [turn],
    timeline: page([item, report, notification]),
    loading: false,
    error: null,
    connectionState: "connected",
    draft: "",
    setDraft: vi.fn(),
    readTools: false,
    setReadTools: vi.fn(),
    send: vi.fn().mockResolvedValue(undefined),
    pendingIntent: null,
    retryPending: vi.fn().mockResolvedValue(undefined),
    cancel: vi.fn().mockResolvedValue(undefined),
    refresh: vi.fn().mockResolvedValue(undefined),
    repair: vi.fn().mockResolvedValue(undefined),
    sending: false,
    activeTurn: null,
    history: null,
    historyLoading: false,
    historyError: null,
    loadHistory: vi.fn().mockResolvedValue(page([item])),
    searchHistory: vi.fn().mockResolvedValue(page([])),
    historyDays: vi.fn().mockResolvedValue(null),
    locateHistory: vi.fn().mockResolvedValue(null),
    markRead: vi.fn().mockResolvedValue(undefined),
  } as unknown as ReturnType<typeof useResidentConversation>;
  vi.mocked(useResidentConversation).mockImplementation(() => state);
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});
const show = () =>
  render(
    <MemoryRouter>
      <ResidentPage />
    </MemoryRouter>,
  );

it("renders fixed private identity, distinct replies/reports/updates and a real Task link", async () => {
  show();
  expect(useResidentConversation).toHaveBeenCalledWith(7);
  const log = screen.getByRole("log");
  expect(
    within(log).getByRole("article", { name: `${prefix}.user` }),
  ).toHaveTextContent("Private question");
  expect(
    within(log).getByRole("article", { name: `${prefix}.assistant` }),
  ).toHaveTextContent("**Private reply**");
  expect(
    within(log).getByRole("article", { name: `${prefix}.taskReport` }),
  ).toHaveTextContent("Delegated work completed");
  expect(
    within(log).getByRole("article", { name: `${prefix}.notification` }),
  ).toHaveTextContent("Remember your plan");
  expect(
    within(log).getByRole("link", { name: `${prefix}.openTask` }),
  ).toHaveAttribute("href", "/conversations/delegated-task");
  expect(screen.queryByRole("combobox")).toBeNull();
  expect(
    screen
      .getAllByTestId("markdown")
      .every((node) => node.dataset.executable === "false"),
  ).toBe(true);
  await waitFor(() =>
    expect(state.loadHistory).toHaveBeenCalledWith({ limit: 40 }),
  );
  expect(state.markRead).not.toHaveBeenCalled();
});

it("sends text only on explicit click or non-IME Enter and leaves drafts in the hook", async () => {
  state.draft = "Draft message";
  show();
  const input = screen.getByRole("textbox", { name: `${prefix}.message` });
  fireEvent.change(input, { target: { value: "Updated draft" } });
  expect(state.setDraft).toHaveBeenCalledWith("Updated draft");
  fireEvent.keyDown(input, { key: "Enter", isComposing: true });
  fireEvent.keyDown(input, { key: "Enter", keyCode: 229 });
  fireEvent.keyDown(input, { key: "Enter", shiftKey: true });
  expect(state.send).not.toHaveBeenCalled();
  fireEvent.keyDown(input, { key: "Enter" });
  await waitFor(() => expect(state.send).toHaveBeenCalledTimes(1));
  fireEvent.click(
    screen.getByRole("checkbox", { name: `${prefix}.readTools` }),
  );
  expect(state.setReadTools).toHaveBeenCalledWith(true);
});

it("uses the original pending receipt and cancels the exact active private turn", async () => {
  state.pendingIntent = {
    request_id: "original",
    prompt: "Frozen prompt",
    read_tools: false,
  };
  state.activeTurn = { ...turn, status: "running" };
  state.draft = "Next draft";
  show();
  expect(state.retryPending).not.toHaveBeenCalled();
  expect(screen.getByRole("button", { name: `${prefix}.send` })).toBeDisabled();
  expect(
    screen.getByRole("checkbox", { name: `${prefix}.readTools` }),
  ).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: `${prefix}.retry` }));
  await waitFor(() => expect(state.retryPending).toHaveBeenCalledTimes(1));
  fireEvent.click(screen.getByRole("button", { name: `${prefix}.cancel` }));
  await waitFor(() =>
    expect(state.cancel).toHaveBeenCalledWith(state.activeTurn),
  );
  expect(state.send).not.toHaveBeenCalled();
});

it("pages and searches independent history, locates an excerpt, and returns to live replies", async () => {
  const old = {
    ...item,
    item_id: "old-item",
    turn: {
      ...turn,
      turn_id: "old-turn",
      prompt: "Old private entry",
      reply: "Old answer",
    },
    created_at: "2025-01-01T00:00:00Z",
  };
  vi.mocked(state.loadHistory).mockImplementation(async (query = {}) =>
    query.cursor ? page([old]) : page([item], "older"),
  );
  vi.mocked(state.searchHistory).mockResolvedValue(page([old]));
  vi.mocked(state.locateHistory).mockResolvedValue({
    ...page([old]),
    anchor_item_id: "old-item",
    has_before: true,
    has_after: true,
  });
  show();
  fireEvent.click(
    await screen.findByRole("button", { name: `${prefix}.loadOlder` }),
  );
  await waitFor(() =>
    expect(state.loadHistory).toHaveBeenCalledWith({
      cursor: "older",
      limit: 40,
    }),
  );
  const search = screen.getByRole("searchbox", {
    name: `${prefix}.historySearch`,
  });
  const user = userEvent.setup();
  await user.type(search, "Old{Enter}");
  await waitFor(() =>
    expect(state.searchHistory).toHaveBeenCalledWith("Old", undefined),
  );
  const history = screen.getByRole("complementary", {
    name: `${prefix}.historyTitle`,
  });
  await user.click(within(history).getByText("Old private entry"));
  await waitFor(() =>
    expect(state.locateHistory).toHaveBeenCalledWith("old-item"),
  );
  expect(screen.getByRole("log")).toHaveTextContent("Old answer");
  fireEvent.click(
    screen.getByRole("button", { name: `${prefix}.returnLatest` }),
  );
  expect(screen.getByRole("log")).toHaveTextContent("**Private reply**");
});

it("opens scoped memory and activity without implicit execution or memory capture", async () => {
  show();
  fireEvent.click(screen.getByRole("button", { name: `${prefix}.memory` }));
  expect(
    await screen.findByRole("dialog", { name: "Memory" }),
  ).toHaveTextContent("private-agent:private-thread");
  fireEvent.click(screen.getByRole("button", { name: "Close memory" }));
  fireEvent.click(screen.getByRole("button", { name: `${prefix}.activity` }));
  expect(
    await screen.findByText("Activity private-agent:private-thread"),
  ).toBeVisible();
  expect(state.send).not.toHaveBeenCalled();
});

it("only marks actual assistant/report/update anchors read when visible and unobscured", async () => {
  show();
  const observer = observers[observers.length - 1];
  expect(
    observer.targets.map(
      (node) => (node as HTMLElement).dataset.residentReadId,
    ),
  ).toEqual(["resident-assistant", "report-message", "note-message"]);
  expect(state.markRead).not.toHaveBeenCalled();
  await act(async () => observer.emit(observer.targets[0], 0));
  expect(state.markRead).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: `${prefix}.memory` }));
  await act(async () => observer.emit(observer.targets[1]));
  expect(state.markRead).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Close memory" }));
  const visibleObserver = observers[observers.length - 1];
  await act(async () => visibleObserver.emit(visibleObserver.targets[1]));
  expect(state.markRead).toHaveBeenCalledWith("report-message");
  expect(state.markRead).not.toHaveBeenCalledWith("resident-user");
});

it("closes memory and opens the real source history excerpt on explicit request", async () => {
  vi.mocked(state.locateHistory).mockResolvedValue({
    ...page([item]),
    anchor_item_id: "history-turn",
    has_before: false,
    has_after: false,
  });
  show();
  fireEvent.click(screen.getByRole("button", { name: `${prefix}.memory` }));
  fireEvent.click(
    await screen.findByRole("button", { name: "Locate memory source" }),
  );
  await waitFor(() =>
    expect(state.locateHistory).toHaveBeenCalledWith("memory-source"),
  );
  expect(screen.queryByRole("dialog", { name: "Memory" })).toBeNull();
  expect(screen.getByText(`${prefix}.historyFocused`)).toBeVisible();
  expect(screen.getByRole("log")).toHaveTextContent("**Private reply**");
  expect(state.send).not.toHaveBeenCalled();
});

it("marks the visible end of a long reply rather than requiring its full article to fit", async () => {
  const longTurn = { ...turn, reply: "Long private reply. ".repeat(1000) };
  state.turns = [longTurn];
  state.timeline = page([{ ...item, turn: longTurn }]);
  show();
  const observer = observers[observers.length - 1];
  const sentinel = observer.targets[0];
  const article = screen.getByRole("article", {
    name: `${prefix}.assistant`,
  });
  expect(sentinel.tagName).toBe("SPAN");
  expect(article).toContainElement(sentinel as HTMLElement);
  expect(article).not.toHaveAttribute("data-resident-read-id");
  expect(observer.root).toBe(screen.getByRole("log"));
  await act(async () => observer.emit(sentinel, 0));
  expect(state.markRead).not.toHaveBeenCalled();
  await act(async () => observer.emit(sentinel, 0.2));
  expect(state.markRead).toHaveBeenCalledWith("resident-assistant");
});

it("allows empty terminal replies to be read but excludes empty active turns", async () => {
  state.turns = [
    { ...turn, reply: "", status: "failed" },
    {
      ...turn,
      turn_id: "active-turn",
      assistant_message_id: "active-assistant",
      reply: "",
      status: "running",
    },
  ];
  state.timeline = page([]);
  show();
  const observer = observers[observers.length - 1];
  expect(
    observer.targets.map(
      (node) => (node as HTMLElement).dataset.residentReadId,
    ),
  ).toEqual(["resident-assistant"]);
  await act(async () => observer.emit(observer.targets[0]));
  expect(state.markRead).toHaveBeenCalledWith("resident-assistant");
  expect(state.markRead).not.toHaveBeenCalledWith("active-assistant");
});

it("does not automatically repair a missing identity or enable sending", async () => {
  state.binding = {
    status: "not_created",
    agent_id: null,
    thread_id: null,
    name: null,
    timezone: "UTC",
    error_code: null,
  };
  state.turns = [];
  state.timeline = null;
  show();
  expect(screen.getByText(`${prefix}.notReady`)).toBeVisible();
  expect(state.repair).not.toHaveBeenCalled();
  expect(state.loadHistory).not.toHaveBeenCalled();
  expect(screen.getByRole("button", { name: `${prefix}.send` })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: `${prefix}.repair` }));
  await waitFor(() => expect(state.repair).toHaveBeenCalledTimes(1));
});

it("isolates local history focus and open private drawers immediately when accounts change", async () => {
  const view = show();
  fireEvent.click(screen.getByRole("button", { name: `${prefix}.memory` }));
  expect(await screen.findByRole("dialog", { name: "Memory" })).toBeVisible();
  account.id = 8;
  state = {
    ...state,
    binding: {
      ...state.binding!,
      agent_id: "other-agent",
      thread_id: "other-thread",
    },
    turns: [],
    timeline: page([]),
    loadHistory: vi.fn().mockResolvedValue({
      ...page([]),
      agent_id: "other-agent",
      thread_id: "other-thread",
    }),
  };
  await act(async () => {
    view.rerender(
      <MemoryRouter>
        <ResidentPage />
      </MemoryRouter>,
    );
  });
  expect(screen.queryByRole("dialog", { name: "Memory" })).toBeNull();
  expect(screen.queryByText("Private question")).toBeNull();
  expect(useResidentConversation).toHaveBeenLastCalledWith(8);
});
