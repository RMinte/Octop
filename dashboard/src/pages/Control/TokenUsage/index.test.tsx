import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { request, requestBlob } from "../../../api/request";
import { useAgent } from "../../../context/AgentContext";
import { useIsMobile } from "../../../hooks/useIsMobile";
import { useUserRole } from "../../../hooks/useUserRole";
import TokenUsagePage from "./index";

vi.mock("../../../api/request", () => ({
  request: vi.fn(),
  requestBlob: vi.fn(),
}));
vi.mock("../../../context/AgentContext", () => ({ useAgent: vi.fn() }));
vi.mock("../../../hooks/useIsMobile", () => ({ useIsMobile: vi.fn() }));
vi.mock("../../../hooks/useUserRole", () => ({ useUserRole: vi.fn() }));
vi.mock("../../../layouts/PageShell", () => ({
  default: ({ children }: { children: ReactNode }) => <div>{children}</div>,
}));
vi.mock("react-i18next", async () => {
  const { default: zh } = await import("../../../locales/zh.json");
  const strings = zh.tokenUsage as Record<string, string>;
  const t = (key: string, options?: { count?: number }) => {
    const text = strings[key.replace(/^tokenUsage\./, "")] ?? key;
    return text.replace("{{count}}", String(options?.count ?? ""));
  };
  return { useTranslation: () => ({ t }) };
});
vi.mock("recharts", async () => ({
  ...(await vi.importActual<typeof import("recharts")>("recharts")),
  ResponsiveContainer: () => null,
}));

const LONG_TASK_TITLE =
  "分析项目中的全部任务并整理一份包含完整上下文的超长标题报告";

function bucket(key: string, label: string, total = 600, turns = 3) {
  return {
    key,
    label,
    input_tokens: total - 100,
    uncached_input_tokens: total - 100,
    cache_read_tokens: 0,
    cache_write_tokens: 0,
    output_tokens: 100,
    reasoning_tokens: 0,
    total_tokens: total,
    model_calls: turns,
    turns,
  };
}

function summary(granularity: string) {
  const buckets =
    granularity === "by_project"
      ? [bucket("project-1", "产品规划"), bucket("", "No project", 200, 1)]
      : granularity === "by_task"
      ? [bucket("task-1", LONG_TASK_TITLE)]
      : granularity === "by_agent"
      ? [bucket("agent-1", "agent-1", 100, 1)]
      : [bucket("2026-10-20", "2026-10-20", 900, 4)];
  const total = buckets.reduce((sum, item) => sum + item.total_tokens, 0);
  return {
    window: "all",
    granularity,
    range_start: 0,
    range_end: 1,
    input_tokens: buckets.reduce((sum, item) => sum + item.input_tokens, 0),
    uncached_input_tokens: 0,
    cache_read_tokens: 0,
    cache_write_tokens: 0,
    output_tokens: buckets.length * 100,
    reasoning_tokens: 0,
    total_tokens: total,
    model_calls: 0,
    cache_hit_percent: 0,
    turns: buckets.reduce((sum, item) => sum + item.turns, 0),
    avg_per_turn: 100,
    buckets,
  };
}

function summaryRequests(granularity: string) {
  return vi
    .mocked(request)
    .mock.calls.map(([path]) => new URL(String(path), "http://localhost"))
    .filter((url) => url.searchParams.get("granularity") === granularity);
}

function emptySourceSummary() {
  return {
    total_tasks: 0,
    items: [],
    unassigned_call_usage: { calls: 0 },
  };
}

async function selectExpert() {
  fireEvent.mouseDown(screen.getByRole("combobox", { name: "专家" }));
  await userEvent.click(screen.getAllByText("写作专家").at(-1)!);
  await waitFor(() =>
    expect(summaryRequests("by_day").at(-1)?.searchParams.get("agent_id")).toBe(
      "agent-1",
    ),
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.stubGlobal(
    "matchMedia",
    vi.fn((query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  );
  vi.mocked(useAgent).mockReturnValue({
    agents: [{ agent_id: "agent-1", name: "写作专家" }],
  } as never);
  vi.mocked(useIsMobile).mockReturnValue(false);
  vi.mocked(useUserRole).mockReturnValue("user");
  vi.mocked(request).mockImplementation(async (path) => {
    if (path === "/users") {
      return [{ id: 7, username: "alice", display_name: "Alice" }] as never;
    }
    const url = new URL(String(path), "http://localhost");
    if (url.pathname.endsWith("/task-summary")) {
      return emptySourceSummary() as never;
    }
    return summary(url.searchParams.get("granularity") ?? "total") as never;
  });
  vi.mocked(requestBlob).mockResolvedValue(new Blob(["xlsx"]));
  vi.stubGlobal(
    "URL",
    Object.assign(URL, {
      createObjectURL: vi.fn(() => "blob:usage"),
      revokeObjectURL: vi.fn(),
    }),
  );
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
  const getComputedStyle = window.getComputedStyle;
  vi.spyOn(window, "getComputedStyle").mockImplementation((element) =>
    getComputedStyle(element),
  );
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("TokenUsagePage project and task dimensions", () => {
  it("includes task usage in summary totals while retaining the expert breakdown", async () => {
    render(<TokenUsagePage />);

    expect(await screen.findByText("900")).toBeInTheDocument();
    expect(screen.getByText("写作专家")).toBeInTheDocument();
    expect(screen.getByText("总 tokens")).toBeInTheDocument();
  });

  it("shows localized project names and task totals without the retained expert filter", async () => {
    render(<TokenUsagePage />);
    await screen.findByText("900");
    await selectExpert();
    await userEvent.click(screen.getByText("按项目"));

    const table = await screen.findByRole("table");
    const projectRow = await within(table).findByText("产品规划");
    expect(
      within(projectRow.closest("tr")!).getByText("500"),
    ).toBeInTheDocument();
    expect(
      within(projectRow.closest("tr")!).getByText("100"),
    ).toBeInTheDocument();
    expect(
      within(projectRow.closest("tr")!).getByText("600"),
    ).toBeInTheDocument();
    expect(
      within(projectRow.closest("tr")!).getByText("3"),
    ).toBeInTheDocument();
    expect(within(table).getByText("未关联项目")).toBeInTheDocument();
    expect(
      screen.getByText(/仅统计任务用量，包含标题生成与自动审核。/),
    ).toBeInTheDocument();
    expect(screen.queryByText("缓存输入")).not.toBeInTheDocument();
    expect(screen.queryByText("缓存命中率")).not.toBeInTheDocument();
    expect(within(table).getByText("project-1")).toBeInTheDocument();
    expect(
      screen.queryByRole("combobox", { name: "专家" }),
    ).not.toBeInTheDocument();
    expect(
      summaryRequests("by_project").at(-1)?.searchParams.has("agent_id"),
    ).toBe(false);

    await userEvent.click(screen.getByRole("button", { name: "导出全部维度" }));
    await waitFor(() => expect(requestBlob).toHaveBeenCalledOnce());
    const exportUrl = new URL(
      String(vi.mocked(requestBlob).mock.calls[0][0]),
      "http://localhost",
    );
    expect(exportUrl.pathname).toBe("/usage/export.xlsx");
    expect(exportUrl.searchParams.has("agent_id")).toBe(false);
    expect(exportUrl.searchParams.get("window")).toBe(
      summaryRequests("by_project").at(-1)?.searchParams.get("window"),
    );

    await userEvent.click(screen.getByText("按任务"));
    await waitFor(() => expect(summaryRequests("by_task")).toHaveLength(1));
    expect(summaryRequests("by_task")[0].searchParams.has("agent_id")).toBe(
      false,
    );
    expect(await screen.findByText("任务来源统计")).toBeInTheDocument();
    const sourceRequest = vi
      .mocked(request)
      .mock.calls.map(([path]) => new URL(String(path), "http://localhost"))
      .find((url) => url.pathname === "/usage/task-summary")!;
    expect(sourceRequest.searchParams.get("window")).toBe(
      summaryRequests("by_task")[0].searchParams.get("window"),
    );
    const taskTable = await screen.findByRole("table");
    expect(
      await within(taskTable).findByText(LONG_TASK_TITLE),
    ).toBeInTheDocument();
    expect(within(taskTable).getByText("task-1")).toBeInTheDocument();
    expect(screen.getByTitle(LONG_TASK_TITLE)).toBeInTheDocument();

    await userEvent.click(screen.getByText("按天"));
    await waitFor(() => {
      expect(
        screen.getByRole("combobox", { name: "专家" }),
      ).toBeInTheDocument();
      expect(
        summaryRequests("by_day").at(-1)?.searchParams.get("agent_id"),
      ).toBe("agent-1");
    });
  });

  it("preserves the selected admin user in task statistics and export", async () => {
    vi.mocked(useUserRole).mockReturnValue("admin");
    render(<TokenUsagePage />);
    await screen.findByText("900");
    fireEvent.mouseDown(screen.getAllByRole("combobox")[0]);
    await userEvent.click(await screen.findByText("Alice"));
    await userEvent.click(screen.getByText("按任务"));
    await waitFor(() => expect(summaryRequests("by_task")).toHaveLength(1));

    const taskRequest = summaryRequests("by_task")[0];
    expect(taskRequest.pathname).toBe("/admin/usage/summary");
    expect(taskRequest.searchParams.get("user_id")).toBe("7");
    expect(taskRequest.searchParams.has("agent_id")).toBe(false);
    await waitFor(() =>
      expect(
        vi.mocked(request).mock.calls.some(([path]) => {
          const url = new URL(String(path), "http://localhost");
          return (
            url.pathname === "/admin/usage/task-summary" &&
            url.searchParams.get("user_id") === "7"
          );
        }),
      ).toBe(true),
    );
    await userEvent.click(screen.getByRole("button", { name: "导出全部维度" }));
    await waitFor(() => expect(requestBlob).toHaveBeenCalledOnce());
    const exportUrl = new URL(
      String(vi.mocked(requestBlob).mock.calls[0][0]),
      "http://localhost",
    );
    expect(exportUrl.pathname).toBe("/admin/usage/export.xlsx");
    expect(exportUrl.searchParams.get("user_id")).toBe("7");
    expect(exportUrl.searchParams.get("window")).toBe(
      taskRequest.searchParams.get("window"),
    );
  });

  it("keeps full task titles and input, output and turns available on mobile", async () => {
    vi.mocked(useIsMobile).mockReturnValue(true);
    render(<TokenUsagePage />);
    await screen.findByText("900");
    await userEvent.click(screen.getByText("按任务"));

    await screen.findAllByText(LONG_TASK_TITLE);
    expect(screen.getByText("task-1")).toBeInTheDocument();
    expect(screen.getByText("输入 500")).toBeInTheDocument();
    expect(screen.getByText("输出 100")).toBeInTheDocument();
    expect(screen.getByText("3 轮")).toBeInTheDocument();
  });

  it("ignores earlier project and summary responses after the task view has loaded", async () => {
    render(<TokenUsagePage />);
    await screen.findByText("900");
    const pending: {
      granularity: string;
      resolve: (value: ReturnType<typeof summary>) => void;
    }[] = [];
    vi.mocked(request).mockImplementation((path) => {
      if (String(path).includes("/task-summary?")) {
        return Promise.resolve(emptySourceSummary()) as never;
      }
      const granularity = new URL(
        String(path),
        "http://localhost",
      ).searchParams.get("granularity")!;
      return new Promise((resolve) => {
        pending.push({ granularity, resolve });
      }) as never;
    });

    await userEvent.click(screen.getByText("按项目"));
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(screen.queryByText("900")).not.toBeInTheDocument();
    await userEvent.click(screen.getByText("汇总"));
    await userEvent.click(screen.getByText("按任务"));
    expect(screen.queryByRole("table")).not.toBeInTheDocument();

    await act(async () => {
      pending
        .find(({ granularity }) => granularity === "by_task")!
        .resolve(summary("by_task"));
    });
    expect(
      within(screen.getByRole("table")).getByText(LONG_TASK_TITLE),
    ).toBeInTheDocument();
    await act(async () => {
      for (const request of pending.filter(
        ({ granularity }) => granularity !== "by_task",
      )) {
        request.resolve(summary(request.granularity));
      }
    });
    expect(
      within(screen.getByRole("table")).getByText(LONG_TASK_TITLE),
    ).toBeInTheDocument();
    expect(screen.queryByText("产品规划")).not.toBeInTheDocument();
    expect(screen.queryByText("900")).not.toBeInTheDocument();
  });

  it("ignores an earlier project request failure while the task request is pending", async () => {
    render(<TokenUsagePage />);
    await screen.findByText("900");
    let rejectProject: (reason: Error) => void = () => {};
    let resolveTask: (value: ReturnType<typeof summary>) => void = () => {};
    vi.mocked(request).mockImplementation((path) => {
      if (String(path).includes("/task-summary?")) {
        return Promise.resolve(emptySourceSummary()) as never;
      }
      const granularity = new URL(
        String(path),
        "http://localhost",
      ).searchParams.get("granularity");
      return new Promise((resolve, reject) => {
        if (granularity === "by_project") rejectProject = reject;
        else resolveTask = resolve;
      }) as never;
    });
    await userEvent.click(screen.getByText("按项目"));
    await userEvent.click(screen.getByText("按任务"));
    await act(async () => rejectProject(new Error("obsolete project failure")));
    expect(
      screen.queryByText("obsolete project failure"),
    ).not.toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    await act(async () => resolveTask(summary("by_task")));
    expect(
      within(screen.getByRole("table")).getByText(LONG_TASK_TITLE),
    ).toBeInTheDocument();
  });
});
