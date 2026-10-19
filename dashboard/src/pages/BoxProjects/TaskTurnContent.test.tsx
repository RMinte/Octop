import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type {
  TaskContentBlock,
  TaskPlan,
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

describe("任务计划和动作进展", () => {
  it("执行中展开最新计划，显示真实完成数与全部当前项，不重复显示历史计划块", () => {
    render(
      <TaskTurnContent
        turn={{
          ...turn,
          content_blocks: [
            {
              ...block("old-plan", "plan", "历史计划不应成为正文"),
              plan: { ...plan, revision: 1 },
            },
          ],
        }}
      />,
    );
    const card = screen.getByText("执行计划").closest("details")!;
    expect(card).toHaveAttribute("open");
    expect(screen.getByText("1 / 4 已完成")).toBeVisible();
    expect(screen.getByText("当前：接入页面")).toBeVisible();
    expect(screen.getByText("当前：检查交互")).toBeVisible();
    expect(within(card).getAllByRole("listitem")).toHaveLength(4);
    expect(screen.queryByText("历史计划不应成为正文")).not.toBeInTheDocument();
  });

  it("完成轮次默认折叠，展开后仍保留未完成计划项", async () => {
    const user = userEvent.setup();
    render(<TaskTurnContent turn={{ ...turn, status: "completed" }} />);
    const summary = screen.getByText("执行计划").closest("summary")!;
    const card = summary.closest("details")!;
    expect(card).not.toHaveAttribute("open");
    expect(screen.getByText("1 / 4 已完成")).toBeVisible();
    await user.click(summary);
    expect(screen.getByText("运行验证")).toBeVisible();
    expect(within(card).getAllByRole("img", { name: "已完成" })).toHaveLength(
      1,
    );
    expect(within(card).getByRole("img", { name: "待执行" })).toBeVisible();
  });

  it.each(["waiting_approval", "paused", "cancelled", "failed"] as const)(
    "%s 使用轮次真实状态，保留计划步骤状态",
    (status) => {
      render(<TaskTurnContent turn={{ ...turn, status }} />);
      const card = screen.getByText("执行计划").closest("details")!;
      expect(
        within(card).getByText(zh.projects.tasks.status[status]),
      ).toBeVisible();
      expect(within(card).getAllByRole("img", { name: "执行中" })).toHaveLength(
        2,
      );
      expect(within(card).getByText("1 / 4 已完成")).toBeVisible();
    },
  );

  it("进展和真实动作保持顺序，动作标题与状态来自宿主，思考可展开且正文独立", async () => {
    const user = userEvent.setup();
    render(
      <TaskTurnContent
        turn={{
          ...turn,
          plan: null,
          content_blocks: [
            block("progress-before", "progress", "先检查现有接口。"),
            block("reasoning", "reasoning_summary", "保留的思考详情。"),
            {
              ...block("tool", "tool", ""),
              tool_call_id: "call_a",
              tool_name: "read_file",
              tool_label: "读取文件",
              started_at: "2026-10-19T00:00:00Z",
            },
            block("progress-after", "progress", "接口已核对，开始接入。"),
            block("reply", "text", "已经完成接入。"),
          ],
        }}
      />,
    );
    const actions = screen.getByRole("region", { name: "动作进展" });
    const before = within(actions).getByText("先检查现有接口。");
    const tool = within(actions).getByText("读取文件");
    const after = within(actions).getByText("接口已核对，开始接入。");
    expect(
      before.compareDocumentPosition(tool) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      tool.compareDocumentPosition(after) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(within(actions).getByText("已完成")).toBeVisible();
    const reasoning = within(actions).getByText("思考摘要").closest("details")!;
    expect(reasoning).not.toHaveAttribute("open");
    await user.click(within(actions).getByText("思考摘要"));
    expect(screen.getByText("保留的思考详情。")).toBeVisible();
    expect(
      within(actions).queryByText("已经完成接入。"),
    ).not.toBeInTheDocument();
    expect(screen.getByText("已经完成接入。")).toBeVisible();
  });

  it("清空计划显示明确空态，没有计划的轮次不显示计划卡", () => {
    const { rerender } = render(
      <TaskTurnContent
        turn={{
          ...turn,
          plan: { ...plan, revision: 3, steps: [] },
          plan_revision: 3,
        }}
      />,
    );
    expect(screen.getByText("当前计划已清空。")).toBeVisible();
    expect(screen.getByText("0 / 0 已完成")).toBeVisible();
    rerender(
      <TaskTurnContent turn={{ ...turn, plan: null, plan_revision: 0 }} />,
    );
    expect(screen.queryByText("执行计划")).not.toBeInTheDocument();
  });
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
      content: "相同的步骤名称",
      status: "in_progress",
      result: "A 的阶段结果。",
      started_at: "2026-10-19T00:00:00Z",
      finished_at: null,
      removed_at: null,
    },
    {
      step_id: "step_b",
      content: "相同的步骤名称",
      status: "in_progress",
      result: "",
      started_at: "2026-10-19T00:00:01Z",
      finished_at: null,
      removed_at: null,
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
function stepElement(container: HTMLElement, id: string) {
  const element = container.querySelector<HTMLElement>(
    `[data-step-id="${id}"]`,
  );
  expect(element).not.toBeNull();
  return element!;
}

describe("按稳定步骤归属展开动作和结果", () => {
  it("并行同名步骤只关联明确的计划和步骤 ID，未关联记录留在整轮过程区", () => {
    const { container } = render(
      <TaskTurnContent
        turn={{
          ...turn,
          plan: scopedPlan,
          content_blocks: [
            scopedBlock("a-progress", "step_a", "progress", "A 的执行进展。"),
            scopedBlock("b-progress", "step_b", "progress", "B 的执行进展。"),
            scopedBlock("a-reasoning", "step_a", "reasoning", "A 的思考记录。"),
            {
              ...scopedBlock(
                "wrong-plan",
                "step_a",
                "progress",
                "其他计划的动作。",
              ),
              plan_id: "plan_other",
            },
            {
              ...block("unscoped", "progress", "没有步骤归属的动作。"),
              plan_id: "plan_a",
            },
            scopedBlock(
              "unknown-step",
              "step_unknown",
              "progress",
              "未知步骤的动作。",
            ),
            block("reply", "text", "最终答复。"),
          ],
        }}
      />,
    );
    const a = stepElement(container, "step_a");
    const b = stepElement(container, "step_b");
    expect(within(a).getByText("A 的执行进展。")).toBeVisible();
    expect(within(a).getByText("A 的阶段结果。")).toBeVisible();
    expect(within(a).getByText("A 的思考记录。")).toBeInTheDocument();
    expect(within(a).queryByText("B 的执行进展。")).not.toBeInTheDocument();
    expect(within(b).getByText("B 的执行进展。")).toBeVisible();
    const card = screen.getByText("接口接入计划").closest("details")!;
    for (const text of [
      "其他计划的动作。",
      "没有步骤归属的动作。",
      "未知步骤的动作。",
      "最终答复。",
    ]) {
      expect(within(card).queryByText(text)).not.toBeInTheDocument();
      expect(screen.getByText(text)).toBeVisible();
    }
  });

  it("步骤改名、改排和移除后，原动作仍留在原步骤，并保留阶段结果和耗时", async () => {
    const user = userEvent.setup();
    const blocks = [
      scopedBlock("action-a", "step_a", "progress", "最初属于 A 的动作。"),
      scopedBlock("action-b", "step_b", "progress", "最初属于 B 的动作。"),
    ];
    const { container, rerender } = render(
      <TaskTurnContent
        turn={{ ...turn, plan: scopedPlan, content_blocks: blocks }}
      />,
    );
    const a = stepElement(container, "step_a");
    rerender(
      <TaskTurnContent
        turn={{
          ...turn,
          plan: {
            ...scopedPlan,
            revision: 6,
            steps: [
              { ...scopedPlan.steps[1], content: "改排后的 B" },
              { ...scopedPlan.steps[0], content: "改名后的 A" },
            ],
          },
          content_blocks: blocks,
        }}
      />,
    );
    expect(stepElement(container, "step_a")).toBe(a);
    expect(within(a).getByText("改名后的 A")).toBeVisible();
    expect(within(a).getByText("最初属于 A 的动作。")).toBeVisible();
    expect(
      a.compareDocumentPosition(stepElement(container, "step_b")) &
        Node.DOCUMENT_POSITION_PRECEDING,
    ).toBeTruthy();
    rerender(
      <TaskTurnContent
        turn={{
          ...turn,
          plan: {
            ...scopedPlan,
            revision: 7,
            steps: [scopedPlan.steps[1]],
            retired_steps: [
              {
                ...scopedPlan.steps[0],
                content: "已移除的 A",
                status: "blocked",
                result: "A 已受阻并保留检查结果。",
                finished_at: "2026-10-19T00:00:02Z",
                removed_at: "2026-10-19T00:00:03Z",
              },
            ],
          },
          content_blocks: blocks,
        }}
      />,
    );
    const retired = stepElement(container, "step_a");
    expect(screen.getByText("已移除的步骤")).toBeVisible();
    expect(retired).not.toHaveAttribute("open");
    await user.click(within(retired).getByText("已移除的 A"));
    expect(within(retired).getByText("最初属于 A 的动作。")).toBeVisible();
    expect(within(retired).getByText("A 已受阻并保留检查结果。")).toBeVisible();
    expect(within(retired).getByText("2000 毫秒")).toBeVisible();
    expect(within(retired).getByText("移除")).toBeVisible();
    expect(
      within(retired).queryByText("最初属于 B 的动作。"),
    ).not.toBeInTheDocument();
  });

  it("历史计划没有 ID 时保持清单，不按相同文字或顺序关联动作", () => {
    const { container } = render(
      <TaskTurnContent
        turn={{
          ...turn,
          plan,
          content_blocks: [
            {
              ...block("legacy-action", "progress", "核对接口"),
              step_id: "step_a",
              plan_id: null,
            },
          ],
        }}
      />,
    );
    const card = screen.getByText("执行计划").closest("details")!;
    expect(container.querySelector("[data-step-id]")).toBeNull();
    expect(within(card).getByText("核对接口")).toBeInTheDocument();
    expect(within(card).queryByText("动作进展")).not.toBeInTheDocument();
    expect(screen.getByRole("region", { name: "动作进展" })).toHaveTextContent(
      "核对接口",
    );
  });

  it("工具可展开完整参数、返回内容及可信时间，使用服务端时区", async () => {
    const user = userEvent.setup();
    const output = [
      { type: "text", text: "长结果：" + "完整内容".repeat(500) },
      { type: "json", data: { success: true, rows: [1, 2, 3] } },
    ];
    const argumentsValue = {
      path: "src/project.ts",
      options: { include: ["all", "metadata"] },
    };
    render(
      <TaskTurnContent
        turn={{
          ...turn,
          plan: scopedPlan,
          content_blocks: [
            {
              ...scopedBlock("tool-a", "step_a", "tool", "读取完整文件。"),
              tool_label: "读取项目文件",
              tool_call_id: "call_a",
              tool_name: "read_file",
              started_at: "2026-10-19T00:00:00Z",
              finished_at: "2026-10-19T00:00:01.250Z",
              duration_ms: 1250,
              arguments: argumentsValue,
              output,
            },
          ],
        }}
      />,
    );
    const tool = screen.getByText("读取项目文件").closest("details")!;
    expect(tool).not.toHaveAttribute("open");
    await user.click(screen.getByText("读取项目文件"));
    expect(within(tool).getByLabelText("调用参数").textContent).toBe(
      JSON.stringify(argumentsValue, null, 2),
    );
    expect(within(tool).getByLabelText("返回内容").textContent).toBe(
      JSON.stringify(output, null, 2),
    );
    expect(within(tool).getByText("1250 毫秒")).toBeVisible();
    const start = tool.querySelector('time[datetime="2026-10-19T00:00:00Z"]')!;
    expect(start).toHaveTextContent(/8:00:00/);
    expect(within(tool).getByText("已完成")).toBeVisible();
  });

  it.each(["completed", "interrupted"] as const)(
    "缺少执行开始证据的 %s 工具不标成已经执行，不伪造时间",
    async (status) => {
      const user = userEvent.setup();
      render(
        <TaskTurnContent
          turn={{
            ...turn,
            plan: null,
            content_blocks: [
              {
                ...block("not-started", "tool", ""),
                status,
                tool_label: "尚未执行的动作",
                started_at: null,
                finished_at: null,
                duration_ms: null,
                arguments: { command: "check" },
                output: null,
              },
            ],
          }}
        />,
      );
      const tool = screen.getByText("尚未执行的动作").closest("details")!;
      expect(within(tool).getByText("未确认执行")).toBeVisible();
      expect(within(tool).queryByText("已完成")).not.toBeInTheDocument();
      await user.click(screen.getByText("尚未执行的动作"));
      expect(tool.querySelector("time")).toBeNull();
      expect(within(tool).queryByText("耗时")).not.toBeInTheDocument();
      expect(within(tool).getByText("暂无返回内容。")).toBeVisible();
    },
  );

  it("未执行的非法请求仍显示明确失败，展开返回内容解释原因，不编造执行时间", async () => {
    const user = userEvent.setup();
    render(
      <TaskTurnContent
        turn={{
          ...turn,
          plan: null,
          content_blocks: [
            {
              ...block("invalid-step", "tool", ""),
              status: "failed",
              tool_label: "执行请求被拒绝",
              tool_name: "execute",
              started_at: null,
              finished_at: null,
              duration_ms: null,
              arguments: { command: "check" },
              output: "步骤不存在，请求未执行。",
            },
          ],
        }}
      />,
    );
    const tool = screen.getByText("执行请求被拒绝").closest("details")!;
    expect(within(tool).getByText("执行失败")).toBeVisible();
    expect(within(tool).queryByText("未确认执行")).not.toBeInTheDocument();
    await user.click(screen.getByText("执行请求被拒绝"));
    expect(within(tool).getByLabelText("返回内容")).toHaveTextContent(
      "步骤不存在，请求未执行。",
    );
    expect(tool.querySelector("time")).toBeNull();
    expect(within(tool).queryByText("耗时")).not.toBeInTheDocument();
  });

  it.each(["client__shell", "ask_user_question"])(
    "%s 已有原生返回时显示完成，缺少执行时间仍保持未知",
    async (name) => {
      const user = userEvent.setup();
      render(
        <TaskTurnContent
          turn={{
            ...turn,
            plan: null,
            content_blocks: [
              {
                ...block("native-result", "tool", ""),
                tool_name: name,
                tool_label: "收到原生工具返回",
                started_at: null,
                finished_at: null,
                duration_ms: null,
                output: [{ type: "text", text: "原生工具执行结果。" }],
              },
            ],
          }}
        />,
      );
      const tool = screen.getByText("收到原生工具返回").closest("details")!;
      expect(within(tool).getByText("已完成")).toBeVisible();
      expect(within(tool).queryByText("未确认执行")).not.toBeInTheDocument();
      await user.click(screen.getByText("收到原生工具返回"));
      expect(within(tool).getByLabelText("返回内容")).toHaveTextContent(
        "原生工具执行结果。",
      );
      expect(tool.querySelector("time")).toBeNull();
      expect(within(tool).queryByText("耗时")).not.toBeInTheDocument();
    },
  );

  it("待审批动作保持真实等待状态，受阻和跳过步骤不会自动成为已完成", () => {
    render(
      <TaskTurnContent
        turn={{
          ...turn,
          status: "waiting_approval",
          plan: {
            ...scopedPlan,
            steps: [
              { ...scopedPlan.steps[0], status: "blocked" },
              { ...scopedPlan.steps[1], status: "skipped" },
            ],
          },
          content_blocks: [
            {
              ...block("approval-tool", "tool", ""),
              tool_label: "等待审批的动作",
              started_at: null,
              status: "streaming",
            },
            {
              ...block("parallel-tool", "tool", ""),
              tool_label: "已经开始的并行动作",
              started_at: "2026-10-19T00:00:00Z",
              status: "streaming",
            },
          ],
        }}
      />,
    );
    expect(screen.getByRole("img", { name: "受阻" })).toBeVisible();
    expect(screen.getByRole("img", { name: "已跳过" })).toBeVisible();
    expect(screen.getByText("0 / 2 已完成")).toBeVisible();
    const tool = screen.getByText("等待审批的动作").closest("details")!;
    expect(within(tool).getByText("待审批")).toBeVisible();
    expect(within(tool).queryByText("执行中")).not.toBeInTheDocument();
    const running = screen.getByText("已经开始的并行动作").closest("details")!;
    expect(within(running).getByText("执行中")).toBeVisible();
    expect(within(running).queryByText("待审批")).not.toBeInTheDocument();
  });
});
