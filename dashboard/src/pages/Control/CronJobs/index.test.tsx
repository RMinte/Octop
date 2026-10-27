import { Form, type FormInstance } from "antd";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { expect, it, vi } from "vitest";
import CronJobsPage from ".";

const state = vi.hoisted(() => ({
  jobs: [
    {
      id: "one",
      name: "Project one timer",
      project_id: "p1",
      selected_expert_id: null,
    },
    {
      id: "two",
      name: "Project two timer",
      project_id: "p2",
      selected_expert_id: null,
    },
    {
      id: "old",
      name: "Legacy timer",
      project_id: null,
      selected_expert_id: "expert-1",
    },
  ],
}));
vi.mock("../../../context/AgentContext", () => ({
  useAgent: () => ({ activeAgentId: null, activeAgent: null, agents: [] }),
}));
vi.mock("../../../hooks/useCardTableView", () => ({
  useCardTableView: () => ({
    isMobile: false,
    viewMode: "card",
    setViewMode: vi.fn(),
    showCardView: true,
  }),
}));
vi.mock("../../../layouts/PageShell", () => ({
  default: ({ children }: { children: ReactNode }) => <main>{children}</main>,
}));
vi.mock("../../../api/modules/projects", () => ({
  projectTasksApi: {
    resources: async () => ({
      experts: [{ expert_id: "supported", name: "Supported expert" }],
    }),
  },
  projectsApi: {
    list: async () => [
      { project_id: "p1", name: "Project one", archived: false },
      { project_id: "p2", name: "Project two", archived: false },
    ],
  },
}));
vi.mock("./components", () => ({
  createColumns: () => [],
  CronJobCard: ({
    job,
    onDetail,
  }: {
    job: { name: string; project_name: string };
    onDetail?: () => void;
  }) => (
    <article onClick={onDetail}>
      {job.name}: {job.project_name}
    </article>
  ),
  ExecuteNowModal: () => null,
  JobDrawer: ({
    open,
    experts,
    form,
  }: {
    open: boolean;
    experts: Array<{ name: string }>;
    form: FormInstance;
  }) => (
    <Form form={form}>
      {open ? (
        <aside>
          {experts.map((expert) => (
            <span key={expert.name}>{expert.name}</span>
          ))}
        </aside>
      ) : null}
    </Form>
  ),
  JobDetailDrawer: () => null,
  useCronJobs: () => ({
    jobs: state.jobs,
    loading: false,
    listRefreshing: false,
    listStale: false,
    cronTimezone: "Asia/Shanghai",
    createJob: vi.fn(),
    updateJob: vi.fn(),
    deleteJob: vi.fn(),
    toggleEnabled: vi.fn(),
    executeNow: vi.fn(),
    jobToFormValues: vi.fn(),
    refetchJobs: vi.fn(),
  }),
}));

it("shows timers without an active expert and filters by project, including legacy unassigned timers", async () => {
  render(<CronJobsPage />);
  await waitFor(() =>
    expect(
      screen.getByText("Project one timer: Project one"),
    ).toBeInTheDocument(),
  );
  expect(
    screen.getByText("Legacy timer: cronJobs.noProject"),
  ).toBeInTheDocument();
  const user = userEvent.setup();
  await user.click(screen.getByRole("combobox"));
  await user.click(
    screen.getByText("Project one", {
      selector: ".ant-select-item-option-content",
    }),
  );
  expect(
    screen.getByText("Project one timer: Project one"),
  ).toBeInTheDocument();
  expect(screen.queryByText(/Project two timer/)).not.toBeInTheDocument();
  expect(screen.queryByText(/Legacy timer/)).not.toBeInTheDocument();
  await user.click(screen.getByRole("combobox"));
  await user.click(
    screen.getByText("cronJobs.noProject", {
      selector: ".ant-select-item-option-content",
    }),
  );
  expect(
    screen.getByText("Legacy timer: cronJobs.noProject"),
  ).toBeInTheDocument();
  expect(screen.queryByText(/Project one timer/)).not.toBeInTheDocument();
  await user.click(
    screen.getByRole("button", { name: "+ cronJobs.createJob" }),
  );
  expect(screen.getByText("Supported expert")).toBeInTheDocument();
});
