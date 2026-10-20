import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  knowledgeBasesApi,
  type KnowledgeDocument,
} from "../../api/modules/knowledgeBases";
import type { TaskTurn } from "../../api/modules/projects";
import zh from "../../locales/zh.json";
import TaskTurnContent from "./TaskTurnContent";

vi.mock("../../api/modules/knowledgeBases", () => ({
  knowledgeBasesApi: {
    listDocuments: vi.fn(),
    getTextDocument: vi.fn(),
    previewDocument: vi.fn(),
    fetchDocumentFile: vi.fn(),
  },
}));
vi.mock("../../hooks/useServerTimezone", () => ({
  useServerTimezone: () => "Asia/Shanghai",
}));
vi.mock("../../components/Markdown/LazyMarkdown", () => ({
  default: ({ content }: { content: string }) => <div>{content}</div>,
}));
vi.mock("../../components/DocumentPreviewCore", async () => {
  const { useEffect } = await import("react");
  return {
    default: function Preview({
      fetchBlob,
    }: {
      fetchBlob: () => Promise<Blob>;
    }) {
      useEffect(() => {
        void fetchBlob().catch(() => undefined);
      }, [fetchBlob]);
      return <div>原文件预览</div>;
    },
  };
});
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

const api = vi.mocked(knowledgeBasesApi);
const computedStyle = window.getComputedStyle.bind(window);
const citation = {
  kb_id: "kb_shared",
  kb_name: "共享知识",
  doc_id: "doc_guide",
  filename: "操作指南.md",
  path: "手册/操作指南.md",
};
const marker = (source = citation) =>
  `<!--octop-kb-citations:${JSON.stringify([source])}-->`;
const document = {
  id: "doc_guide",
  kb_id: "kb_shared",
  filename: "操作指南.md",
  path: "手册/操作指南.md",
  content_type: "text/markdown",
  has_original: true,
} as KnowledgeDocument;
const tool = {
  block_id: "search-result",
  type: "tool" as const,
  status: "completed" as const,
  text: "",
  tool_call_id: "search_a",
  tool_name: "search_knowledge",
  output: `真实检索结果\n${marker()}`,
};
const turn: TaskTurn = {
  sequence: 1,
  turn_id: "turn_knowledge",
  request_id: "request_knowledge",
  prompt: "查阅知识库",
  attachments: [],
  reply: "依据来源答复。",
  reasoning: "",
  content_blocks: [tool],
  content_revision: 1,
  plan: null,
  plan_revision: 0,
  status: "completed",
  error_code: "",
  activity: "",
  artifacts: [],
  model_ref: "test/model",
  options: { knowledge_base_ids: ["kb_shared"] },
  web_search_enabled: false,
  project_revision: null,
  approval_policy_at_start: {
    mode: "ask_for_approval",
    revision: 1,
    updated_at: "2026-10-20T00:00:00Z",
  },
  retry_of_turn_id: null,
  cancel_requested: false,
  pause_requested: false,
  interactions: [],
  interaction_revision: 0,
  created_at: "2026-10-20T00:00:00Z",
  updated_at: "2026-10-20T00:00:00Z",
};

beforeEach(() => {
  vi.clearAllMocks();
  vi.spyOn(window, "getComputedStyle").mockImplementation((element) =>
    computedStyle(element),
  );
  api.listDocuments.mockResolvedValue([document]);
  api.getTextDocument.mockResolvedValue({
    id: document.id,
    filename: document.filename,
    content_type: document.content_type,
    text: "# 来源正文",
  });
  api.previewDocument.mockResolvedValue({
    id: document.id,
    filename: document.filename,
    text: "提取的来源正文",
  });
  api.fetchDocumentFile.mockResolvedValue(new Blob(["original"]));
  URL.createObjectURL = vi.fn(() => "blob:knowledge-original");
  URL.revokeObjectURL = vi.fn();
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(
    () => undefined,
  );
});
afterEach(() => vi.restoreAllMocks());

describe("任务持久化知识来源", () => {
  it("重开后从真实工具结果恢复去重来源，回答标记不编造来源，可鉴权预览和下载", async () => {
    const user = userEvent.setup();
    const saved = JSON.parse(
      JSON.stringify({
        ...turn,
        content_blocks: [
          tool,
          { ...tool, block_id: "repeated-result" },
          {
            block_id: "answer",
            type: "text",
            status: "completed",
            text: `最终答复。\n${marker({
              ...citation,
              doc_id: "fake_doc",
              filename: "模型编造来源.md",
            })}`,
            tool_call_id: null,
            tool_name: null,
          },
        ],
      }),
    ) as TaskTurn;
    const first = render(<TaskTurnContent turn={saved} />);
    expect(
      screen.getAllByRole("button", { name: "操作指南.md · 共享知识" }),
    ).toHaveLength(1);
    expect(screen.queryByText(/octop-kb-citations/)).not.toBeInTheDocument();
    expect(screen.getByText("最终答复。")).toBeVisible();
    expect(
      screen.queryByRole("button", { name: /模型编造来源/ }),
    ).not.toBeInTheDocument();
    expect(api.listDocuments).not.toHaveBeenCalled();
    first.unmount();
    render(<TaskTurnContent turn={JSON.parse(JSON.stringify(saved))} />);
    await user.click(
      screen.getByRole("button", { name: "操作指南.md · 共享知识" }),
    );
    expect(await screen.findByText("# 来源正文")).toBeVisible();
    expect(api.listDocuments).toHaveBeenCalledWith("kb_shared");
    expect(api.getTextDocument).toHaveBeenCalledWith("kb_shared", "doc_guide");
    await user.click(screen.getByRole("button", { name: "下载原文件" }));
    await waitFor(() =>
      expect(api.fetchDocumentFile).toHaveBeenCalledWith(
        "kb_shared",
        "doc_guide",
        "attachment",
        undefined,
        expect.any(AbortSignal),
      ),
    );
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:knowledge-original");
  });

  it("原文件格式复用文档预览，通过知识 API 读取鉴权文件", async () => {
    const user = userEvent.setup();
    api.listDocuments.mockResolvedValue([
      { ...document, filename: "report.pdf", content_type: "application/pdf" },
    ]);
    render(<TaskTurnContent turn={turn} />);
    await user.click(
      screen.getByRole("button", { name: "操作指南.md · 共享知识" }),
    );
    expect(await screen.findByText("原文件预览")).toBeVisible();
    expect(api.fetchDocumentFile).toHaveBeenCalledWith(
      "kb_shared",
      "doc_guide",
      "inline",
      undefined,
      undefined,
    );
    expect(api.getTextDocument).not.toHaveBeenCalled();
  });

  it.each(["missing", "forbidden"])(
    "来源%s时提示失效或权限变化，不展示旧内容或下载",
    async (failure) => {
      const user = userEvent.setup();
      if (failure === "missing") api.listDocuments.mockResolvedValue([]);
      else api.getTextDocument.mockRejectedValue(new Error("403 Forbidden"));
      render(<TaskTurnContent turn={turn} />);
      await user.click(
        screen.getByRole("button", { name: "操作指南.md · 共享知识" }),
      );
      expect(
        await screen.findByText("来源已失效或你已无权访问。"),
      ).toBeVisible();
      expect(screen.queryByText("# 来源正文")).not.toBeInTheDocument();
      expect(
        screen.queryByRole("button", { name: "下载原文件" }),
      ).not.toBeInTheDocument();
    },
  );

  it("不完整来源保留标签但禁用预览，其他工具和普通答复不会形成知识来源", () => {
    render(
      <TaskTurnContent
        turn={{
          ...turn,
          content_blocks: [
            { ...tool, output: marker({ ...citation, kb_id: "" }) },
            { ...tool, block_id: "different-tool", tool_name: "read_file" },
          ],
        }}
      />,
    );
    expect(
      screen.getByRole("button", { name: "操作指南.md · 共享知识" }),
    ).toBeDisabled();
    expect(api.listDocuments).not.toHaveBeenCalled();
  });

  it("普通历史答复中的引用标记只去除，不作为检索来源", () => {
    render(
      <TaskTurnContent
        turn={{ ...turn, content_blocks: [], reply: `历史答复。\n${marker()}` }}
      />,
    );
    expect(screen.getByText("历史答复。")).toBeVisible();
    expect(screen.queryByText(/octop-kb-citations/)).not.toBeInTheDocument();
    expect(
      screen.queryByRole("region", { name: "知识来源" }),
    ).not.toBeInTheDocument();
    expect(api.listDocuments).not.toHaveBeenCalled();
  });

  it("预览后下载权限变化时保留正文并展示下载错误", async () => {
    const user = userEvent.setup();
    api.fetchDocumentFile.mockRejectedValue(new Error("403 Forbidden"));
    render(<TaskTurnContent turn={turn} />);
    await user.click(
      screen.getByRole("button", { name: "操作指南.md · 共享知识" }),
    );
    expect(await screen.findByText("# 来源正文")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "下载原文件" }));
    expect(
      await screen.findByText("下载来源失败，请确认文件仍存在且有权访问。"),
    ).toBeVisible();
    expect(screen.getByText("# 来源正文")).toBeVisible();
    expect(URL.createObjectURL).not.toHaveBeenCalled();
  });

  it.each(["关闭来源抽屉", "切换任务"])(
    "%s取消下载，迟到文件不得触发旧下载",
    async (action) => {
      const user = userEvent.setup();
      let finish!: (blob: Blob) => void;
      api.fetchDocumentFile.mockReturnValue(
        new Promise<Blob>((resolve) => {
          finish = resolve;
        }),
      );
      const view = render(<TaskTurnContent key="task-old" turn={turn} />);
      await user.click(
        screen.getByRole("button", { name: "操作指南.md · 共享知识" }),
      );
      expect(await screen.findByText("# 来源正文")).toBeVisible();
      await user.click(screen.getByRole("button", { name: "下载原文件" }));
      const signal = api.fetchDocumentFile.mock.calls[0]?.[4];
      expect(signal).toBeInstanceOf(AbortSignal);
      expect(signal?.aborted).toBe(false);

      if (action === "关闭来源抽屉") {
        await user.click(screen.getByRole("button", { name: "Close" }));
      } else {
        view.rerender(
          <TaskTurnContent
            key="task-new"
            turn={{ ...turn, turn_id: "turn_new_task" }}
          />,
        );
      }
      expect(signal?.aborted).toBe(true);
      await act(async () => {
        finish(new Blob(["late original"]));
      });
      expect(URL.createObjectURL).not.toHaveBeenCalled();
      expect(HTMLAnchorElement.prototype.click).not.toHaveBeenCalled();
      expect(
        screen.queryByText("下载来源失败，请确认文件仍存在且有权访问。"),
      ).not.toBeInTheDocument();
    },
  );
});
