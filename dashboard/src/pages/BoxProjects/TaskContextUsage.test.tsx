import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  projectTasksApi,
  type TaskContextUsage as ContextUsage,
} from "../../api/modules/projects";
import zh from "../../locales/zh.json";
import TaskContextUsage, { TaskTurnUsage } from "./TaskContextUsage";

vi.mock("../../api/modules/projects", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../api/modules/projects")>()),
  projectTasksApi: { contextUsage: vi.fn() },
}));
vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string, options?: Record<string, unknown>) => {
      const value = key
        .split(".")
        .reduce<unknown>(
          (current, part) => (current as Record<string, unknown>)?.[part],
          zh,
        );
      return String(value ?? key).replace(
        /\{\{(\w+)\}\}/g,
        (match, name: string) =>
          options && name in options ? String(options[name]) : match,
      );
    },
  }),
}));

const api = vi.mocked(projectTasksApi);
const usage: ContextUsage = {
  max_tokens: 10000,
  used_tokens: 1500,
  input_tokens: 1500,
  output_tokens: 150,
  segments: [
    { key: "system_prompt", tokens: 200 },
    { key: "conversation", tokens: 1300 },
  ],
  source: "model_request",
  estimated: false,
};
const completed = [
  {
    turn_id: "turn_a",
    status: "completed" as const,
    input_tokens: 4000,
    output_tokens: 500,
  },
];
const flush = async () => {
  await act(async () => {
    await Promise.resolve();
  });
};
beforeEach(() => {
  vi.useFakeTimers();
  vi.clearAllMocks();
  api.contextUsage.mockResolvedValue(usage);
});
afterEach(() => {
  vi.clearAllTimers();
  vi.useRealTimers();
});

describe("任务上下文与计费用量", () => {
  it("将最近请求占用、估算细分与已加载轮次计费分开显示", async () => {
    render(
      <TaskContextUsage
        taskID="task_a"
        active={false}
        turns={[...completed, { turn_id: "legacy", status: "completed" }]}
      />,
    );
    await flush();
    const details = screen.getByRole("button", { name: "上下文占用细分" });
    expect(details).toHaveTextContent(
      "上下文占用：1,500 / 10,000 tokens · 15%",
    );
    expect(
      screen.getByText(/已加载 2 轮计费用量：输入 4,000 · 输出 500 tokens/),
    ).toBeInTheDocument();
    expect(screen.getByText(/其中 1 轮未记录，未计入汇总/)).toBeInTheDocument();
    fireEvent.click(details);
    expect(
      screen.getByText("各部分为估算，合计可能与模型计量不同。"),
    ).toBeInTheDocument();
    expect(screen.getByText("系统提示：200 tokens")).toBeInTheDocument();
    expect(screen.getByText("会话内容：1,300 tokens")).toBeInTheDocument();
    expect(
      screen.getByText("最近请求：输入 1,500 · 输出 150 tokens"),
    ).toBeInTheDocument();
  });

  it("无实际上下文记录时不显示零占用和百分比", async () => {
    api.contextUsage.mockResolvedValue({
      ...usage,
      used_tokens: 0,
      input_tokens: 0,
      output_tokens: 0,
      segments: [],
      source: "empty",
    });
    render(<TaskContextUsage taskID="task_a" active={false} turns={[]} />);
    await flush();
    expect(screen.getByText("尚无上下文用量数据")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "上下文占用细分" })).toBeNull();
    expect(screen.queryByText(/0%/)).toBeNull();
    expect(screen.getByText("已加载 0 轮暂无计费用量记录")).toBeInTheDocument();
  });

  it("标明压缩后的上下文是估算，计费记录仍使用真实轮次数据", async () => {
    api.contextUsage.mockResolvedValue({
      ...usage,
      estimated: true,
      used_tokens: 300,
      input_tokens: 300,
      output_tokens: 0,
    });
    render(
      <TaskContextUsage taskID="task_a" active={false} turns={completed} />,
    );
    await flush();
    const details = screen.getByRole("button", { name: "上下文占用细分" });
    expect(details).toHaveTextContent(
      "上下文占用：300 / 10,000 tokens · 3% · 估算",
    );
    fireEvent.click(details);
    expect(
      screen.getByText("当前估算：输入 300 · 输出 0 tokens"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("已加载 1 轮计费用量：输入 4,000 · 输出 500 tokens"),
    ).toBeInTheDocument();
  });

  it("活动中每秒刷新，结束和新的压缩轮完成时立即刷新", async () => {
    const running = [{ ...completed[0], status: "running" as const }];
    const view = render(
      <TaskContextUsage taskID="task_a" active turns={running} />,
    );
    await flush();
    expect(api.contextUsage).toHaveBeenCalledTimes(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(999);
    });
    expect(api.contextUsage).toHaveBeenCalledTimes(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(api.contextUsage).toHaveBeenCalledTimes(2);
    view.rerender(
      <TaskContextUsage taskID="task_a" active={false} turns={completed} />,
    );
    await flush();
    expect(api.contextUsage).toHaveBeenCalledTimes(3);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(api.contextUsage).toHaveBeenCalledTimes(3);
    view.rerender(
      <TaskContextUsage
        taskID="task_a"
        active={false}
        turns={[...completed, { turn_id: "turn_compact", status: "completed" }]}
      />,
    );
    await flush();
    expect(api.contextUsage).toHaveBeenCalledTimes(4);
  });

  it("切换任务时中止旧请求，迟到结果不能覆盖新任务", async () => {
    let finish!: (value: ContextUsage) => void;
    api.contextUsage.mockReturnValueOnce(
      new Promise((resolve) => {
        finish = resolve;
      }),
    );
    const view = render(
      <TaskContextUsage taskID="task_a" active turns={completed} />,
    );
    const signal = api.contextUsage.mock.calls[0][1]!;
    view.rerender(
      <TaskContextUsage taskID="task_b" active={false} turns={completed} />,
    );
    await flush();
    expect(signal.aborted).toBe(true);
    await act(async () => {
      finish({ ...usage, used_tokens: 9000 });
    });
    expect(
      screen.getByRole("button", { name: "上下文占用细分" }),
    ).toHaveTextContent("1,500 / 10,000");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(api.contextUsage).toHaveBeenCalledTimes(2);
  });

  it("读取失败可以独立重试，不需要重新获取任务详情", async () => {
    api.contextUsage.mockRejectedValueOnce(new Error("usage unavailable"));
    render(
      <TaskContextUsage taskID="task_a" active={false} turns={completed} />,
    );
    await flush();
    expect(screen.getByText("usage unavailable")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重试用量" }));
    await flush();
    expect(api.contextUsage).toHaveBeenCalledTimes(2);
    expect(screen.queryByText("usage unavailable")).toBeNull();
    expect(
      screen.getByRole("button", { name: "上下文占用细分" }),
    ).toBeInTheDocument();
  });

  it("每轮计费区分零用量与没有记录", () => {
    const view = render(
      <TaskTurnUsage turn={{ input_tokens: 0, output_tokens: 0 }} />,
    );
    expect(
      screen.getByText(/本轮计费用量：输入 0 · 输出 0 tokens/),
    ).toBeInTheDocument();
    view.rerender(<TaskTurnUsage turn={{}} />);
    expect(screen.queryByText(/本轮计费用量/)).toBeNull();
  });
});

it("does not present ACP compatibility zeros as measured turn usage", async () => {
  const { engineCapabilities } = await import("./taskEngine");
  const engine = {
    engine_id: "acp",
    connection_id: "codex",
    model_id: null,
  } as const;
  render(
    <TaskTurnUsage
      turn={{
        input_tokens: 0,
        output_tokens: 0,
        execution_engine: {
          ...engine,
          label: "Codex",
          capabilities: engineCapabilities(engine),
        },
      }}
    />,
  );
  expect(screen.getByText(/用量由执行引擎管理/)).toBeInTheDocument();
  expect(screen.queryByText(/输入 0/)).not.toBeInTheDocument();
});

it("counts ACP as missing in mixed history and does not poll Octop context for an ACP task", async () => {
  const { engineCapabilities } = await import("./taskEngine");
  const engine = {
    engine_id: "acp",
    connection_id: "codex",
    model_id: null,
  } as const;
  render(
    <TaskContextUsage
      taskID="mixed"
      active
      engine={engine}
      turns={[
        ...completed,
        {
          turn_id: "acp",
          status: "completed",
          input_tokens: 0,
          output_tokens: 0,
          execution_engine: {
            ...engine,
            label: "Codex",
            capabilities: engineCapabilities(engine),
          },
        },
      ]}
    />,
  );
  await flush();
  expect(
    screen.getByText(zh.projects.tasks.contextUsage.engineContextManaged),
  ).toBeInTheDocument();
  expect(
    screen.getByText(/已加载 2 轮计费用量：输入 4,000 · 输出 500 tokens/),
  ).toBeInTheDocument();
  expect(screen.getByText(/其中 1 轮未记录，未计入汇总/)).toBeInTheDocument();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(2000);
  });
  expect(api.contextUsage).not.toHaveBeenCalled();
  expect(
    screen.queryByRole("button", { name: "上下文占用细分" }),
  ).not.toBeInTheDocument();
});
