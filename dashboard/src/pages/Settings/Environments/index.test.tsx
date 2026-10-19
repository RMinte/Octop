import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { App } from "antd";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useTaskPresentationSettings } from "../../../hooks/useTaskPresentationSettings";
import EnvironmentsPage from ".";

const { save, list, settings } = vi.hoisted(() => ({
  save: vi.fn(),
  list: vi.fn(),
  settings: vi.fn(),
}));
vi.mock("../../../api", () => ({
  default: { batchSaveEnvs: save, listEnvs: list },
}));
vi.mock("../../../api/modules/projects", () => ({
  projectTasksApi: { settings },
}));
vi.mock("../../../hooks/useIsMobile", () => ({ useIsMobile: () => false }));
vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

function SettingsObserver() {
  const value = useTaskPresentationSettings();
  return <output aria-label="task settings">{JSON.stringify(value)}</output>;
}

beforeEach(() => {
  vi.clearAllMocks();
  list.mockResolvedValue([
    { key: "OCTOP_TASK_STEP_PANEL_ENABLED", value: "true" },
  ]);
  settings.mockResolvedValue({
    action_timeline_enabled: true,
    step_panel_enabled: true,
  });
});

describe("环境变量保存与任务展示刷新", () => {
  it("仅在服务端确认保存成功后刷新已打开的任务设置", async () => {
    let finishSave!: () => void;
    save.mockImplementationOnce(
      () =>
        new Promise<void>((resolve) => {
          finishSave = resolve;
        }),
    );
    render(
      <App>
        <EnvironmentsPage />
        <SettingsObserver />
      </App>,
    );
    await waitFor(() => expect(settings).toHaveBeenCalledTimes(1));
    const input = await screen.findByPlaceholderText("Value");
    fireEvent.change(input, { target: { value: "false" } });
    fireEvent.click(screen.getByRole("button", { name: "common.save" }));
    await waitFor(() => expect(save).toHaveBeenCalledOnce());
    expect(settings).toHaveBeenCalledTimes(1);
    settings.mockResolvedValue({
      action_timeline_enabled: true,
      step_panel_enabled: false,
    });
    await act(async () => finishSave());
    await waitFor(() => expect(settings).toHaveBeenCalledTimes(2));
    expect(screen.getByLabelText("task settings")).toHaveTextContent(
      '"step_panel_enabled":false',
    );
  });
});
