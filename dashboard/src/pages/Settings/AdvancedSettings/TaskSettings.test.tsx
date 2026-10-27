import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  projectTasksApi,
  type TaskPresentationSettings,
} from "../../../api/modules/projects";
import { invalidateTaskPresentationSettings } from "../../../hooks/useTaskPresentationSettings";
import TaskSettingsPanel from "./TaskSettings";

const { t } = vi.hoisted(() => ({ t: (key: string) => key }));

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t }),
}));

vi.mock("../../../api/modules/projects", () => ({
  projectTasksApi: {
    settings: vi.fn(),
    updateSettings: vi.fn(),
  },
}));

vi.mock("../../../hooks/useTaskPresentationSettings", () => ({
  invalidateTaskPresentationSettings: vi.fn(),
}));

const api = vi.mocked(projectTasksApi);
const initial: TaskPresentationSettings = {
  action_timeline_enabled: false,
  step_panel_enabled: true,
};
const actions = () =>
  screen.getByRole("switch", { name: "advancedSettings.tasks.actions" });
const steps = () =>
  screen.getByRole("switch", { name: "advancedSettings.tasks.steps" });
const save = () => screen.getByRole("button", { name: "common.save" });

beforeEach(() => {
  vi.resetAllMocks();
  api.settings.mockResolvedValue(initial);
});

describe("TaskSettingsPanel", () => {
  it("loads the server switches and only allows saving an edited draft", async () => {
    const user = userEvent.setup();
    render(<TaskSettingsPanel />);

    await screen.findByRole("switch", {
      name: "advancedSettings.tasks.actions",
    });
    expect(actions()).not.toBeChecked();
    expect(steps()).toBeChecked();
    expect(save()).toBeDisabled();
    expect(screen.getByRole("button", { name: "common.reset" })).toBeDisabled();

    await user.click(save());
    expect(api.updateSettings).not.toHaveBeenCalled();

    await user.click(actions());
    expect(save()).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "common.reset" }));
    expect(actions()).not.toBeChecked();
    expect(steps()).toBeChecked();
    expect(save()).toBeDisabled();
    expect(invalidateTaskPresentationSettings).not.toHaveBeenCalled();
  });

  it("waits for confirmation before notifying task views and uses the confirmed settings", async () => {
    let confirm!: (value: TaskPresentationSettings) => void;
    api.updateSettings.mockReturnValue(
      new Promise((resolve) => {
        confirm = resolve;
      }),
    );
    const user = userEvent.setup();
    render(<TaskSettingsPanel />);
    await screen.findByRole("switch", {
      name: "advancedSettings.tasks.actions",
    });

    await user.click(actions());
    await user.click(save());

    expect(api.updateSettings).toHaveBeenCalledExactlyOnceWith({
      action_timeline_enabled: true,
      step_panel_enabled: true,
    });
    expect(actions()).toBeDisabled();
    expect(steps()).toBeDisabled();
    expect(invalidateTaskPresentationSettings).not.toHaveBeenCalled();
    expect(
      screen.queryByText("advancedSettings.tasks.saved"),
    ).not.toBeInTheDocument();

    await act(async () => {
      confirm({ action_timeline_enabled: true, step_panel_enabled: false });
    });

    expect(
      await screen.findByText("advancedSettings.tasks.saved"),
    ).toBeVisible();
    expect(actions()).toBeChecked();
    expect(steps()).not.toBeChecked();
    expect(save()).toBeDisabled();
    expect(invalidateTaskPresentationSettings).toHaveBeenCalledTimes(1);
  });

  it("preserves the draft after a failed save and allows retrying it", async () => {
    const draft = { action_timeline_enabled: false, step_panel_enabled: false };
    api.updateSettings
      .mockRejectedValueOnce(new Error("save unavailable"))
      .mockResolvedValueOnce(draft);
    const user = userEvent.setup();
    render(<TaskSettingsPanel />);
    await screen.findByRole("switch", { name: "advancedSettings.tasks.steps" });

    await user.click(steps());
    await user.click(save());

    expect(await screen.findByText("save unavailable")).toBeVisible();
    expect(actions()).not.toBeChecked();
    expect(steps()).not.toBeChecked();
    expect(save()).toBeEnabled();
    expect(invalidateTaskPresentationSettings).not.toHaveBeenCalled();

    await user.click(save());

    expect(
      await screen.findByText("advancedSettings.tasks.saved"),
    ).toBeVisible();
    expect(api.updateSettings).toHaveBeenNthCalledWith(1, draft);
    expect(api.updateSettings).toHaveBeenNthCalledWith(2, draft);
    expect(screen.queryByText("save unavailable")).not.toBeInTheDocument();
    expect(save()).toBeDisabled();
    expect(invalidateTaskPresentationSettings).toHaveBeenCalledTimes(1);
  });

  it("keeps failed loads from exposing editable defaults and retries loading", async () => {
    api.settings
      .mockRejectedValueOnce(new Error("settings unavailable"))
      .mockResolvedValueOnce(initial);
    const user = userEvent.setup();
    render(<TaskSettingsPanel />);

    expect(await screen.findByText("settings unavailable")).toBeVisible();
    expect(screen.queryByRole("switch")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "common.save" }),
    ).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "common.retry" }));

    await waitFor(() => expect(api.settings).toHaveBeenCalledTimes(2));
    await screen.findByRole("switch", {
      name: "advancedSettings.tasks.actions",
    });
    expect(actions()).not.toBeChecked();
    expect(steps()).toBeChecked();
    expect(save()).toBeDisabled();
    expect(screen.queryByText("settings unavailable")).not.toBeInTheDocument();
    expect(api.updateSettings).not.toHaveBeenCalled();
  });
});
