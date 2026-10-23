import { render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import TaskSourceTrajectory from "./TaskSourceTrajectory";
vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));
it("keeps recorded source evidence read-only and never reveals a redacted payload", () => {
  render(
    <TaskSourceTrajectory
      items={[
        {
          source_event_id: "one",
          kind: "tool",
          source_status: "incomplete",
          summary: "Read failed",
          payload: { path: "note.txt" },
          history_only: true,
          restorable: false,
        },
        {
          source_event_id: "two",
          kind: "system",
          source_status: "redacted",
          payload: "private context",
          history_only: true,
          restorable: false,
        },
      ]}
    />,
  );
  expect(screen.getByText("Read failed")).toBeInTheDocument();
  expect(screen.getByText(/note.txt/)).toBeInTheDocument();
  expect(screen.queryByText("private context")).not.toBeInTheDocument();
  expect(screen.queryByRole("button")).not.toBeInTheDocument();
});
