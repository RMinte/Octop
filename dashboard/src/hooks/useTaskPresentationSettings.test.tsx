import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { settings } = vi.hoisted(() => ({ settings: vi.fn() }));
vi.mock("../api/modules/projects", () => ({
  projectTasksApi: { settings },
}));

import {
  invalidateTaskPresentationSettings,
  useTaskPresentationSettings,
} from "./useTaskPresentationSettings";

const defaults = { action_timeline_enabled: true, step_panel_enabled: true };
const classic = { action_timeline_enabled: false, step_panel_enabled: false };

beforeEach(() => settings.mockReset());

describe("任务过程展示设置", () => {
  it("进入页面获取一次，重新聚焦和保存环境变量后刷新当前与历史共用设置", async () => {
    settings.mockResolvedValue(defaults);
    const hook = renderHook(() => useTaskPresentationSettings());
    await waitFor(() => expect(settings).toHaveBeenCalledTimes(1));
    expect(hook.result.current).toEqual(defaults);
    settings.mockResolvedValue(classic);
    act(() => window.dispatchEvent(new Event("focus")));
    await waitFor(() => expect(hook.result.current).toEqual(classic));
    expect(settings).toHaveBeenCalledTimes(2);
    settings.mockResolvedValue(defaults);
    act(() => invalidateTaskPresentationSettings());
    await waitFor(() => expect(hook.result.current).toEqual(defaults));
    expect(settings).toHaveBeenCalledTimes(3);
  });

  it("切回可见页面刷新，普通重渲染和卸载后事件不重复请求", async () => {
    settings.mockResolvedValue(defaults);
    const hook = renderHook(() => useTaskPresentationSettings());
    await waitFor(() => expect(settings).toHaveBeenCalledTimes(1));
    hook.rerender();
    expect(settings).toHaveBeenCalledTimes(1);
    act(() => document.dispatchEvent(new Event("visibilitychange")));
    await waitFor(() => expect(settings).toHaveBeenCalledTimes(2));
    hook.unmount();
    act(() => {
      window.dispatchEvent(new Event("focus"));
      invalidateTaskPresentationSettings();
    });
    expect(settings).toHaveBeenCalledTimes(2);
  });

  it("较旧的慢响应不能覆盖保存后新设置", async () => {
    let completeOld!: (value: typeof defaults) => void;
    settings.mockReturnValueOnce(
      new Promise((resolve) => {
        completeOld = resolve;
      }),
    );
    const hook = renderHook(() => useTaskPresentationSettings());
    settings.mockResolvedValueOnce(classic);
    act(() => invalidateTaskPresentationSettings());
    await waitFor(() => expect(hook.result.current).toEqual(classic));
    await act(async () => completeOld(defaults));
    expect(hook.result.current).toEqual(classic);
  });

  it("暂时失败保留最近确认值，下一次焦点恢复继续读取", async () => {
    settings.mockResolvedValueOnce(classic);
    const hook = renderHook(() => useTaskPresentationSettings());
    await waitFor(() => expect(hook.result.current).toEqual(classic));
    settings.mockRejectedValueOnce(new Error("unavailable"));
    await act(async () => window.dispatchEvent(new Event("focus")));
    expect(hook.result.current).toEqual(classic);
    settings.mockResolvedValueOnce(defaults);
    act(() => window.dispatchEvent(new Event("focus")));
    await waitFor(() => expect(hook.result.current).toEqual(defaults));
  });
});
