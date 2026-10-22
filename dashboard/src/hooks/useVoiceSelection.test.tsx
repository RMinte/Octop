import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const voice = vi.hoisted(() => ({
  active: null as { stt: string; tts: string } | null,
  mobile: false,
  fetch: vi.fn(),
  synthesize: vi.fn(),
  stream: vi.fn(),
  transcribe: vi.fn(),
  browserSpeak: vi.fn(),
  prime: vi.fn(),
  error: vi.fn(),
}));

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key, i18n: { language: "zh" } }),
}));
vi.mock("../api/modules/voice", () => ({
  voiceApi: {
    synthesize: voice.synthesize,
    synthesizeStream: voice.stream,
    transcribe: voice.transcribe,
  },
}));
vi.mock("./useVoiceConfig", () => ({
  cachedActiveVoice: () => voice.active,
  fetchActiveVoice: voice.fetch,
}));
vi.mock("../utils/mobileDevice", () => ({
  isMobileUserAgent: () => voice.mobile,
}));
vi.mock("./useAudioUnlock", () => ({
  ensureAudioUnlocked: vi.fn(),
  isAutoplayBlockedError: () => false,
  primeAudioElement: voice.prime,
}));
vi.mock("../utils/browserSpeech", () => ({
  speakBrowserText: voice.browserSpeak,
  stopBrowserSpeech: vi.fn(),
}));
vi.mock("../utils/wavStreamPlayer", () => ({
  WavStreamPlayer: class {
    ensureContext() {
      return true;
    }
    stop() {}
  },
}));
vi.mock("@/utils/antdMessage", () => ({
  message: { error: voice.error, info: vi.fn(), warning: vi.fn() },
}));

import { useVoiceInput } from "./useVoiceInput";
import { useVoiceOutput } from "./useVoiceOutput";

class FakeAudio {
  src = "";
  onended = null;
  onerror = null;
  play = vi.fn().mockResolvedValue(undefined);
  pause = vi.fn();
  load = vi.fn();
  removeAttribute = vi.fn();
}

class FakeMediaRecorder {
  static instances: FakeMediaRecorder[] = [];
  static isTypeSupported() {
    return true;
  }
  state = "inactive";
  onstop: ((event: Event) => void) | null = null;
  ondataavailable: ((event: { data: Blob }) => void) | null = null;
  constructor() {
    FakeMediaRecorder.instances.push(this);
  }
  start() {
    this.state = "recording";
  }
  stop() {
    this.state = "inactive";
    this.ondataavailable?.({
      data: new Blob(["recording"], { type: "audio/webm" }),
    });
    this.onstop?.(new Event("stop"));
  }
}

beforeEach(() => {
  vi.clearAllMocks();
  voice.active = { stt: "box-stt", tts: "box-tts" };
  voice.mobile = false;
  voice.fetch.mockResolvedValue(voice.active);
  voice.synthesize.mockResolvedValue(
    new Blob(["audio"], { type: "audio/mpeg" }),
  );
  voice.transcribe.mockResolvedValue({ text: "recognized" });
  FakeMediaRecorder.instances = [];
  vi.stubGlobal("Audio", FakeAudio);
  vi.stubGlobal("MediaRecorder", FakeMediaRecorder);
  vi.stubGlobal("navigator", {
    mediaDevices: {
      getUserMedia: vi.fn().mockResolvedValue({
        getTracks: () => [{ stop: vi.fn() }],
      }),
    },
  });
  vi.stubGlobal("URL", {
    createObjectURL: vi.fn().mockReturnValue("blob:audio"),
    revokeObjectURL: vi.fn(),
  });
});

afterEach(() => vi.unstubAllGlobals());

describe("TTS provider selection", () => {
  it.each([
    [false, "box-tts"],
    [true, "box-tts"],
    [false, "remote-tts"],
    [true, "remote-tts"],
  ])("mobile=%s keeps selected provider %s", async (mobile, provider) => {
    voice.mobile = mobile;
    voice.active = { stt: "box-stt", tts: provider };
    const hook = renderHook(() => useVoiceOutput());
    act(() => hook.result.current.speak("message", "hello"));
    await waitFor(() =>
      expect(voice.synthesize).toHaveBeenCalledWith("hello", provider),
    );
    expect(voice.browserSpeak).not.toHaveBeenCalled();
  });

  it.each(["box-tts", "remote-tts"])(
    "resolves cold mobile config before choosing %s",
    async (provider) => {
      voice.mobile = true;
      voice.active = null;
      let resolve!: (active: { stt: string; tts: string }) => void;
      voice.fetch.mockReturnValueOnce(
        new Promise((complete) => (resolve = complete)),
      );
      const hook = renderHook(() => useVoiceOutput());
      act(() => hook.result.current.speak("message", "hello"));
      expect(voice.prime).toHaveBeenCalledOnce();
      expect(voice.synthesize).not.toHaveBeenCalled();
      await act(async () => resolve({ stt: "box-stt", tts: provider }));
      await waitFor(() =>
        expect(voice.synthesize).toHaveBeenCalledWith("hello", provider),
      );
    },
  );

  it("uses Edge on mobile only when browser TTS is selected", async () => {
    voice.mobile = true;
    voice.active = { stt: "browser", tts: "browser" };
    const hook = renderHook(() => useVoiceOutput());
    act(() => hook.result.current.speak("message", "hello"));
    await waitFor(() =>
      expect(voice.synthesize).toHaveBeenCalledWith("hello", "edge"),
    );
  });

  it("keeps browser TTS on desktop", () => {
    voice.active = { stt: "browser", tts: "browser" };
    const hook = renderHook(() => useVoiceOutput());
    act(() => hook.result.current.speak("message", "hello"));
    expect(voice.browserSpeak).toHaveBeenCalledWith(
      "hello",
      expect.any(Object),
    );
    expect(voice.synthesize).not.toHaveBeenCalled();
  });

  it("passes the same MiMo selection through streaming and buffered fallback", async () => {
    voice.active = { stt: "box-stt", tts: "mimo-tts" };
    voice.stream.mockImplementationOnce(async () => {
      voice.active = { stt: "box-stt", tts: "box-tts" };
      return { contentType: "audio/mpeg", body: { cancel: vi.fn() } };
    });
    const hook = renderHook(() => useVoiceOutput());
    act(() => hook.result.current.speak("message", "hello"));
    await waitFor(() =>
      expect(voice.synthesize).toHaveBeenCalledWith("hello", "mimo-tts"),
    );
    expect(voice.stream).toHaveBeenCalledWith("hello", "mimo-tts");
  });
});

describe("STT provider selection", () => {
  it.each([
    ["box-stt", "remote-stt"],
    ["remote-stt", "box-stt"],
  ])(
    "keeps recording provider %s after default changes to %s",
    async (provider, changed) => {
      voice.active = { stt: provider, tts: "box-tts" };
      const onText = vi.fn();
      const hook = renderHook(() => useVoiceInput(onText));
      act(() => hook.result.current.toggle());
      await waitFor(() => expect(hook.result.current.recording).toBe(true));
      voice.active = { stt: changed, tts: "box-tts" };
      voice.fetch.mockResolvedValue(voice.active);
      act(() => hook.result.current.toggle());
      await waitFor(() => expect(onText).toHaveBeenCalledWith("recognized"));
      expect(voice.transcribe).toHaveBeenCalledWith(
        expect.any(Blob),
        "zh-CN",
        provider,
      );
      expect(voice.fetch).not.toHaveBeenCalled();
    },
  );

  it("resolves a cold cache before recording and freezes that provider", async () => {
    voice.active = null;
    let resolve!: (active: { stt: string; tts: string }) => void;
    voice.fetch.mockReturnValueOnce(
      new Promise((complete) => (resolve = complete)),
    );
    const hook = renderHook(() => useVoiceInput(vi.fn()));
    act(() => hook.result.current.toggle());
    expect(navigator.mediaDevices.getUserMedia).not.toHaveBeenCalled();
    await act(async () => resolve({ stt: "box-stt", tts: "box-tts" }));
    await waitFor(() => expect(hook.result.current.recording).toBe(true));
    voice.active = { stt: "remote-stt", tts: "box-tts" };
    act(() => hook.result.current.toggle());
    await waitFor(() =>
      expect(voice.transcribe).toHaveBeenCalledWith(
        expect.any(Blob),
        "zh-CN",
        "box-stt",
      ),
    );
    expect(voice.fetch).toHaveBeenCalledOnce();
  });
});
