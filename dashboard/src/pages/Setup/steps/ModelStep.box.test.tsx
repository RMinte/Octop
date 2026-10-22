import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { request } from "../../../api/request";
import zh from "../../../locales/zh.json";
import { wizardApi } from "../wizardClient";
import ModelStep from "./ModelStep";

vi.mock("../../../api/request", () => ({ request: vi.fn() }));
vi.mock("../wizardClient", () => ({
  wizardApi: { testProvider: vi.fn() },
  wizardSession: { saveDraft: vi.fn() },
  resolveSetupProbeToken: vi.fn().mockResolvedValue("setup-token"),
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

it("carries the box model limits and vision input through keyless first-time setup", async () => {
  vi.mocked(request).mockResolvedValue([
    {
      id: "box",
      name: "盒子模型",
      protocol: "box",
      api_key_prefix: "",
      base_url: "http://127.0.0.1:58000/v1",
      models: [
        {
          id: "RM-01 VLM",
          name: "RM-01 VLM",
          input: ["text", "image"],
          context_window: 262144,
          max_tokens: 32768,
        },
      ],
    },
  ]);
  vi.mocked(wizardApi.testProvider).mockResolvedValue({
    ok: true,
    latency_ms: 1,
  });
  const onContinue = vi.fn();
  render(
    <ModelStep onBack={vi.fn()} onSkip={vi.fn()} onContinue={onContinue} />,
  );
  await screen.findByDisplayValue("http://127.0.0.1:58000/v1");
  fireEvent.click(screen.getByRole("button", { name: "测试连通性" }));
  const next = screen.getByRole("button", { name: "测试通过后继续" });
  await waitFor(() => expect(next).toBeEnabled());
  fireEvent.click(next);
  await waitFor(() => expect(onContinue).toHaveBeenCalledOnce());
  expect(onContinue.mock.calls[0][0]).toMatchObject({
    type: "box",
    api_key: "",
    models: [
      {
        id: "RM-01 VLM",
        input: ["text", "image"],
        context_window: 262144,
        max_tokens: 32768,
      },
    ],
  });
});
