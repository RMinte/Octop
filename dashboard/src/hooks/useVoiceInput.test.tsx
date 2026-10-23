import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useVoiceInput } from "./useVoiceInput";
vi.mock("./useVoiceConfig", () => ({
  cachedActiveVoice: () => ({ stt: "browser", tts: "browser" }),
  fetchActiveVoice: vi.fn(),
}));
vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key, i18n: { language: "zh" } }),
}));
const abort = vi.fn();
const recognitions: FakeRecognition[] = [];
class FakeRecognition {
  onresult: ((event: unknown) => void) | null = null;
  onerror: ((event: unknown) => void) | null = null;
  onend: (() => void) | null = null;
  start = vi.fn();
  stop = vi.fn();
  abort = abort;
  constructor() {
    recognitions.push(this);
  }
}
afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
  recognitions.length = 0;
});
describe("Task voice input navigation", () => {
  it("aborts native speech on unmount and never appends text to the next Task", async () => {
    Object.defineProperty(window, "SpeechRecognition", {
      configurable: true,
      value: FakeRecognition,
    });
    const onText = vi.fn();
    const { result, unmount } = renderHook(() => useVoiceInput(onText));
    act(() => result.current.toggle());
    await waitFor(() => expect(recognitions[0].start).toHaveBeenCalled());
    unmount();
    expect(abort).toHaveBeenCalledTimes(1);
    expect(recognitions[0].onresult).toBe(null);
    await act(async () => {
      await Promise.resolve();
    });
    expect(onText).not.toHaveBeenCalled();
    delete (window as unknown as Record<string, unknown>).SpeechRecognition;
  });
});
