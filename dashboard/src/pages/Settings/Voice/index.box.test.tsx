import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { voiceApi, type VoicePreset } from "../../../api/modules/voice";
import zh from "../../../locales/zh.json";
import { VoiceSettingsPanel } from ".";

vi.mock("../../../api/modules/voice", () => ({
  voiceApi: {
    getPresets: vi.fn(),
    getProviders: vi.fn(),
    getActive: vi.fn(),
    createProvider: vi.fn(),
    patchProvider: vi.fn(),
    setActive: vi.fn(),
    testConfiguration: vi.fn(),
  },
}));
vi.mock("../../../hooks/useVoiceConfig", () => ({
  invalidateVoiceConfigCache: vi.fn(),
}));
vi.mock("react-i18next", () => {
  const t = (key: string) =>
    key
      .split(".")
      .reduce<unknown>(
        (value, part) => (value as Record<string, unknown>)?.[part],
        zh,
      ) ?? key;
  return { useTranslation: () => ({ t }) };
});

const presets: VoicePreset[] = [
  {
    id: "box-stt",
    name: "Box ASR",
    kind: "box",
    capability: "stt",
    free: true,
    requires_key: false,
    description: "",
    base_url: "http://127.0.0.1:58100/v1",
    extra: { model: "qwen3-asr" },
  },
  {
    id: "box-tts",
    name: "Box TTS",
    kind: "box",
    capability: "tts",
    free: true,
    requires_key: false,
    description: "",
    base_url: "http://127.0.0.1:58101/v1",
    extra: { model: "qwen3-tts", instructions: "" },
  },
];
const getComputedStyle = window.getComputedStyle.bind(window);

beforeEach(() => {
  vi.clearAllMocks();
  vi.spyOn(window, "getComputedStyle").mockImplementation((element) =>
    getComputedStyle(element),
  );
  vi.mocked(voiceApi.getPresets).mockResolvedValue(presets);
  vi.mocked(voiceApi.getProviders).mockResolvedValue([]);
  vi.mocked(voiceApi.getActive).mockResolvedValue({
    stt: "browser",
    tts: "openai",
  });
});
afterEach(() => vi.restoreAllMocks());

it("saves a box voice with its own address and voice description without credentials", async () => {
  render(<VoiceSettingsPanel />);
  const ttsName = await screen.findByText("Box TTS");
  const ttsCard = ttsName.closest('[role="button"]') as HTMLElement;
  fireEvent.click(within(ttsCard).getByRole("button", { name: "配置" }));
  expect(screen.queryByText("API Key")).not.toBeInTheDocument();
  fireEvent.change(screen.getByDisplayValue("http://127.0.0.1:58101/v1"), {
    target: { value: "http://box-tts:58101/v1" },
  });
  fireEvent.change(screen.getByRole("textbox", { name: "音色描述" }), {
    target: { value: "清晰舒缓的女声" },
  });
  fireEvent.click(screen.getByRole("button", { name: /保\s*存/ }));
  await waitFor(() => expect(voiceApi.createProvider).toHaveBeenCalledOnce());
  const payload = vi.mocked(voiceApi.createProvider).mock.calls[0][0];
  expect(payload).toMatchObject({
    name: "box-tts",
    kind: "box",
    capability: "tts",
    base_url: "http://box-tts:58101/v1",
    api_key: null,
  });
  expect(JSON.parse(payload.extra_json!)).toEqual({
    model: "qwen3-tts",
    instructions: "清晰舒缓的女声",
  });
  expect(voiceApi.setActive).not.toHaveBeenCalled();
});
