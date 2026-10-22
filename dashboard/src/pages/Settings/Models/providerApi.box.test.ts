import { expect, it } from "vitest";
import { toImportedProviderModel } from "./providerApi";

it("retains the box's context limit and known vision capability on model import", () => {
  expect(
    toImportedProviderModel(
      {
        id: "RM-01 VLM",
        name: "RM-01 VLM",
        context_window: 262144,
      },
      "box",
    ),
  ).toMatchObject({
    context_window: 262144,
    input: ["text", "image"],
    enabled: false,
  });
});

it("keeps the speech task classification so imported models stay outside chat selection", () => {
  expect(
    toImportedProviderModel(
      {
        id: "qwen3-asr",
        name: "qwen3-asr",
        task: "asr",
      },
      "box",
    ),
  ).toMatchObject({ task: "asr", enabled: false });
});
