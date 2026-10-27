import { render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import type { TaskInteraction } from "../../api/modules/projects";
import TaskInteractions from "./TaskInteractions";

vi.mock("../../context/TaskHostContext", () => ({
  useTaskHost: () => ({ projectTasksApi: {} }),
}));
vi.mock("../../hooks/useServerTimezone", () => ({
  useServerTimezone: () => "Asia/Shanghai",
}));

it("shows a skipped schedule confirmation as a compact action record", () => {
  const interaction: TaskInteraction = {
    interaction_id: "confirmation-1",
    task_id: "task-1",
    turn_id: "turn-1",
    tool_call_id: "tool-1",
    revision: 1,
    status: "resolved",
    kind: "approval",
    payload: {
      tool_name: "send_message",
      arguments: { text: "Daily summary" },
      execution_host: "server",
      reason: "Allowed by this schedule",
    },
    resolution: {
      outcome: "allow_once",
      source: "cron_skip_confirmation",
      answer: null,
    },
    created_at: "2026-10-27T10:00:00Z",
    expires_at: "2026-10-27T11:00:00Z",
    resolved_at: "2026-10-27T10:00:00Z",
  };
  render(
    <TaskInteractions
      taskID="task-1"
      items={[interaction]}
      disabled={false}
      onChange={vi.fn()}
    />,
  );
  const record = screen.getByText(
    /projects.tasks.approvalMode.cron_skip_confirmation/,
  );
  expect(record.tagName).toBe("SUMMARY");
  expect(record.closest("details")).not.toHaveAttribute("open");
  expect(record).toHaveTextContent("send_message");
  expect(record).toHaveTextContent(
    "projects.tasks.interactionOutcome.allow_once",
  );
  expect(screen.queryByRole("button")).not.toBeInTheDocument();
});
