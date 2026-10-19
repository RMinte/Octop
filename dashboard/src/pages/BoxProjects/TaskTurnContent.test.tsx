import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type {
  TaskContentBlock,
  TaskPlan,
  TaskPresentationSettings,
  TaskTurn,
} from "../../api/modules/projects";
import zh from "../../locales/zh.json";
import TaskTurnContent from "./TaskTurnContent";

vi.mock("../../components/Markdown/LazyMarkdown", () => ({
  default: ({ content }: { content: string }) => <div>{content}</div>,
}));
vi.mock("../../hooks/useServerTimezone", () => ({
  useServerTimezone: () => "Asia/Shanghai",
}));
vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string, options?: Record<string, unknown>) => {
      const text = key
        .split(".")
        .reduce<unknown>(
          (value, part) => (value as Record<string, unknown>)?.[part],
          zh,
        );
      return typeof text === "string"
        ? text.replace(/\{\{(\w+)\}\}/g, (match, name) =>
            options?.[name] == null ? match : String(options[name]),
          )
        : key;
    },
  }),
}));

const plan: TaskPlan = {
  source: "harness_todos",
  revision: 2,
  steps: [
    { content: "核对接口", status: "completed" },
    { content: "接入页面", status: "in_progress" },
    { content: "检查交互", status: "in_progress" },
    { content: "运行验证", status: "pending" },
  ],
};
const turn: TaskTurn = {
  sequence: 1,
  turn_id: "turn_a",
  request_id: "request_a",
  prompt: "接入计划",
  attachments: [],
  reply: "",
  reasoning: "",
  content_blocks: [],
  content_revision: 0,
  plan,
  plan_revision: 2,
  status: "running",
  error_code: "",
  activity: "",
  artifacts: [],
  model_ref: "provider/model",
  options: {},
  web_search_enabled: false,
  project_revision: 1,
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
};
const block = (
  id: string,
  type: TaskContentBlock["type"],
  text: string,
): TaskContentBlock => ({
  block_id: id,
  type,
  text,
  status: "completed",
  tool_call_id: null,
  tool_name: null,
});

const settings = (
  actionTimeline: boolean,
  stepPanel: boolean,
): TaskPresentationSettings => ({
  action_timeline_enabled: actionTimeline,
  step_panel_enabled: stepPanel,
});
const scopedPlan: TaskPlan = {
  source: "octop_planning",
  plan_id: "plan_a",
  title: "接口接入计划",
  revision: 5,
  active_step_id: "step_b",
  steps: [
    {
      step_id: "step_a",
      content: "检查接口",
      status: "completed",
      result: "接口已核对。",
      started_at: "2026-10-19T00:00:00Z",
      finished_at: "2026-10-19T00:00:02Z",
    },
    {
      step_id: "step_b",
      content: "接入页面",
      status: "in_progress",
      result: "",
      started_at: "2026-10-19T00:00:03Z",
      finished_at: null,
    },
  ],
  retired_steps: [],
};
const scopedBlock = (
  id: string,
  stepID: string,
  type: TaskContentBlock["type"],
  text: string,
): TaskContentBlock => ({
  ...block(id, type, text),
  plan_id: "plan_a",
  step_id: stepID,
});
const executionBlocks: TaskContentBlock[] = [
  scopedBlock("a-progress", "step_a", "progress", "历史步骤的公开进展。"),
  scopedBlock("b-progress", "step_b", "progress", "正在检查页面接线。"),
  scopedBlock("reasoning", "step_b", "reasoning", "保存的思考原文。"),
  scopedBlock("summary", "step_b", "reasoning_summary", "保存的思考摘要。"),
  {
    ...scopedBlock("tool", "step_b", "tool", ""),
    tool_call_id: "call_a",
    tool_name: "read_file",
    tool_label: "读取文件",
    brief: "检查页面接线",
    arguments: { path: "src/page.tsx" },
    output: "真实工具输出",
    started_at: "2026-10-19T00:00:04Z",
    finished_at: "2026-10-19T00:00:06Z",
    duration_ms: 2000,
  },
  block("unscoped", "progress", "创建计划前的活动。"),
  { ...block("other-plan", "progress", "其他计划活动。"), plan_id: "other" },
  block("answer", "text", "最终答复。"),
];
const plannedTurn = {
  ...turn,
  plan: scopedPlan,
  content_blocks: executionBlocks,
};

function stepElement(container: HTMLElement, id: string) {
  const element = container.querySelector<HTMLElement>(
    `[data-step-id="${id}"]`,
  );
  expect(element).not.toBeNull();
  return element!;
}

describe("计划面板与动作链路互斥", () => {
  it.each([true, false])(
    "步骤面板开启时只展示步骤结果和当前短活动，动作开关为 %s",
    (actionTimeline) => {
      const original = JSON.stringify(plannedTurn);
      const { container } = render(
        <TaskTurnContent
          turn={plannedTurn}
          settings={settings(actionTimeline, true)}
        />,
      );
      const panel = screen.getByText("接口接入计划").closest("details")!;
      expect(panel).toHaveAttribute("open");
      expect(within(panel).getByText("1 / 2 已完成")).toBeVisible();
      expect(
        within(stepElement(container, "step_b")).getByText("检查页面接线"),
      ).toBeVisible();
      expect(
        within(stepElement(container, "step_a")).getByText("接口已核对。"),
      ).toBeInTheDocument();
      expect(screen.getByText("最终答复。")).toBeVisible();
      for (const text of [
        "历史步骤的公开进展。",
        "正在检查页面接线。",
        "保存的思考原文。",
        "保存的思考摘要。",
        "创建计划前的活动。",
        "其他计划活动。",
        "真实工具输出",
        "调用参数",
        "思考详情",
      ]) {
        expect(screen.queryByText(text)).not.toBeInTheDocument();
      }
      expect(
        screen.queryByRole("region", { name: "动作链路" }),
      ).not.toBeInTheDocument();
      expect(
        screen.queryByRole("region", { name: "思考过程" }),
      ).not.toBeInTheDocument();
      expect(JSON.stringify(plannedTurn)).toBe(original);
    },
  );

  it("手动折叠计划仍隐藏整个动作区，完成历史也不重新显示未归属记录", async () => {
    const user = userEvent.setup();
    const { rerender } = render(<TaskTurnContent turn={plannedTurn} />);
    await user.click(screen.getByText("接口接入计划").closest("summary")!);
    expect(
      screen.getByText("接口接入计划").closest("details"),
    ).not.toHaveAttribute("open");
    expect(
      screen.queryByRole("region", { name: "动作链路" }),
    ).not.toBeInTheDocument();
    rerender(
      <TaskTurnContent turn={{ ...plannedTurn, status: "completed" }} />,
    );
    expect(screen.queryByText("创建计划前的活动。")).not.toBeInTheDocument();
    expect(screen.queryByText("保存的思考原文。")).not.toBeInTheDocument();
    expect(screen.getByText("最终答复。")).toBeVisible();
  });

  it.each([true, false])(
    "有计划但关闭步骤面板时恢复统一过程区，动作开关为 %s",
    async (actionTimeline) => {
      const user = userEvent.setup();
      render(
        <TaskTurnContent
          turn={plannedTurn}
          settings={settings(actionTimeline, false)}
        />,
      );
      expect(screen.queryByText("接口接入计划")).not.toBeInTheDocument();
      const process = screen.getByRole("region", {
        name: actionTimeline ? "动作链路" : "思考过程",
      });
      for (const text of [
        "历史步骤的公开进展。",
        "正在检查页面接线。",
        "检查页面接线",
        "创建计划前的活动。",
        "其他计划活动。",
      ]) {
        expect(within(process).getByText(text)).toBeVisible();
      }
      expect(within(process).queryByText("最终答复。")).not.toBeInTheDocument();
      if (actionTimeline) {
        expect(screen.queryByText("保存的思考原文。")).not.toBeInTheDocument();
        expect(screen.queryByText("保存的思考摘要。")).not.toBeInTheDocument();
      } else {
        await user.click(
          within(process).getByText("思考过程", { selector: "summary" }),
        );
        await user.click(within(process).getByText("思考摘要"));
        expect(within(process).getByText("保存的思考原文。")).toBeVisible();
        expect(within(process).getByText("保存的思考摘要。")).toBeVisible();
      }
    },
  );

  it.each([
    [true, true],
    [true, false],
    [false, true],
    [false, false],
  ])(
    "无计划回合不受步骤面板影响，动作 %s / 面板 %s",
    (actionTimeline, stepPanel) => {
      render(
        <TaskTurnContent
          turn={{ ...plannedTurn, plan: null }}
          settings={settings(actionTimeline, stepPanel)}
        />,
      );
      expect(screen.getAllByRole("region")).toHaveLength(1);
      expect(
        screen.getByRole("region", {
          name: actionTimeline ? "动作链路" : "思考过程",
        }),
      ).toBeVisible();
      expect(screen.queryByText("接口接入计划")).not.toBeInTheDocument();
      expect(screen.queryByText("保存的思考原文。") != null).toBe(
        !actionTimeline,
      );
    },
  );

  it("设置变化立即重投影已保存的回合，生成时的开关不控制当前展示", () => {
    const stored = {
      ...plannedTurn,
      options: { action_timeline_enabled: false },
    };
    const { rerender } = render(
      <TaskTurnContent turn={stored} settings={settings(true, true)} />,
    );
    expect(screen.queryByRole("region")).not.toBeInTheDocument();
    rerender(
      <TaskTurnContent turn={stored} settings={settings(true, false)} />,
    );
    expect(screen.getByRole("region", { name: "动作链路" })).toBeVisible();
    expect(screen.queryByText("保存的思考原文。")).not.toBeInTheDocument();
    rerender(
      <TaskTurnContent turn={stored} settings={settings(false, false)} />,
    );
    expect(screen.getByRole("region", { name: "思考过程" })).toBeVisible();
    expect(screen.getByText("保存的思考原文。")).toBeInTheDocument();
    rerender(
      <TaskTurnContent turn={stored} settings={settings(false, true)} />,
    );
    expect(screen.queryByRole("region")).not.toBeInTheDocument();
  });

  it("同会话的无计划追问展示过程，不继承前一轮计划", () => {
    render(
      <>
        <TaskTurnContent turn={{ ...plannedTurn, status: "completed" }} />
        <TaskTurnContent
          turn={{
            ...turn,
            plan: null,
            sequence: 2,
            content_blocks: [block("next", "progress", "追问的执行进展。")],
          }}
        />
      </>,
    );
    expect(screen.getAllByRole("region")).toHaveLength(1);
    expect(screen.getByText("追问的执行进展。")).toBeVisible();
    expect(screen.queryByText("创建计划前的活动。")).not.toBeInTheDocument();
  });
});

describe("步骤面板详情", () => {
  it("仅当前步骤采用明确归属的一条最新活动，保留旧动态计划但不按顺序猜归属", () => {
    const { container } = render(
      <TaskTurnContent
        turn={{
          ...plannedTurn,
          plan: {
            ...scopedPlan,
            steps: [...scopedPlan.steps].reverse(),
            retired_steps: [
              {
                step_id: "retired",
                content: "已移除步骤",
                status: "blocked",
                result: "保留的阻塞说明。",
              },
            ],
          },
          content_blocks: [
            ...executionBlocks,
            scopedBlock("unknown", "missing", "progress", "未知步骤活动。"),
            scopedBlock("removed", "retired", "progress", "移除步骤的活动。"),
            scopedBlock("next", "step_b", "progress", "下一条   简短\n活动。"),
          ],
        }}
      />,
    );
    expect(
      within(stepElement(container, "step_b")).getByText("下一条 简短 活动。"),
    ).toBeVisible();
    expect(screen.queryByText("检查页面接线")).not.toBeInTheDocument();
    expect(screen.queryByText("未知步骤活动。")).not.toBeInTheDocument();
    expect(screen.queryByText("移除步骤的活动。")).not.toBeInTheDocument();
    expect(screen.getByText("保留的阻塞说明。")).toBeInTheDocument();
  });

  it("没有公开进展或工具标签时不从思考原文提取当前活动", () => {
    render(
      <TaskTurnContent
        turn={{
          ...plannedTurn,
          content_blocks: [
            scopedBlock(
              "thought",
              "step_b",
              "reasoning",
              "不能成为面板状态的长思考。",
            ),
          ],
        }}
      />,
    );
    expect(
      screen.queryByText("不能成为面板状态的长思考。"),
    ).not.toBeInTheDocument();
    expect(screen.getByText("暂无步骤结果。")).toBeVisible();
  });

  it("保留步骤结果和服务端时间，时间未知不虚报零耗时", () => {
    const { container } = render(<TaskTurnContent turn={plannedTurn} />);
    const a = stepElement(container, "step_a");
    expect(within(a).getByText("2000 毫秒")).toBeInTheDocument();
    expect(
      a.querySelector('time[datetime="2026-10-19T00:00:00Z"]'),
    ).not.toBeNull();
    const b = stepElement(container, "step_b");
    expect(within(b).queryByText("耗时")).not.toBeInTheDocument();
    expect(within(b).queryByText("0 毫秒")).not.toBeInTheDocument();
  });

  it("暂停和终态不把保存的工具标签显示成当前执行活动", () => {
    const { rerender } = render(
      <TaskTurnContent turn={{ ...plannedTurn, status: "paused" }} />,
    );
    expect(screen.queryByText("检查页面接线")).not.toBeInTheDocument();
    rerender(<TaskTurnContent turn={{ ...plannedTurn, status: "failed" }} />);
    expect(screen.queryByText("检查页面接线")).not.toBeInTheDocument();
  });

  it("计划卡提供已有产物入口，折叠面板仍可访问", async () => {
    const open = vi.fn();
    render(
      <TaskTurnContent
        turn={{
          ...plannedTurn,
          status: "completed",
          plan: {
            ...scopedPlan,
            active_step_id: null,
            steps: scopedPlan.steps.map((step) => ({
              ...step,
              status: "completed",
            })),
          },
          artifacts: [
            {
              artifact_id: "artifact_a",
              task_id: "task_a",
              turn_id: "turn_a",
              name: "结果.txt",
              media_type: "text/plain",
              byte_count: 1,
              sha256: "hash",
              source_path: null,
              download_url: "/artifact",
              created_at: turn.created_at,
            },
          ],
        }}
        onOpenArtifacts={open}
      />,
    );
    expect(
      screen.getByText("接口接入计划").closest("details"),
    ).not.toHaveAttribute("open");
    await userEvent
      .setup()
      .click(screen.getByRole("button", { name: "查看产物（1）" }));
    expect(open).toHaveBeenCalledOnce();
  });

  it("有结果的跳过步骤计入已结束进度，计划完成后默认折叠", () => {
    render(
      <TaskTurnContent
        turn={{
          ...plannedTurn,
          plan: {
            ...scopedPlan,
            active_step_id: null,
            steps: [
              scopedPlan.steps[0],
              {
                ...scopedPlan.steps[1],
                status: "skipped",
                result: "该项无需修改。",
              },
            ],
          },
        }}
      />,
    );
    const card = screen.getByText("接口接入计划").closest("details")!;
    expect(card).not.toHaveAttribute("open");
    expect(within(card).getByText("2 / 2 已完成")).toBeVisible();
    expect(screen.getByText("该项无需修改。")).toBeInTheDocument();
  });
});

describe("过程区执行详情与旧记录", () => {
  it("公开进展与工具保持事件顺序，完整参数和实际输出只在工具详情中", async () => {
    render(<TaskTurnContent turn={{ ...plannedTurn, plan: null }} />);
    const process = screen.getByRole("region", { name: "动作链路" });
    const before = within(process).getByText("正在检查页面接线。");
    const label = within(process).getByText("检查页面接线");
    expect(
      before.compareDocumentPosition(label) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    const tool = label.closest("details")!;
    expect(tool).not.toHaveAttribute("open");
    await userEvent.setup().click(label);
    expect(within(tool).getByLabelText("调用参数")).toHaveTextContent(
      '"path": "src/page.tsx"',
    );
    expect(within(tool).getByLabelText("返回内容")).toHaveTextContent(
      "真实工具输出",
    );
    expect(within(tool).getByText("2000 毫秒")).toBeVisible();
  });

  it.each([
    [{}, "未确认执行"],
    [{ status: "failed" }, "执行失败"],
    [{ status: "streaming" }, "待审批"],
    [{ status: "streaming", started_at: "2026-10-19T00:00:00Z" }, "执行中"],
    [{ status: "interrupted", started_at: "2026-10-19T00:00:00Z" }, "已停止"],
    [{ tool_name: "client__app", output: "done" }, "已完成"],
  ] as const)("工具状态来自真实执行字段 %j", (overrides, label) => {
    render(
      <TaskTurnContent
        turn={{
          ...turn,
          plan: null,
          status: "waiting_approval",
          content_blocks: [
            {
              ...block("tool", "tool", ""),
              tool_name: "read_file",
              tool_label: "工具执行",
              ...overrides,
            },
          ],
        }}
      />,
    );
    expect(
      within(screen.getByText("工具执行").closest("details")!).getByText(label),
    ).toBeVisible();
  });

  it("旧版无内容块时按当前开关保留 Thinking 和最终回答，面板优先时隐藏过程", () => {
    const legacy = {
      ...turn,
      plan: null,
      reply: "旧版答复。",
      reasoning: "旧版思考原文。",
      activity: "read_file",
    };
    const { rerender } = render(
      <TaskTurnContent turn={legacy} settings={settings(true, true)} />,
    );
    expect(screen.queryByText("旧版思考原文。")).not.toBeInTheDocument();
    expect(screen.getByText("旧版答复。")).toBeVisible();
    rerender(
      <TaskTurnContent turn={legacy} settings={settings(false, true)} />,
    );
    expect(screen.getByText("旧版思考原文。")).toBeInTheDocument();
    rerender(
      <TaskTurnContent
        turn={{ ...legacy, plan }}
        settings={settings(false, true)}
      />,
    );
    expect(screen.queryByRole("region")).not.toBeInTheDocument();
    expect(screen.queryByText("旧版思考原文。")).not.toBeInTheDocument();
    expect(screen.getAllByText("旧版答复。")).toHaveLength(1);
  });

  it("已保存计划快照不重复成为正文", () => {
    render(
      <TaskTurnContent
        turn={{
          ...turn,
          plan: null,
          content_blocks: [
            { ...block("old", "plan", "历史计划不应成为正文"), plan },
          ],
        }}
      />,
    );
    expect(screen.queryByText("历史计划不应成为正文")).not.toBeInTheDocument();
    expect(screen.queryByText("执行计划")).not.toBeInTheDocument();
  });
});
