import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";
import zh from "../../locales/zh.json";
import { projectsApi, projectTasksApi } from "../../api/modules/projects";
import LegacyConversationContinuation from "./LegacyConversationContinuation";

const queue = vi.hoisted(() => ({
  drafts: { current: new Map<string, string>() },
}));
vi.mock("./useTaskMessageQueue", () => ({ useTaskMessageQueue: () => queue }));
vi.mock("../Chat/hooks/chatStore", () => ({
  readInputDraft: () => "Original draft",
}));
vi.mock("../../api/modules/projects", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../api/modules/projects")>()),
  projectsApi: { list: vi.fn() },
  projectTasksApi: { resources: vi.fn(), fromThread: vi.fn() },
}));
vi.mock("react-i18next", () => {
  const t = (key: string) =>
    key
      .split(".")
      .reduce<unknown>(
        (value, part) => (value as Record<string, unknown>)?.[part],
        zh,
      ) ?? key;
  return { useTranslation: () => ({ t }) };
});
const api = vi.mocked(projectTasksApi, true);
beforeEach(() => {
  vi.clearAllMocks();
  queue.drafts.current.clear();
  vi.mocked(projectsApi.list).mockResolvedValue([]);
  api.resources.mockResolvedValue({
    experts: [
      { expert_id: "expert_a", name: "A", description: "", config_revision: 1 },
    ],
  } as Awaited<ReturnType<typeof projectTasksApi.resources>>);
  api.fromThread.mockResolvedValue({ task_id: "new_task" } as Awaited<
    ReturnType<typeof projectTasksApi.fromThread>
  >);
});
function Location() {
  const location = useLocation();
  return (
    <output data-testid="location">
      {location.pathname + location.search}
    </output>
  );
}
function mount(threadID: string | null = "thread_a", busy = false) {
  return render(
    <MemoryRouter initialEntries={["/chat/expert_a/thread_a"]}>
      <LegacyConversationContinuation
        agentID="expert_a"
        threadID={threadID}
        busy={busy}
      />
      <Location />
    </MemoryRouter>,
  );
}

it("explicitly continues history without sending a new turn and transfers the text draft", async () => {
  mount();
  await userEvent.click(
    screen.getByRole("button", { name: zh.projects.tasks.legacy.continue }),
  );
  const confirm = await screen.findByRole("button", {
    name: new RegExp(zh.common.confirm.split("").join("\\s*")),
  });
  await waitFor(() => expect(confirm).toBeEnabled());
  await userEvent.click(confirm);
  await waitFor(() =>
    expect(api.fromThread).toHaveBeenCalledWith({
      request_id: expect.any(String),
      agent_id: "expert_a",
      thread_id: "thread_a",
      project_id: null,
      selected_expert_id: "expert_a",
    }),
  );
  await waitFor(() =>
    expect(screen.getByTestId("location")).toHaveTextContent(
      "/conversations/new_task",
    ),
  );
  expect(queue.drafts.current.get("new_task")).toBe("Original draft");
});

it("reuses the continuation request after an unknown receipt", async () => {
  api.fromThread.mockRejectedValueOnce(new Error("Unknown receipt"));
  mount();
  await userEvent.click(
    screen.getByRole("button", { name: zh.projects.tasks.legacy.continue }),
  );
  const confirm = await screen.findByRole("button", {
    name: new RegExp(zh.common.confirm.split("").join("\\s*")),
  });
  await waitFor(() => expect(confirm).toBeEnabled());
  await userEvent.click(confirm);
  await screen.findByText("Unknown receipt");
  await userEvent.click(confirm);
  await waitFor(() => expect(api.fromThread).toHaveBeenCalledTimes(2));
  expect(api.fromThread.mock.calls[1][0]).toEqual(
    api.fromThread.mock.calls[0][0],
  );
});

it("keeps active legacy work in place until it can be continued", () => {
  mount("thread_a", true);
  expect(
    screen.getByRole("button", { name: zh.projects.tasks.legacy.continue }),
  ).toBeDisabled();
  expect(api.fromThread).not.toHaveBeenCalled();
});

it("opens a new unified draft directly when no legacy thread exists", async () => {
  mount(null);
  await userEvent.click(
    screen.getByRole("button", { name: zh.projects.tasks.legacy.start }),
  );
  expect(screen.getByTestId("location")).toHaveTextContent(
    "/conversations?expert_id=expert_a",
  );
  expect(api.fromThread).not.toHaveBeenCalled();
  expect(queue.drafts.current.get("draft:standalone")).toBe("Original draft");
});
