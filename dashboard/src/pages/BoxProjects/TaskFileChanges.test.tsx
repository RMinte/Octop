import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";
import type { TaskFileChange } from "../../api/modules/projects";
import zh from "../../locales/zh.json";
import TaskFileChanges from "./TaskFileChanges";

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

const change = (
  path: string,
  additions: number | null,
  deletions: number | null,
  diff: string | null = null,
): TaskFileChange => ({ path, additions, deletions, diff });

const getComputedStyle = window.getComputedStyle.bind(window);
let computedStyleSpy: ReturnType<typeof vi.spyOn>;
beforeAll(() => {
  computedStyleSpy = vi
    .spyOn(window, "getComputedStyle")
    .mockImplementation((element) => getComputedStyle(element));
});
afterAll(() => computedStyleSpy.mockRestore());

describe("任务文件变更", () => {
  it("没有文件变更时不显示卡片", () => {
    const { container } = render(<TaskFileChanges changes={[]} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("按路径汇总收据，并默认显示三个文件且支持展开和收起", async () => {
    const user = userEvent.setup();
    render(
      <TaskFileChanges
        changes={[
          change("src/a.ts", 2, 1),
          change("src/b.ts", 1, 0),
          change("src/a.ts", 3, 4),
          change("src/c.ts", 0, 2),
          change("src/d.ts", 6, 0),
        ]}
      />,
    );
    const card = screen.getByRole("region", { name: "文件变更" });
    expect(within(card).getByText("已编辑 4 个文件")).toBeInTheDocument();
    expect(within(card).getByLabelText("新增 12 行")).toHaveTextContent("+12");
    expect(within(card).getByLabelText("删除 7 行")).toHaveTextContent("−7");
    const first = within(card).getByRole("button", { name: /^src\/a\.ts/ });
    expect(within(first).getByLabelText("新增 5 行")).toHaveTextContent("+5");
    expect(within(first).getByLabelText("删除 5 行")).toHaveTextContent("−5");
    expect(
      within(card).queryByRole("button", { name: /^src\/d\.ts/ }),
    ).toBeNull();
    const expand = within(card).getByRole("button", {
      name: "再显示 1 个文件",
    });
    expect(expand).toHaveAttribute("aria-expanded", "false");
    await user.click(expand);
    expect(
      within(card).getByRole("button", { name: /^src\/d\.ts/ }),
    ).toBeInTheDocument();
    const collapse = within(card).getByRole("button", { name: "收起文件列表" });
    expect(collapse).toHaveAttribute("aria-expanded", "true");
    await user.click(collapse);
    expect(
      within(card).queryByRole("button", { name: /^src\/d\.ts/ }),
    ).toBeNull();
  });

  it("未知行数保留未知，并在点击文件后显示各次差异及缺失快照说明", async () => {
    const user = userEvent.setup();
    render(
      <TaskFileChanges
        changes={[
          change(
            "src/a.ts",
            2,
            1,
            "--- a/src/a.ts\n+++ b/src/a.ts\n@@ -1 +1 @@\n-old\n+first",
          ),
          change("src/a.ts", null, null),
          change(
            "src/a.ts",
            1,
            1,
            "--- a/src/a.ts\n+++ b/src/a.ts\n@@ -1 +1 @@\n-first\n+last",
          ),
        ]}
      />,
    );
    const card = screen.getByRole("region", { name: "文件变更" });
    expect(within(card).getByText("部分行数未记录")).toBeInTheDocument();
    const file = within(card).getByRole("button", { name: /^src\/a\.ts/ });
    expect(within(file).getByLabelText("新增行数未记录")).toHaveTextContent(
      "+?",
    );
    expect(within(file).getByLabelText("删除行数未记录")).toHaveTextContent(
      "−?",
    );
    expect(within(file).queryByText("+0")).toBeNull();
    await user.click(file);
    const drawer = screen.getByRole("dialog", { name: "审核文件变更" });
    expect(within(drawer).getByText("只读差异")).toBeInTheDocument();
    expect(within(drawer).queryByRole("textbox")).toBeNull();
    expect(
      within(drawer).getByText(
        "部分操作未记录完整文件快照，无法展示全部差异。",
      ),
    ).toBeInTheDocument();
    const diff = within(drawer).getByLabelText("「src/a.ts」的差异");
    expect(diff.textContent).toContain("-old\n+first");
    expect(diff.textContent).toContain("-first\n+last");
    expect(diff.querySelector('[data-kind="added"]')).toHaveTextContent(
      "+first",
    );
    expect(diff.querySelector('[data-kind="removed"]')).toHaveTextContent(
      "-old",
    );
  });

  it("审核按钮打开首个文件，支持切换文件并明确提示暂无差异", async () => {
    const user = userEvent.setup();
    render(
      <TaskFileChanges
        changes={[
          change(
            "src/a.ts",
            1,
            0,
            "--- /dev/null\n+++ b/src/a.ts\n@@ -0,0 +1 @@\n+hello",
          ),
          change("src/b.ts", null, null),
        ]}
      />,
    );
    await user.click(screen.getByRole("button", { name: /审\s*核/ }));
    const drawer = screen.getByRole("dialog", { name: "审核文件变更" });
    expect(
      within(drawer).getByLabelText("「src/a.ts」的差异"),
    ).toBeInTheDocument();
    await user.click(within(drawer).getByRole("button", { name: "src/b.ts" }));
    expect(within(drawer).queryByLabelText("「src/a.ts」的差异")).toBeNull();
    expect(
      within(drawer).getByRole("button", { name: "src/b.ts" }),
    ).toHaveAttribute("aria-current", "true");
    expect(within(drawer).getByText("暂无可展示的差异。")).toBeInTheDocument();
    expect(
      within(drawer).getByText(
        "部分操作未记录完整文件快照，无法展示全部差异。",
      ),
    ).toBeInTheDocument();
  });
});
