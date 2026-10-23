import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { request } from "../../../api/request";
import {
  taskUsageApi,
  type ReferenceCostAmounts,
  type RetainedSourceTotals,
  type RetainedTaskUsageTotals,
  type TaskCallUsage,
  type TaskTokenCounts,
  type TaskTokenCoverage,
  type TaskTokenTotals,
  type TaskUsageItem,
  type TaskUsagePage,
} from "../../../api/modules/taskUsage";
import TaskSourceUsage from "./TaskSourceUsage";

vi.mock("../../../api/request", () => ({ request: vi.fn() }));
vi.mock("react-i18next", async () => {
  const { default: zh } = await import("../../../locales/zh.json");
  const strings = zh.tokenUsage as Record<string, string>;
  return {
    useTranslation: () => ({
      t: (key: string, options?: { count?: number }) =>
        (strings[key.replace(/^tokenUsage\./, "")] ?? key).replace(
          "{{count}}",
          String(options?.count ?? ""),
        ),
    }),
  };
});

const fields = [
  "input_tokens",
  "output_tokens",
  "total_tokens",
  "uncached_input_tokens",
  "cache_read_tokens",
  "cache_write_tokens",
  "reasoning_tokens",
] as const;

function referenceCost(
  overrides: Partial<ReferenceCostAmounts> = {},
): ReferenceCostAmounts {
  return {
    amount_usd: null,
    known_amount_usd: null,
    lower_bound_usd: null,
    upper_bound_usd: null,
    status: "no_data",
    missing_fields: [],
    assumptions: [],
    ...overrides,
  };
}

function callUsage(
  calls = 0,
  total: number | null = null,
  missing = 0,
  cost: ReferenceCostAmounts = referenceCost(),
): TaskCallUsage {
  return {
    calls,
    statuses: calls ? { succeeded: calls } : {},
    tokens: {
      ...Object.fromEntries(fields.map((key) => [key, null])),
      total_tokens: total,
    } as TaskTokenTotals,
    missing_calls: {
      ...Object.fromEntries(fields.map((key) => [key, calls])),
      total_tokens: missing,
    } as TaskTokenCounts,
    derived_calls: Object.fromEntries(
      fields.map((key) => [key, 0]),
    ) as TaskTokenCounts,
    coverage: {
      ...Object.fromEntries(
        fields.map((key) => [key, calls ? "not_collected" : "no_data"]),
      ),
      total_tokens: !calls
        ? "no_data"
        : total === null
        ? "not_collected"
        : missing
        ? "partial"
        : "recorded",
    } as TaskTokenCoverage,
    attribution_counts: {},
    reference_cost: {
      ...cost,
      estimated_calls: 0,
      estimated_range_calls: 0,
      partial_calls: 0,
      unknown_calls: 0,
    },
  };
}

function retainedSource(total: number | null): RetainedSourceTotals {
  return {
    input_tokens: total,
    output_tokens: total === null ? null : 0,
    total_tokens: total,
    usage_records: total === null ? 0 : 1,
    source_basis: total === null ? "no_data" : "configuration_inference",
    reference_cost: {
      ...referenceCost(),
      estimated_records: 0,
      estimated_range_records: 0,
      partial_records: 0,
      unknown_records: 0,
    },
  };
}

function retained(
  total: number | null,
  box: number | null = null,
  third: number | null = null,
): RetainedTaskUsageTotals {
  return {
    ...retainedSource(total),
    turns: total === null ? null : 1,
    coverage: total === null ? "no_data" : "recorded",
    source_attribution: total === null ? "no_data" : "inferred",
    sources: { box: retainedSource(box), third_party: retainedSource(third) },
    unattributed_usage: retainedSource(null),
    measurement_unit: "retained_task_journal",
    cannot_add_to_call_usage: true,
  };
}

function task(
  taskId: string,
  overrides: Partial<TaskUsageItem> = {},
): TaskUsageItem {
  return {
    task_id: taskId,
    title: taskId,
    project_id: "",
    project_name: "",
    last_usage_at: 1,
    call_usage: callUsage(),
    sources: { box: callUsage(), third_party: callUsage() },
    retained_task_usage: retained(null),
    ...overrides,
  };
}

function page(
  items: TaskUsageItem[] = [],
  overrides: Partial<TaskUsagePage> = {},
): TaskUsagePage {
  const rates = {
    input: "10",
    output: "50",
    cache_read: "1",
    cache_write_30m: "12.5",
    cache_write_12h: "25",
  };
  return {
    window: "all",
    range_start: 0,
    range_end: 1,
    timezone: "Asia/Shanghai",
    generated_at: 1,
    account_scope: "user",
    user_id: 7,
    measurement_unit: "logical_model_invocation",
    recording_scope: "retained_records_only",
    history_complete: false,
    physical_attempts_available: false,
    capabilities: {
      source_breakdown: true,
      reference_cost: true,
      historical_source_inference: true,
      actual_cost: false,
      execution_location: false,
      savings: false,
      budget: false,
    },
    pricing: {
      model: "gpt-6-astra",
      version: "fixture",
      checked_at: "2026-10-23",
      currency: "USD",
      basis: "openai_reference_estimate",
      service_tier: "standard",
      rate_unit: "USD_per_million_tokens",
      long_context_threshold: 272000,
      short_context: rates,
      long_context: rates,
      source_urls: [],
      assumptions: [],
    },
    total_tasks: items.length,
    offset: 0,
    limit: 20,
    next_offset: null,
    totals: {
      call_usage: callUsage(),
      sources: { box: callUsage(), third_party: callUsage() },
      retained_task_usage: retained(null),
    },
    unassigned_call_usage: callUsage(),
    unassigned_sources: { box: callUsage(), third_party: callUsage() },
    items,
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(request).mockResolvedValue(page());
  const original = window.getComputedStyle;
  vi.spyOn(window, "getComputedStyle").mockImplementation((element) =>
    original(element),
  );
});

describe("TaskSourceUsage", () => {
  it("uses receipts before historical inference without adding totals and keeps unknowns visible", async () => {
    const old = retained(900, 800, 100);
    old.reference_cost = {
      ...old.reference_cost,
      ...referenceCost({ status: "estimated", amount_usd: "99" }),
    };
    const historical = retained(570, 200, 300);
    historical.source_attribution = "partial";
    historical.unattributed_usage = {
      ...retainedSource(70),
      source_basis: "unattributed",
    };
    historical.reference_cost = {
      ...historical.reference_cost,
      ...referenceCost({
        status: "estimated_range",
        lower_bound_usd: "0.1",
        upper_bound_usd: "0.2",
      }),
    };
    const fresh = task("new-receipts", {
      retained_task_usage: old,
      call_usage: callUsage(
        3,
        120,
        2,
        referenceCost({ status: "partial", known_amount_usd: "0.123" }),
      ),
      sources: { box: callUsage(1, 120), third_party: callUsage(2, null, 2) },
    });
    const zero = task("reported-zero", {
      call_usage: callUsage(1, 0),
      sources: { box: callUsage(1, 0), third_party: callUsage() },
    });
    const result = page(
      [fresh, task("historical", { retained_task_usage: historical }), zero],
      {
        total_tasks: 42,
        totals: {
          call_usage: callUsage(9, 123456, 4),
          sources: {
            box: callUsage(4, 120000),
            third_party: callUsage(5, 3456, 4),
          },
          retained_task_usage: retained(999999, 900000, 99999),
        },
        unassigned_call_usage: callUsage(2, null, 2),
      },
    );
    vi.mocked(request).mockResolvedValue(result);
    render(<TaskSourceUsage windowKey="today" adminUserFilter={null} />);

    const table = (await screen.findAllByText("new-receipts"))[0].closest(
      "table",
    )!;
    const newRow = within(table).getAllByText("new-receipts")[0].closest("tr")!;
    const columns = within(newRow).getAllByRole("cell");
    expect(columns[1]).toHaveTextContent("900");
    expect(columns[2]).toHaveTextContent("120");
    expect(columns[3]).toHaveTextContent("未知");
    expect(columns[4]).toHaveTextContent("2");
    expect(columns[5]).toHaveTextContent("$0.123");
    expect(columns[5]).toHaveTextContent("已知小计，另有未知");
    expect(columns[6]).toHaveTextContent("新调用流水");
    expect(within(newRow).queryByText("800")).not.toBeInTheDocument();
    expect(within(newRow).queryByText("$99")).not.toBeInTheDocument();

    const historyRow = within(table)
      .getAllByText("historical")[0]
      .closest("tr")!;
    expect(historyRow).toHaveTextContent("历史配置推断");
    expect(historyRow).toHaveTextContent("$0.1–$0.2");
    expect(historyRow).toHaveTextContent("参考区间");
    expect(historyRow).toHaveTextContent("未能归属来源的 Tokens: 70");
    expect(within(historyRow).getAllByRole("cell")[4]).toHaveTextContent(
      "无记录",
    );
    const zeroRow = within(table)
      .getAllByText("reported-zero")[0]
      .closest("tr")!;
    expect(within(zeroRow).getAllByRole("cell")[1]).toHaveTextContent("无记录");
    expect(within(zeroRow).getAllByRole("cell")[2]).toHaveTextContent("0");
    expect(within(zeroRow).getAllByRole("cell")[3]).toHaveTextContent("无记录");

    const totals = screen.getByRole("group", {
      name: "所选范围合计（全部任务，非当前页）",
    });
    expect(totals).toHaveTextContent("123,456");
    expect(totals).toHaveTextContent("999,999");
    expect(totals).toHaveTextContent("42");
    expect(
      screen.getByText(/另有 2 次调用尚未明确归属任务/),
    ).toBeInTheDocument();
    expect(screen.getByText(/不代表实际账单/)).toBeInTheDocument();
  });

  it("routes user scopes through the API wrapper with server pagination", async () => {
    for (const userFilter of [null, "all", 7] as const) {
      await taskUsageApi.summary({
        window: "range:2026-10-01:2026-10-23",
        userFilter,
        offset: 20,
        limit: 20,
        task_id: "task-filter",
      });
      const url = new URL(
        String(vi.mocked(request).mock.calls.at(-1)![0]),
        "http://localhost",
      );
      expect(url.pathname).toBe(
        userFilter === null
          ? "/usage/task-summary"
          : "/admin/usage/task-summary",
      );
      expect(url.searchParams.get("user_id")).toBe(
        typeof userFilter === "number" ? "7" : null,
      );
      expect(url.searchParams.get("task_id")).toBe("task-filter");
      expect(url.searchParams.get("offset")).toBe("20");
      expect(url.searchParams.get("window")).toBe(
        "range:2026-10-01:2026-10-23",
      );
    }
  });

  it("resets pagination on scope changes, ignores stale pages and refreshes only on request", async () => {
    vi.mocked(request).mockResolvedValue(
      page([task("first-page")], { total_tasks: 41, next_offset: 20 }),
    );
    const view = render(
      <TaskSourceUsage windowKey="today" adminUserFilter={null} />,
    );
    await screen.findAllByText("first-page");
    let resolveOld: (result: TaskUsagePage) => void = () => {};
    vi.mocked(request).mockImplementation((path) => {
      const url = new URL(String(path), "http://localhost");
      if (
        url.pathname === "/usage/task-summary" &&
        url.searchParams.get("offset") === "20"
      ) {
        return new Promise((resolve) => {
          resolveOld = resolve;
        }) as never;
      }
      return Promise.resolve(page([task("new-owner")])) as never;
    });
    await userEvent.click(screen.getByTitle("2"));
    await waitFor(() =>
      expect(String(vi.mocked(request).mock.calls.at(-1)![0])).toContain(
        "offset=20",
      ),
    );
    view.rerender(
      <TaskSourceUsage windowKey="yesterday" adminUserFilter={7} />,
    );
    await screen.findAllByText("new-owner");
    const latest = new URL(
      String(vi.mocked(request).mock.calls.at(-1)![0]),
      "http://localhost",
    );
    expect(latest.pathname).toBe("/admin/usage/task-summary");
    expect(latest.searchParams.get("user_id")).toBe("7");
    expect(latest.searchParams.get("offset")).toBe("0");
    await act(async () => resolveOld(page([task("obsolete-page")])));
    expect(screen.queryByText("obsolete-page")).not.toBeInTheDocument();
    const before = vi.mocked(request).mock.calls.length;
    await userEvent.click(screen.getByRole("button", { name: "刷新来源统计" }));
    await waitFor(() => expect(request).toHaveBeenCalledTimes(before + 1));
  });

  it("shows an incomplete known subtotal when a reference estimate has no upper bound", async () => {
    const item = task("unknown-cost", {
      call_usage: callUsage(
        1,
        null,
        1,
        referenceCost({ status: "unknown", lower_bound_usd: "0.004" }),
      ),
    });
    vi.mocked(request).mockResolvedValue(page([item]));
    render(<TaskSourceUsage windowKey="all" adminUserFilter={null} />);
    const row = (await screen.findAllByText("unknown-cost"))[0].closest("tr")!;
    expect(row).toHaveTextContent("$0.004");
    expect(row).toHaveTextContent("已知小计，另有未知");
    expect(row).not.toHaveTextContent("参考区间");
  });

  it("keeps request errors local and allows a manual retry", async () => {
    vi.mocked(request).mockRejectedValueOnce(
      new Error("source request failed"),
    );
    render(<TaskSourceUsage windowKey="all" adminUserFilter={null} />);
    expect(
      await screen.findByText("source request failed"),
    ).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "刷新来源统计" }));
    await screen.findByText("区间内无记录");
    expect(screen.queryByText("source request failed")).not.toBeInTheDocument();
  });
});
