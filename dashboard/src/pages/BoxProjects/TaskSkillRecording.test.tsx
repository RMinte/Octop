import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  taskWorkbenchApi,
  type TaskWorkbenchScope,
} from "../../api/modules/taskWorkbench";
import TaskSkillRecording from "./TaskSkillRecording";
vi.mock("../../api/modules/taskWorkbench", () => ({
  taskWorkbenchApi: {
    recordingStatus: vi.fn(),
    recordingSkill: vi.fn(),
    startRecording: vi.fn(),
    stopRecording: vi.fn(),
    applyRecording: vi.fn(),
  },
}));
vi.mock("react-i18next", () => {
  const t = (key: string) => key;
  return { useTranslation: () => ({ t }) };
});
const api = vi.mocked(taskWorkbenchApi);
const scope: TaskWorkbenchScope = {
  task_id: "task_a",
  scope_revision: "scope-3",
  server_directory: "/project",
  client_directory: null,
  capabilities: {
    files: true,
    browser: true,
    terminal: true,
    recording: true,
    tool_ui: true,
  },
  read_only: false,
};
beforeEach(() => {
  vi.clearAllMocks();
  api.recordingStatus.mockResolvedValue({ ok: true, active: null });
  api.startRecording.mockResolvedValue({ ok: true, recordingId: "record_a" });
  api.stopRecording.mockResolvedValue({
    ok: true,
    recordingId: "record_a",
    skillContent: "# Steps\n1. Open",
    skillName: "browser-skill",
  });
  api.applyRecording.mockResolvedValue({});
});
describe("Task Skill recording", () => {
  it("restores a generated draft after reopening and still requires explicit application", async () => {
    api.recordingStatus.mockResolvedValue({
      ok: true,
      active: null,
      latestRecordingId: "record_a",
    });
    api.recordingSkill.mockResolvedValue({
      ok: true,
      recordingId: "record_a",
      skillContent: "# Recovered draft",
      skillName: "browser-skill",
      skillExists: true,
    });
    render(<TaskSkillRecording scope={scope} hasProject={false} />);
    expect(
      await screen.findByRole("textbox", {
        name: "projects.tasks.workbench.skillContent",
      }),
    ).toHaveValue("# Recovered draft");
    expect(api.recordingSkill).toHaveBeenCalledWith(
      "task_a",
      "record_a",
      "scope-3",
    );
    expect(api.applyRecording).not.toHaveBeenCalled();
    expect(
      screen.getByRole("button", {
        name: "projects.tasks.workbench.applySkill",
      }),
    ).toBeEnabled();
  });

  it("requires generated Skill review and confirmation before writing to task/project scope", async () => {
    render(<TaskSkillRecording scope={scope} hasProject />);
    fireEvent.change(
      screen.getByRole("textbox", {
        name: "projects.tasks.workbench.skillName",
      }),
      { target: { value: "browser-skill" } },
    );
    await waitFor(() =>
      expect(
        screen.getByRole("button", {
          name: "projects.tasks.workbench.startRecording",
        }),
      ).toBeEnabled(),
    );
    fireEvent.click(
      screen.getByRole("button", {
        name: "projects.tasks.workbench.startRecording",
      }),
    );
    await waitFor(() =>
      expect(api.startRecording).toHaveBeenCalledWith(
        "task_a",
        "browser-skill",
        "scope-3",
      ),
    );
    fireEvent.click(
      await screen.findByRole("button", {
        name: "projects.tasks.workbench.stopRecording",
      }),
    );
    expect(
      await screen.findByRole("textbox", {
        name: "projects.tasks.workbench.skillContent",
      }),
    ).toHaveValue("# Steps\n1. Open");
    expect(api.applyRecording).not.toHaveBeenCalled();
    fireEvent.change(
      screen.getByRole("textbox", {
        name: "projects.tasks.workbench.skillContent",
      }),
      { target: { value: "# Reviewed" } },
    );
    fireEvent.click(
      screen.getByRole("button", {
        name: "projects.tasks.workbench.applySkill",
      }),
    );
    await waitFor(() =>
      expect(api.applyRecording).toHaveBeenCalledWith("task_a", "record_a", {
        request_id: expect.any(String),
        name: "browser-skill",
        content: "# Reviewed",
        expected_scope_revision: "scope-3",
      }),
    );
  });
  it("reuses the same receipt after an unknown save outcome", async () => {
    api.applyRecording
      .mockRejectedValueOnce(new Error("network receipt lost"))
      .mockResolvedValueOnce({});
    render(<TaskSkillRecording scope={scope} hasProject={false} />);
    fireEvent.change(
      screen.getByRole("textbox", {
        name: "projects.tasks.workbench.skillName",
      }),
      { target: { value: "browser-skill" } },
    );
    await waitFor(() =>
      expect(
        screen.getByRole("button", {
          name: "projects.tasks.workbench.startRecording",
        }),
      ).toBeEnabled(),
    );
    fireEvent.click(
      screen.getByRole("button", {
        name: "projects.tasks.workbench.startRecording",
      }),
    );
    fireEvent.click(
      await screen.findByRole("button", {
        name: "projects.tasks.workbench.stopRecording",
      }),
    );
    fireEvent.click(
      await screen.findByRole("button", {
        name: "projects.tasks.workbench.applySkill",
      }),
    );
    await screen.findByText("network receipt lost");
    await waitFor(() =>
      expect(
        screen.getByRole("button", {
          name: "projects.tasks.workbench.applySkill",
        }),
      ).toBeEnabled(),
    );
    fireEvent.click(
      screen.getByRole("button", {
        name: "projects.tasks.workbench.applySkill",
      }),
    );
    await waitFor(() => expect(api.applyRecording).toHaveBeenCalledTimes(2));
    expect(api.applyRecording.mock.calls[0][2].request_id).toBe(
      api.applyRecording.mock.calls[1][2].request_id,
    );
  });
});
