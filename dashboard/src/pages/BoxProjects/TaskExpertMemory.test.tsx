import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import zh from "../../locales/zh.json";
import { taskExpertMemoryApi } from "../../api/modules/taskExpertMemory";
import TaskExpertMemory from "./TaskExpertMemory";

vi.mock("../../api/modules/taskExpertMemory", () => ({
  taskExpertMemoryApi: {
    list: vi.fn(),
    policy: vi.fn(),
    save: vi.fn(),
    updatePolicy: vi.fn(),
  },
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
const api = vi.mocked(taskExpertMemoryApi, true);
const policy = {
  expert_id: "expert_a",
  expert_name: "A",
  config_revision: 4,
  enabled: true,
  capture_enabled: false,
  legacy_recall_enabled: false,
};
beforeEach(() => {
  vi.clearAllMocks();
  api.policy.mockResolvedValue(policy);
  api.list.mockImplementation(async (_id, source) => ({
    expert_id: "expert_a",
    source,
    entries: [
      {
        path: "private_namespace/fact.md",
        content: "Known fact",
        layer: "canonical",
      },
    ],
    enabled: true,
  }));
  api.save.mockResolvedValue({
    expert_id: "expert_a",
    source: "scope",
    status: "completed",
    replayed: false,
  });
  api.updatePolicy.mockImplementation(async () => {
    const updated = {
      ...policy,
      config_revision: 5,
      legacy_recall_enabled: true,
    };
    api.policy.mockResolvedValue(updated);
    return updated;
  });
});
async function open() {
  render(
    <TaskExpertMemory
      taskID="task_a"
      expertID="expert_a"
      projectID="project_a"
    />,
  );
  await userEvent.click(
    screen.getByRole("button", { name: zh.projects.tasks.memory.open }),
  );
  await screen.findByText("Known fact");
}

it("reads and saves the chosen scope without displaying storage identifiers", async () => {
  await open();
  expect(
    screen.queryByText("private_namespace/fact.md"),
  ).not.toBeInTheDocument();
  const input = screen.getByRole("textbox", {
    name: zh.projects.tasks.memory.content,
  });
  fireEvent.change(input, { target: { value: "Confirmed project fact" } });
  await userEvent.click(
    screen.getByRole("button", { name: zh.projects.tasks.memory.save }),
  );
  await waitFor(() =>
    expect(api.save).toHaveBeenCalledWith(
      "task_a",
      expect.objectContaining({
        expected_expert_id: "expert_a",
        source: "scope",
        content: "Confirmed project fact",
        request_id: expect.any(String),
      }),
    ),
  );
  await waitFor(() => expect(input).toHaveValue(""));
  await userEvent.click(screen.getByText(zh.projects.tasks.memory.general));
  fireEvent.change(input, { target: { value: "Reusable expert method" } });
  await userEvent.click(
    screen.getByRole("button", { name: zh.projects.tasks.memory.save }),
  );
  await waitFor(() =>
    expect(api.save).toHaveBeenLastCalledWith(
      "task_a",
      expect.objectContaining({
        expected_expert_id: "expert_a",
        source: "general",
      }),
    ),
  );
});

it("keeps legacy memory read-only and updates expert-wide recall with a revision", async () => {
  await open();
  await userEvent.click(screen.getByText(zh.projects.tasks.memory.legacy));
  await screen.findByText(zh.projects.tasks.memory.legacyHint);
  expect(
    screen.queryByRole("textbox", { name: zh.projects.tasks.memory.content }),
  ).not.toBeInTheDocument();
  await waitFor(() =>
    expect(
      screen.getByRole("switch", {
        name: zh.projects.tasks.memory.legacyRecall,
      }),
    ).toBeEnabled(),
  );
  await userEvent.click(
    screen.getByRole("switch", { name: zh.projects.tasks.memory.legacyRecall }),
  );
  await waitFor(() =>
    expect(api.updatePolicy).toHaveBeenCalledWith("task_a", {
      expected_expert_id: "expert_a",
      expected_revision: 4,
      legacy_recall_enabled: true,
    }),
  );
  expect(api.save).not.toHaveBeenCalled();
});

it("retains an uncertain save for review and does not repeat it", async () => {
  api.save.mockResolvedValue({
    expert_id: "expert_a",
    source: "scope",
    status: "uncertain",
    replayed: false,
  });
  await open();
  fireEvent.change(
    screen.getByRole("textbox", { name: zh.projects.tasks.memory.content }),
    { target: { value: "Maybe saved" } },
  );
  await userEvent.click(
    screen.getByRole("button", { name: zh.projects.tasks.memory.save }),
  );
  await screen.findByText(zh.projects.tasks.memory.uncertain);
  expect(
    screen.getByRole("button", { name: zh.projects.tasks.memory.save }),
  ).toBeDisabled();
  expect(api.save).toHaveBeenCalledOnce();
});

it("shows automatic extraction switches and preserves owner-only shared policy", async () => {
  api.policy.mockResolvedValue({
    ...policy,
    policy_editable: false,
    auto_extract_enabled: true,
    general_promotion_enabled: false,
  });
  await open();
  expect(
    screen.getByRole("switch", {
      name: zh.projects.tasks.memory.auto_extract_enabled,
    }),
  ).toBeChecked();
  expect(
    screen.getByRole("switch", {
      name: zh.projects.tasks.memory.auto_extract_enabled,
    }),
  ).toBeDisabled();
  expect(
    screen.getByRole("switch", {
      name: zh.projects.tasks.memory.general_promotion_enabled,
    }),
  ).not.toBeChecked();
  expect(
    screen.getByText(zh.projects.tasks.memory.sharedPolicyReadOnly),
  ).toBeInTheDocument();
  expect(api.updatePolicy).not.toHaveBeenCalled();
});
it("marks an uncertain automatic write for verification without retrying it", async () => {
  api.list.mockResolvedValue({
    expert_id: "expert_a",
    source: "scope",
    enabled: true,
    entries: [{ path: "fact.md", content: "Known fact", layer: "canonical" }],
    extraction_jobs: [
      {
        job_id: "job_a",
        turn_id: "turn_a",
        status: "uncertain",
        phase: "applying",
      },
    ],
  });
  await open();
  expect(
    screen.getByText(zh.projects.tasks.memory.extractionStatus.uncertain),
  ).toBeInTheDocument();
  expect(
    screen.getByText(zh.projects.tasks.memory.extractionUncertain),
  ).toBeInTheDocument();
  expect(api.save).not.toHaveBeenCalled();
});
