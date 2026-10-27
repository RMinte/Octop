import { Form } from "antd";
import type { ReactNode } from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { JobDrawer } from "./JobDrawer";
import { buildDefaultFormValues } from "./constants";
import type { CronJobFormValues } from "../useCronJobs";

vi.mock("antd", async (importOriginal) => {
  const actual = await importOriginal<typeof import("antd")>();
  return {
    ...actual,
    Drawer: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  };
});

vi.mock("../../../../api/modules/provider", () => ({
  providerApi: { listResolvedModels: async () => [] },
}));
vi.mock("../../../../api/modules/connectors", () => ({
  connectorsApi: { listInstances: async () => [] },
}));

function Harness({
  values,
  onSubmit = vi.fn(),
}: {
  values: CronJobFormValues;
  onSubmit?: (values: CronJobFormValues) => void;
}) {
  const [form] = Form.useForm<CronJobFormValues>();
  return (
    <JobDrawer
      open
      editingJob={values}
      projects={[{ project_id: "p1", name: "Project one", archived: false }]}
      experts={[{ expert_id: "supported", name: "Supported expert" }]}
      defaultProjectId="p1"
      cronTimezone="Asia/Shanghai"
      form={form}
      onClose={vi.fn()}
      onSubmit={onSubmit}
    />
  );
}

const values: CronJobFormValues = {
  ...buildDefaultFormValues("Asia/Shanghai"),
  id: "existing",
  name: "Daily summary",
  prompt: "Summarize progress",
  schedule: { type: "cron", cron: "0 9 * * *", timezone: "Asia/Shanghai" },
  project_id: "p1",
  selected_expert_id: "unsupported",
};

it("offers the supported Task catalog while retaining an unavailable existing expert ID", async () => {
  render(<Harness values={values} />);
  await screen.findByText("unsupported (cronJobs.expertUnavailable)");
  const expertSelect = screen.getByRole("combobox", {
    name: "cronJobs.expert",
  });
  expect(expertSelect).not.toBeDisabled();
  await userEvent.click(expertSelect);
  expect(
    screen.getByText("Supported expert", {
      selector: ".ant-select-item-option-content",
    }),
  ).toBeInTheDocument();
  const unavailable = screen.getByText(
    "unsupported (cronJobs.expertUnavailable)",
    { selector: ".ant-select-item-option-content" },
  );
  expect(unavailable.closest(".ant-select-item-option")).toHaveClass(
    "ant-select-item-option-disabled",
  );
});

it("explains and disables schedule expert selection when continuing a linked task", async () => {
  render(
    <Harness
      values={{
        ...values,
        conversation_policy: "task",
        target_task_id: "target-task",
      }}
    />,
  );
  await waitFor(() =>
    expect(
      screen.getByRole("combobox", { name: "cronJobs.expert" }),
    ).toBeDisabled(),
  );
  expect(screen.getByText("cronJobs.targetTaskExpertHint")).toBeInTheDocument();
});

it.each([false, true])(
  "loads skip confirmation as %s and submits the changed value",
  async (skipConfirmation) => {
    const onSubmit = vi.fn();
    render(
      <Harness
        values={{ ...values, skip_confirmation: skipConfirmation }}
        onSubmit={onSubmit}
      />,
    );
    const toggle = screen.getByRole("switch", {
      name: "cronJobs.form.skipConfirmation",
    });
    await waitFor(() =>
      expect(toggle).toHaveAttribute("aria-checked", String(skipConfirmation)),
    );
    expect(
      screen.getByText("cronJobs.form.skipConfirmationHint"),
    ).toBeInTheDocument();
    await userEvent.click(toggle);
    await userEvent.click(screen.getByRole("button", { name: "common.save" }));
    await waitFor(() => expect(onSubmit).toHaveBeenCalledOnce());
    expect(onSubmit.mock.calls[0][0]).toMatchObject({
      skip_confirmation: !skipConfirmation,
      project_id: "p1",
      selected_expert_id: "unsupported",
    });
  },
);
