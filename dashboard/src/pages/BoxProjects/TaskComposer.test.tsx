import { useEffect, useState } from "react";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Modal } from "antd";
import { beforeEach, expect, it, vi } from "vitest";
import {
  projectTasksApi,
  type Project,
  type TaskExecutionOptions,
  type TaskResources,
} from "../../api/modules/projects";
import TaskComposer from "./TaskComposer";

vi.mock("../../api/modules/projects", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../api/modules/projects")>()),
  projectTasksApi: { resources: vi.fn() },
}));
vi.mock("./TaskKnowledgeField", () => ({ default: () => null }));
vi.mock("../Agent/Skills/components/SkillHubTab", () => ({
  default: () => <button>SkillHub catalogue</button>,
}));
vi.mock("react-i18next", () => {
  const t = (key: string, options?: Record<string, unknown>) =>
    options?.name ? `${key}:${options.name}` : key;
  return { useTranslation: () => ({ t }) };
});

const expertSkill = { package_id: "expert-package", slug: "review" };
const projectSkill = { package_id: "project-package", slug: "review" };
const project = {
  skills: [expertSkill, projectSkill],
  mcp_servers: [],
  plugins: [],
  subagents: [],
} as unknown as Project;
let resources: TaskResources;
beforeEach(() => {
  vi.clearAllMocks();
  resources = {
    skills: [
      { ...expertSkill, name: "Expert review", description: "" },
      { ...projectSkill, name: "Project review", description: "" },
    ],
    mcp_servers: [],
    plugins: [],
    subagents: [],
    directory_roots: [],
    approval: {
      allowed_modes: [],
      reviewer_ready: true,
      execution_profile: "host_process",
    },
    experts: [
      {
        expert_id: "expert",
        name: "Expert",
        description: "",
        config_revision: 1,
        default_skills: [expertSkill],
      },
    ],
    models: [
      { model_ref: "provider/model", name: "Model", supports_images: false },
    ],
    default_model_ref: "provider/model",
  };
  vi.mocked(projectTasksApi.resources).mockImplementation(
    async () => resources,
  );
});

function Composer({
  initial = {},
  changed = vi.fn(),
  loaded,
}: {
  initial?: TaskExecutionOptions;
  changed?: (next: TaskExecutionOptions) => void;
  loaded?: (catalog: TaskResources) => void;
}) {
  const [value, setValue] = useState(initial);
  return (
    <TaskComposer
      executionSettings={{
        value,
        onChange: (next) => {
          setValue(next);
          changed(next);
        },
        project,
        selectedExpertID: "expert",
        disabled: false,
        onResourcesLoaded: loaded,
      }}
      identity={<button disabled>Expert identity</button>}
      actions={<button>Add attachment</button>}
      send={<button>Send</button>}
      webSearch={<button>Task web search</button>}
      approval={<button>Account approval</button>}
    >
      <textarea aria-label="Draft" />
    </TaskComposer>
  );
}

it("keeps model and action slots available while identity is locked, with one resource request", async () => {
  const loaded = vi.fn();
  render(<Composer loaded={loaded} />);
  await waitFor(() => expect(loaded).toHaveBeenCalledWith(resources));
  expect(projectTasksApi.resources).toHaveBeenCalledTimes(1);
  expect(
    screen.getByRole("button", { name: "Expert identity" }),
  ).toBeDisabled();
  expect(
    screen.getByRole("combobox", { name: "projects.tasks.model" }),
  ).not.toBeDisabled();
  expect(screen.getByRole("textbox", { name: "Draft" })).toBeVisible();
  expect(screen.getByRole("button", { name: "Add attachment" })).toBeVisible();
  expect(screen.getByRole("button", { name: "Task web search" })).toBeVisible();
  expect(
    screen.getByRole("button", { name: "Account approval" }),
  ).toBeVisible();
});

it("removes inherited skills by package identity, preserves explicit empty, and can restore inheritance", async () => {
  const changed = vi.fn();
  render(<Composer changed={changed} />);
  await screen.findByRole("button", {
    name: "projects.composer.removeResource:Expert review",
  });
  expect(screen.getAllByText("Expert review")).toHaveLength(1);
  fireEvent.click(
    screen.getByRole("button", {
      name: "projects.composer.removeResource:Expert review",
    }),
  );
  expect(changed).toHaveBeenLastCalledWith({ skills: [projectSkill] });
  fireEvent.click(
    screen.getByRole("button", {
      name: "projects.composer.removeResource:Project review",
    }),
  );
  expect(changed).toHaveBeenLastCalledWith({ skills: [] });
  fireEvent.click(
    screen.getByRole("button", {
      name: "projects.composer.removeResource:projects.tasks.customSkills · projects.composer.explicitlyEmpty",
    }),
  );
  expect(changed).toHaveBeenLastCalledWith({ skills: undefined });
  expect(await screen.findByText("Expert review")).toBeVisible();
});

it("requires explicit confirmation before replacing inherited workspace skills", async () => {
  resources.experts![0].has_workspace_skills = true;
  const confirm = vi
    .spyOn(Modal, "confirm")
    .mockReturnValue({ destroy: vi.fn(), update: vi.fn() });
  const changed = vi.fn();
  render(<Composer changed={changed} />);
  await screen.findByText("projects.composer.workspaceSkills");
  fireEvent.click(
    screen.getByRole("button", {
      name: "projects.composer.removeResource:Expert review",
    }),
  );
  expect(changed).not.toHaveBeenCalled();
  expect(confirm).toHaveBeenCalledWith(
    expect.objectContaining({
      content: "projects.composerResources.replaceWorkspaceSkills",
    }),
  );
  await act(async () => {
    await confirm.mock.calls[0][0].onOk?.();
  });
  expect(changed).toHaveBeenLastCalledWith({ skills: [projectSkill] });
  confirm.mockRestore();
});

it("keeps Task and expert memory overrides independent and resets overrides without losing the model", async () => {
  const changed = vi.fn();
  const user = userEvent.setup();
  render(
    <Composer
      changed={changed}
      initial={{
        model_ref: "provider/model",
        skills: [],
        plugins: [],
        memory_enabled: false,
        expert_memory_enabled: false,
      }}
    />,
  );
  await screen.findByText("projects.tasks.expertMemory · projects.disabled");
  fireEvent.click(
    screen.getByRole("button", {
      name: "projects.composer.removeResource:projects.memory · projects.disabled",
    }),
  );
  expect(changed).toHaveBeenLastCalledWith(
    expect.objectContaining({
      model_ref: "provider/model",
      memory_enabled: undefined,
      expert_memory_enabled: false,
    }),
  );
  await user.click(
    screen.getByRole("button", { name: "projects.tasks.executionOptions" }),
  );
  await user.click(
    screen.getByRole("button", { name: "projects.composer.resetOverrides" }),
  );
  expect(changed).toHaveBeenLastCalledWith({ model_ref: "provider/model" });
});

it("keeps the skill catalogue usable when opened from the settings popover", async () => {
  const user = userEvent.setup();
  render(<Composer initial={{ skills: [] }} />);
  await user.click(
    screen.getByRole("button", { name: "projects.tasks.executionOptions" }),
  );
  await user.click(screen.getByRole("button", { name: "projects.addSkill" }));
  await user.click(
    await screen.findByRole("button", { name: "SkillHub catalogue" }),
  );
  expect(
    screen.getByRole("button", { name: "SkillHub catalogue" }),
  ).toBeVisible();
  await user.click(
    screen.getByRole("button", { name: "projects.finishSkillSelection" }),
  );
  await waitFor(() =>
    expect(
      screen.queryByRole("button", { name: "SkillHub catalogue" }),
    ).not.toBeInTheDocument(),
  );
});

it("does not replace expert skill defaults before their metadata is available", async () => {
  resources.experts![0].default_skills = undefined;
  const changed = vi.fn();
  render(<Composer changed={changed} />);
  const remove = await screen.findByRole("button", {
    name: "projects.composer.removeResource:Project review",
  });
  expect(remove).toBeDisabled();
  fireEvent.click(remove);
  expect(changed).not.toHaveBeenCalled();
  const user = userEvent.setup();
  await user.click(
    screen.getByRole("button", { name: "projects.tasks.executionOptions" }),
  );
  expect(
    screen.getByRole("switch", { name: "projects.tasks.customSkills" }),
  ).toBeDisabled();
  await waitFor(() =>
    expect(
      screen.getByText("projects.composerResources.skillDefaultsUnavailable"),
    ).toBeVisible(),
  );
});

it("closes scoped options on Task changes without remounting account controls", async () => {
  const mounted = vi.fn();
  function Approval() {
    useEffect(() => {
      mounted();
    }, []);
    return <button>Persistent account approval</button>;
  }
  const settings = { value: {}, onChange: vi.fn(), disabled: false };
  const view = render(
    <TaskComposer
      executionSettings={{ ...settings, scopeKey: "task-a" }}
      approval={<Approval />}
    />,
  );
  const user = userEvent.setup();
  await user.click(
    screen.getByRole("button", { name: "projects.tasks.executionOptions" }),
  );
  await screen.findByRole("dialog");
  view.rerender(
    <TaskComposer
      executionSettings={{ ...settings, scopeKey: "task-b" }}
      approval={<Approval />}
    />,
  );
  await waitFor(() =>
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
  );
  expect(mounted).toHaveBeenCalledTimes(1);
});
