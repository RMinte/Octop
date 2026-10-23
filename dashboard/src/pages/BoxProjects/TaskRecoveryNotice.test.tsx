import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import type { TaskTurn } from "../../api/modules/projects";
import TaskRecoveryNotice from "./TaskRecoveryNotice";

vi.mock("react-i18next", () => {
  const t = (key: string) => key;
  return { useTranslation: () => ({ t }) };
});
const turn = {
  turn_id: "turn",
  status: "interrupted",
  recovery: {
    can_resume: true,
    needs_reconciliation: false,
    phase: "mcp_connect",
    reason_code: "TASK_RESOURCE_UNAVAILABLE",
    diagnostic_id: "diagnostic-proof",
    cause_type: "ConnectionError",
    resource_type: "mcp",
    resource_id: "service",
    status_code: 503,
  },
} as TaskTurn;

it("resumes the original interrupted turn only on explicit selection", () => {
  const resume = vi.fn();
  render(<TaskRecoveryNotice turn={turn} onResume={resume} />);
  expect(resume).not.toHaveBeenCalled();
  fireEvent.click(
    screen.getByRole("button", { name: "projects.recovery.resume" }),
  );
  expect(resume).toHaveBeenCalledTimes(1);
});

it.each([
  {
    name: "unconfirmed external results",
    change: { recovery: { ...turn.recovery!, needs_reconciliation: true } },
  },
  {
    name: "non-resumable interruption",
    change: { recovery: { ...turn.recovery!, can_resume: false } },
  },
  { name: "a failed turn", change: { status: "failed" } },
  { name: "history-only trajectory", change: { history_only: true } },
])("does not offer recovery for $name", ({ change }) => {
  render(
    <TaskRecoveryNotice
      turn={{ ...turn, ...change } as TaskTurn}
      onResume={vi.fn()}
    />,
  );
  expect(
    screen.queryByRole("button", { name: "projects.recovery.resume" }),
  ).not.toBeInTheDocument();
  expect(screen.getByText("projects.recovery.details")).toBeVisible();
});

it("keeps the resume action disabled during another operation or without a handler", () => {
  const resume = vi.fn();
  const view = render(
    <TaskRecoveryNotice turn={turn} onResume={resume} disabled />,
  );
  const button = screen.getByRole("button", {
    name: "projects.recovery.resume",
  });
  expect(button).toBeDisabled();
  fireEvent.click(button);
  expect(resume).not.toHaveBeenCalled();
  view.rerender(<TaskRecoveryNotice turn={turn} />);
  expect(
    screen.getByRole("button", { name: "projects.recovery.resume" }),
  ).toBeDisabled();
});

it("keeps technical diagnostics collapsed until requested", async () => {
  render(<TaskRecoveryNotice turn={turn} />);
  expect(screen.queryByText("diagnostic-proof")).not.toBeInTheDocument();
  fireEvent.click(screen.getByText("projects.recovery.details"));
  expect(await screen.findByText("diagnostic-proof")).toBeVisible();
  expect(screen.getByText("mcp_connect")).toBeVisible();
  expect(screen.getByText("TASK_RESOURCE_UNAVAILABLE")).toBeVisible();
  expect(screen.getByText("ConnectionError")).toBeVisible();
});

it("does not turn ordinary paused or completed turns into recovery failures", () => {
  const view = render(
    <TaskRecoveryNotice turn={{ ...turn, status: "paused" }} />,
  );
  expect(view.container).toBeEmptyDOMElement();
  view.rerender(<TaskRecoveryNotice turn={{ ...turn, status: "completed" }} />);
  expect(view.container).toBeEmptyDOMElement();
  view.rerender(<TaskRecoveryNotice turn={{ ...turn, recovery: null }} />);
  expect(view.container).toBeEmptyDOMElement();
});
