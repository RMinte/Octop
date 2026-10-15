import type { ReactNode } from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import zh from "../../locales/zh.json";

vi.mock("../../api/request", () => ({ request: vi.fn() }));
vi.mock("../Agent/Skills/components/skillHubCache", () => ({
  loadRankingsCache: () => null,
  saveRankingsCache: vi.fn(),
}));
vi.mock("../../hooks/useServerTimezone", () => ({
  useServerTimezone: () => "Asia/Shanghai",
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
vi.mock("../../layouts/PageShell", () => ({
  default: ({
    children,
    actions,
    title,
  }: {
    children: ReactNode;
    actions: ReactNode;
    title: string;
  }) => (
    <div>
      <h1>{title}</h1>
      {actions}
      {children}
    </div>
  ),
}));
vi.mock("../Experts/components/RootDirSelect", () => ({
  default: ({
    value,
    onChange,
  }: {
    value?: string;
    onChange?: (value: string) => void;
  }) => (
    <input
      aria-label="目录"
      value={value ?? ""}
      onChange={(event) => onChange?.(event.target.value)}
    />
  ),
}));
vi.mock("../../utils/antdMessage", () => ({ message: { success: vi.fn() } }));

import { request } from "../../api/request";
import BoxProjectsPage from "./index";
import type { Project } from "../../api/modules/projects";

const api = vi.mocked(request, true);
const t0 = "2026-10-15T00:00:00Z";
const initial: Project = {
  project_id: "proj_a",
  name: "我的项目",
  description: "简介",
  directory: null,
  client_directory: null,
  instructions: "指南",
  skills: [],
  mcp_servers: [],
  revision: 1,
  created_at: t0,
  updated_at: t0,
};
let rows: Project[];

beforeEach(() => {
  vi.clearAllMocks();
  rows = [{ ...initial }];
  api.mockImplementation(async (url, init) => {
    if (url.startsWith("/skills/hub/rankings"))
      return {
        rankings: Object.fromEntries(
          ["recommended", "trending", "hot", "newest"].map((key) => [
            key,
            {
              skills: [
                {
                  slug: "parenting-expert",
                  name: "育儿大师.Skill",
                  description: "科学育儿",
                  version: "1.0",
                },
              ],
            },
          ]),
        ),
      } as never;
    if (url.startsWith("/skills/hub/search"))
      return [
        {
          slug: "parenting-expert",
          name: "育儿大师.Skill",
          description: "科学育儿",
        },
      ] as never;
    if (url.startsWith("/projects/sync"))
      return {
        mode: "snapshot",
        base_sync_cursor: null,
        next_sync_cursor: "projects_next",
        next_page_cursor: null,
        changes: rows.map((data) => ({
          operation: "upsert",
          project_id: data.project_id,
          data,
        })),
      } as never;
    if (url.includes("/tasks/sync") || url.startsWith("/project-tasks/sync"))
      return {
        base_sync_cursor: "tasks_first",
        next_sync_cursor: "tasks_next",
        next_page_cursor: null,
        changes: [],
      } as never;
    if (url.endsWith("/tasks") || url === "/project-tasks")
      return {
        items: [],
        next_page_cursor: null,
        sync_cursor: "tasks_first",
      } as never;
    if (url === "/projects/resources")
      return { skills: [], mcp_servers: [], directory_roots: [] } as never;
    if (url === "/projects" && init?.method === "POST") {
      const fields = JSON.parse(init.body as string);
      const item = { ...initial, ...fields, project_id: "proj_new" };
      rows.push(item);
      return item as never;
    }
    if (url === "/projects") return [...rows] as never;
    const id = url.split("/").pop();
    if (init?.method === "PATCH") {
      const { expected_revision, ...fields } = JSON.parse(init.body as string);
      const index = rows.findIndex((item) => item.project_id === id);
      rows[index] = {
        ...rows[index],
        ...fields,
        revision: expected_revision + 1,
      };
      return rows[index] as never;
    }
    return rows.find((item) => item.project_id === id) as never;
  });
});

function mount(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/box-projects/:projectID?" element={<BoxProjectsPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("独立项目正式页面", () => {
  it("uses the renamed title and reads its own project service", async () => {
    mount("/box-projects/proj_a");
    expect(
      await screen.findByRole("heading", { name: "我的项目" }),
    ).toBeTruthy();
    expect(screen.getByRole("heading", { name: "项目和任务" })).toBeTruthy();
    expect(
      api.mock.calls.some(
        ([url]) => url.includes("agentcomputer") || url.includes("bridge"),
      ),
    ).toBe(false);
  });

  it("creates a project and updates the list through synchronization", async () => {
    const user = userEvent.setup();
    mount("/box-projects");
    await screen.findByRole("button", { name: /我的项目/ });
    await user.click(screen.getAllByRole("button", { name: "新建项目" })[0]);
    await user.type(screen.getByLabelText("项目名称"), "新项目");
    await user.click(screen.getByRole("button", { name: "保存项目" }));
    await screen.findByRole("heading", { name: "新项目" });
    const sent = api.mock.calls.find(
      ([url, init]) => url === "/projects" && init?.method === "POST",
    );
    expect(JSON.parse(sent![1]!.body as string)).toMatchObject({
      name: "新项目",
      skills: [],
      mcp_servers: [],
      directory: null,
      client_directory: null,
    });
    expect(
      (sent![1]!.headers as Record<string, string>)["Idempotency-Key"],
    ).toBeTruthy();
    expect(
      api.mock.calls.filter(([url]) => url.startsWith("/projects/sync")).length,
    ).toBeGreaterThan(1);
    expect(api.mock.calls.some(([url]) => url === "/projects/proj_new")).toBe(
      false,
    );
  });

  it("reuses the creation key after a lost response", async () => {
    const user = userEvent.setup();
    const normal = api.getMockImplementation()!;
    let lost = true;
    api.mockImplementation(async (url, init) => {
      if (url === "/projects" && init?.method === "POST" && lost) {
        lost = false;
        throw new Error("response lost");
      }
      return normal(url, init);
    });
    mount("/box-projects");
    await screen.findByRole("button", { name: /我的项目/ });
    await user.click(screen.getAllByRole("button", { name: "新建项目" })[0]);
    await user.type(screen.getByLabelText("项目名称"), "重试项目");
    await user.click(screen.getByRole("button", { name: "保存项目" }));
    await screen.findByText("response lost");
    await user.click(screen.getByRole("button", { name: "保存项目" }));
    await screen.findByRole("heading", { name: "重试项目" });
    const sends = api.mock.calls.filter(
      ([url, init]) => url === "/projects" && init?.method === "POST",
    );
    expect(sends).toHaveLength(2);
    expect(sends[1][1]!.headers).toEqual(sends[0][1]!.headers);
    expect(sends[1][1]!.body).toEqual(sends[0][1]!.body);
  });

  it("edits with the loaded revision and synchronizes the saved project", async () => {
    const user = userEvent.setup();
    mount("/box-projects/proj_a");
    await user.click(await screen.findByRole("button", { name: "修改项目" }));
    const field = screen.getByLabelText("项目名称");
    await user.clear(field);
    await user.type(field, "项目已修改");
    await user.click(screen.getByRole("button", { name: "保存项目" }));
    await screen.findByRole("heading", { name: "项目已修改" });
    await waitFor(() =>
      expect(
        api.mock.calls.filter(([url]) => url.startsWith("/projects/sync"))
          .length,
      ).toBeGreaterThan(1),
    );
    const sent = api.mock.calls.find(
      ([url, init]) => url === "/projects/proj_a" && init?.method === "PATCH",
    );
    expect(JSON.parse(sent![1]!.body as string).expected_revision).toBe(1);
  });
  it("searches SkillHub from an empty installed library and saves the selected skill", async () => {
    const user = userEvent.setup();
    mount("/box-projects/proj_a");
    await user.click(await screen.findByRole("button", { name: "修改项目" }));
    await user.click(screen.getByRole("tab", { name: "Skill / MCP" }));
    await user.click(screen.getByRole("button", { name: "添加技能" }));
    for (const label of ["为你推荐", "近期飙升", "下载热榜", "最近上新"])
      expect(await screen.findByText(label)).toBeTruthy();
    await user.type(
      screen.getByPlaceholderText("搜索技能名称、描述或分类..."),
      "育",
    );
    await user.click(await screen.findByText("育儿大师.Skill"));
    await user.click(
      screen.getAllByRole("button", { name: "选择", exact: true }).at(-1)!,
    );
    await user.click(screen.getByRole("button", { name: "完成选择" }));
    await user.click(screen.getByRole("button", { name: "保存项目" }));
    await screen.findAllByText("育儿大师.Skill");
    const sent = api.mock.calls.find(
      ([url, init]) => url === "/projects/proj_a" && init?.method === "PATCH",
    );
    expect(JSON.parse(sent![1]!.body as string).skills).toEqual([
      {
        source: "skillhub",
        slug: "parenting-expert",
        name: "育儿大师.Skill",
        description: "科学育儿",
      },
    ]);
    expect(
      api.mock.calls.some(([url]) =>
        url.includes("/skills/hub/search?q=%E8%82%B2"),
      ),
    ).toBe(true);
    await user.click(screen.getByRole("button", { name: "修改项目" }));
    await user.click(screen.getByRole("tab", { name: "Skill / MCP" }));
    expect(screen.getAllByText("育儿大师.Skill").length).toBeGreaterThan(0);
  });
  it("can remove a previously selected SkillHub skill", async () => {
    rows[0].skills = [
      {
        source: "skillhub",
        slug: "parenting-expert",
        name: "育儿大师.Skill",
        description: "科学育儿",
      },
    ];
    const user = userEvent.setup();
    mount("/box-projects/proj_a");
    await user.click(await screen.findByRole("button", { name: "修改项目" }));
    await user.click(screen.getByRole("tab", { name: "Skill / MCP" }));
    await user.click(screen.getByRole("button", { name: /移除技能/ }));
    await user.click(screen.getByRole("button", { name: "保存项目" }));
    await waitFor(() => expect(rows[0].skills).toEqual([]));
  });
  it("saves both directory locations and can clear only the client path", async () => {
    const user = userEvent.setup();
    mount("/box-projects");
    await screen.findByRole("button", { name: /我的项目/ });
    await user.click(screen.getAllByRole("button", { name: "新建项目" })[0]);
    await user.type(screen.getByLabelText("项目名称"), "两个目录");
    await user.type(screen.getByLabelText("目录"), "/server/project");
    await user.type(
      screen.getByLabelText("客户端工作目录"),
      "C:\\Users\\alice\\project",
    );
    await user.click(screen.getByRole("button", { name: "保存项目" }));
    await screen.findByRole("heading", { name: "两个目录" });
    const created = api.mock.calls.find(
      ([url, init]) => url === "/projects" && init?.method === "POST",
    );
    expect(JSON.parse(created![1]!.body as string)).toMatchObject({
      directory: "/server/project",
      client_directory: "C:\\Users\\alice\\project",
    });
    await user.click(screen.getByRole("button", { name: "修改项目" }));
    const field = screen.getByLabelText("客户端工作目录") as HTMLInputElement;
    expect(field.value).toBe("C:\\Users\\alice\\project");
    await user.clear(field);
    await user.click(screen.getByRole("button", { name: "保存项目" }));
    await waitFor(() =>
      expect(
        rows.find((item) => item.project_id === "proj_new")?.client_directory,
      ).toBeNull(),
    );
    expect(rows.find((item) => item.project_id === "proj_new")?.directory).toBe(
      "/server/project",
    );
  });
});
