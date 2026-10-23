import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { useCallback, useLayoutEffect, useRef, useState } from "react";
import { beforeEach, expect, it, vi } from "vitest";
import type {
  Project,
  TaskExecutionOptions,
  TaskResources,
} from "../../api/modules/projects";
import TaskComposerResources, {
  TaskComposerResourceTriggers,
} from "./TaskComposerResources";
import {
  composerResourceQuery,
  useTaskComposerResources,
} from "./useTaskComposerResources";

const api = vi.hoisted(() => ({
  scope: vi.fn(),
  tree: vi.fn(),
  projectTree: vi.fn(),
}));
vi.mock("../../context/TaskHostContext", () => {
  const host = {
    hostID: "peer",
    taskWorkbenchApi: { scope: api.scope, tree: api.tree },
    projectsApi: { workspaceTree: api.projectTree },
  };
  return { useTaskHost: () => host };
});
vi.mock("react-i18next", () => {
  const t = (key: string) => key;
  return { useTranslation: () => ({ t }) };
});

const project = {
  project_id: "project",
  revision: 3,
  directory: "/opaque-server-directory",
  skills: [
    {
      source: "skillhub",
      slug: "project-skill",
      name: "Project skill",
      description: "",
    },
  ],
} as Project;
const resources = {
  skills: [
    {
      package_id: "package",
      slug: "new-skill",
      name: "New skill",
      description: "",
    },
  ],
  experts: [
    {
      expert_id: "expert",
      default_skills: [{ package_id: "expert-package", slug: "expert-skill" }],
    },
  ],
} as TaskResources;

function Composer({
  taskID = null,
  currentProject,
  catalog = resources,
  initial = "",
  options = {},
  onSend,
}: {
  taskID?: string | null;
  currentProject?: Project;
  catalog?: TaskResources | null;
  initial?: string;
  options?: TaskExecutionOptions;
  onSend?: () => void;
}) {
  const input = useRef<HTMLTextAreaElement>(null);
  const getInputElement = useCallback(() => input.current, []);
  const [text, setText] = useState(initial);
  const [value, setValue] = useState(options);
  useLayoutEffect(() => {
    input.current?.setSelectionRange(initial.length, initial.length);
  }, [initial]);
  const controller = useTaskComposerResources({
    taskID,
    project: currentProject,
    selectedExpertID: "expert",
    text,
    onTextChange: setText,
    value,
    onChange: setValue,
    resources: catalog,
    disabled: false,
    getInputElement,
  });
  return (
    <>
      <TaskComposerResourceTriggers controller={controller} />
      <TaskComposerResources controller={controller} />
      <textarea
        aria-label="draft"
        ref={input}
        value={text}
        onChange={(event) => setText(event.target.value)}
        onSelect={controller.onCursorChange}
        onKeyDown={(event) => {
          if (controller.onKeyDown(event)) return;
          if (event.key === "Enter") onSend?.();
        }}
      />
      <output data-testid="options">{JSON.stringify(value)}</output>
    </>
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  api.scope.mockResolvedValue({ scope_revision: "scope-2" });
  api.tree.mockResolvedValue([]);
  api.projectTree.mockResolvedValue([]);
});

it("recognizes cursor-local file and Skill tokens without treating URLs as commands", () => {
  expect(composerResourceQuery("read @docs/read", 15)).toMatchObject({
    kind: "file",
    text: "docs/read",
  });
  expect(composerResourceQuery('read @"docs/my file', 19)).toMatchObject({
    kind: "file",
    quoted: true,
  });
  expect(composerResourceQuery("try /new", 8)).toMatchObject({
    kind: "skill",
    text: "new",
  });
  expect(composerResourceQuery("https://host/new", 16)).toBeNull();
});

it("clearly disables file search for a standalone unsent draft and creates nothing", async () => {
  render(<Composer initial="@read" />);
  expect(
    await screen.findByText("projects.composerResources.filesUnavailableDraft"),
  ).toBeVisible();
  expect(
    screen.getByRole("button", { name: "projects.composerResources.files" }),
  ).toBeDisabled();
  expect(api.scope).not.toHaveBeenCalled();
  expect(api.tree).not.toHaveBeenCalled();
  expect(api.projectTree).not.toHaveBeenCalled();
});

it("searches the selected project's server scope before first send and browses directories", async () => {
  api.projectTree.mockImplementation((_id, directory) =>
    Promise.resolve(
      directory === "."
        ? [{ name: "docs", path: "docs", is_dir: true }]
        : [{ name: "guide.md", path: "docs/guide.md", is_dir: false }],
    ),
  );
  render(<Composer currentProject={project} initial="@do" />);
  fireEvent.click(await screen.findByRole("option", { name: /docs/ }));
  fireEvent.click(await screen.findByRole("option", { name: /guide.md/ }));
  await waitFor(() =>
    expect(screen.getByLabelText("draft")).toHaveValue("@docs/guide.md "),
  );
  expect(api.projectTree).toHaveBeenCalledWith(
    "project",
    "docs",
    3,
    expect.any(AbortSignal),
  );
  expect(api.scope).not.toHaveBeenCalled();
});

it("uses the current Task's authoritative scope and keeps quoted file references", async () => {
  api.tree.mockResolvedValue([
    { name: "my notes.md", path: "my notes.md", is_dir: false },
    { name: "private", path: ".octop/private", is_dir: false },
  ]);
  render(<Composer taskID="task" initial="@my" />);
  fireEvent.click(await screen.findByRole("option", { name: /my notes/ }));
  await waitFor(() =>
    expect(screen.getByLabelText("draft")).toHaveValue('@"my notes.md" '),
  );
  expect(api.scope).toHaveBeenCalledWith("task", expect.any(AbortSignal));
  expect(api.tree).toHaveBeenCalledWith(
    "task",
    ".",
    "scope-2",
    expect.any(AbortSignal),
  );
  expect(api.projectTree).not.toHaveBeenCalled();
});

it("does not request traversal or system directories", async () => {
  const view = render(<Composer taskID="task" initial="@../" />);
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 250));
  });
  expect(api.scope).not.toHaveBeenCalled();
  fireEvent.change(screen.getByLabelText("draft"), {
    target: { value: "@.octop/" },
  });
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 250));
  });
  expect(api.scope).not.toHaveBeenCalled();
  view.unmount();
});

it("selects a real Skill binding and preserves merged expert and project defaults", async () => {
  render(<Composer currentProject={project} initial="/new" />);
  fireEvent.keyDown(screen.getByLabelText("draft"), { key: "Enter" });
  await waitFor(() =>
    expect(screen.getByLabelText("draft")).toHaveValue("/new-skill "),
  );
  expect(JSON.parse(screen.getByTestId("options").textContent!).skills).toEqual(
    [
      { package_id: "expert-package", slug: "expert-skill" },
      project.skills[0],
      { package_id: "package", slug: "new-skill" },
    ],
  );
});

it("consumes picker confirmation before the composer's Enter-to-send handler", async () => {
  const send = vi.fn();
  render(<Composer initial="/new" onSend={send} />);
  await screen.findByRole("option", { name: /New skill/ });
  fireEvent.keyDown(screen.getByLabelText("draft"), { key: "Enter" });
  expect(send).not.toHaveBeenCalled();
  await waitFor(() =>
    expect(screen.getByLabelText("draft")).toHaveValue("/new-skill "),
  );
  fireEvent.keyDown(screen.getByLabelText("draft"), { key: "Enter" });
  expect(send).toHaveBeenCalledTimes(1);
});

it("keeps an explicit empty selection separate from inheritance", async () => {
  render(
    <Composer
      currentProject={project}
      initial="/new"
      options={{ skills: [] }}
    />,
  );
  fireEvent.click(await screen.findByRole("option", { name: /New skill/ }));
  expect(JSON.parse(screen.getByTestId("options").textContent!).skills).toEqual(
    [{ package_id: "package", slug: "new-skill" }],
  );
});

it("requires explicit confirmation before replacing workspace-only inherited Skills", async () => {
  render(
    <Composer
      initial="/new"
      catalog={{
        ...resources,
        experts: [{ ...resources.experts![0], has_workspace_skills: true }],
      }}
    />,
  );
  fireEvent.click(await screen.findByRole("option", { name: /New skill/ }));
  expect(screen.getByTestId("options")).toHaveTextContent("{}");
  fireEvent.click(
    await screen.findByRole("button", {
      name: "projects.composerResources.replaceWorkspaceSkillsConfirm",
    }),
  );
  await waitFor(() =>
    expect(
      JSON.parse(screen.getByTestId("options").textContent!).skills,
    ).toHaveLength(2),
  );
});

it("does not silently overwrite a same-slug Skill from another resource", async () => {
  render(
    <Composer
      initial="/new"
      options={{ skills: [{ package_id: "other", slug: "new-skill" }] }}
    />,
  );
  expect(
    await screen.findByRole("option", { name: /New skill/ }),
  ).toBeDisabled();
  expect(JSON.parse(screen.getByTestId("options").textContent!).skills).toEqual(
    [{ package_id: "other", slug: "new-skill" }],
  );
});

it("waits for expert Skill metadata before replacing an inherited selection", async () => {
  render(
    <Composer
      initial="/new"
      currentProject={project}
      catalog={{
        ...resources,
        experts: [
          { expert_id: "expert", name: "Older peer", config_revision: 1 },
        ],
      }}
    />,
  );
  expect(
    await screen.findByText(
      "projects.composerResources.skillDefaultsUnavailable",
    ),
  ).toBeVisible();
  expect(
    screen.getByRole("button", { name: "projects.composerResources.skills" }),
  ).toBeDisabled();
  expect(
    screen.queryByRole("option", { name: /New skill/ }),
  ).not.toBeInTheDocument();
  expect(screen.getByTestId("options")).toHaveTextContent("{}");
});

it("allows a known explicit selection even if an older peer lacks default metadata", async () => {
  render(
    <Composer
      initial="/new"
      options={{ skills: [] }}
      catalog={{
        ...resources,
        experts: [
          { expert_id: "expert", name: "Older peer", config_revision: 1 },
        ],
      }}
    />,
  );
  fireEvent.click(await screen.findByRole("option", { name: /New skill/ }));
  expect(JSON.parse(screen.getByTestId("options").textContent!).skills).toEqual(
    [{ package_id: "package", slug: "new-skill" }],
  );
});

it("reserves the Task compact command instead of offering a conflicting Skill shortcut", async () => {
  render(
    <Composer
      initial="/compact"
      catalog={{
        ...resources,
        skills: [
          {
            package_id: "package",
            slug: "compact",
            name: "Conflicting compact skill",
            description: "",
          },
        ],
      }}
    />,
  );
  expect(
    await screen.findByText("projects.composerResources.empty"),
  ).toBeVisible();
  expect(
    screen.queryByRole("option", { name: /Conflicting compact skill/ }),
  ).not.toBeInTheDocument();
  expect(screen.getByTestId("options")).toHaveTextContent("{}");
});

it("ignores late file results after changing Tasks", async () => {
  let resolve!: (value: unknown[]) => void;
  api.tree.mockReturnValueOnce(
    new Promise((done) => {
      resolve = done;
    }),
  );
  const view = render(<Composer taskID="first" initial="@note" />);
  await waitFor(() => expect(api.tree).toHaveBeenCalledTimes(1));
  view.rerender(<Composer taskID="second" initial="@note" />);
  await act(async () =>
    resolve([{ name: "note-private", path: "note-private", is_dir: false }]),
  );
  await waitFor(() => expect(api.tree).toHaveBeenCalledTimes(2));
  expect(
    screen.queryByRole("option", { name: /note-private/ }),
  ).not.toBeInTheDocument();
});
