import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ActiveVoice } from "../api/modules/voice";

const { getActive } = vi.hoisted(() => ({ getActive: vi.fn() }));
vi.mock("../api/modules/voice", () => ({ voiceApi: { getActive } }));

import {
  cachedActiveVoice,
  fetchActiveVoice,
  invalidateVoiceConfigCache,
  prefetchVoiceConfig,
} from "./useVoiceConfig";

const box = { stt: "box-stt", tts: "box-tts" };
const remote = { stt: "remote-stt", tts: "remote-tts" };

function pending() {
  let resolve!: (value: ActiveVoice) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<ActiveVoice>((accept, fail) => {
    resolve = accept;
    reject = fail;
  });
  return { promise, resolve, reject };
}

beforeEach(() => {
  getActive.mockReset();
  invalidateVoiceConfigCache();
});

describe("voice configuration cache recovery", () => {
  it("retries after the first request fails", async () => {
    getActive
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValueOnce(box);
    await expect(fetchActiveVoice()).rejects.toThrow("offline");
    expect(cachedActiveVoice()).toBeNull();
    await expect(fetchActiveVoice()).resolves.toEqual(box);
    expect(cachedActiveVoice()).toEqual(box);
    expect(getActive).toHaveBeenCalledTimes(2);
  });

  it("allows a voice action to retry a failed background preload", async () => {
    getActive
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValueOnce(box);
    prefetchVoiceConfig();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(getActive).toHaveBeenCalledOnce();
    await expect(fetchActiveVoice()).resolves.toEqual(box);
    expect(getActive).toHaveBeenCalledTimes(2);
  });

  it("keeps the newer forced result when an old success arrives later", async () => {
    const old = pending();
    const latest = pending();
    getActive
      .mockReturnValueOnce(old.promise)
      .mockReturnValueOnce(latest.promise);
    const first = fetchActiveVoice();
    const forced = fetchActiveVoice(true);
    latest.resolve(box);
    await forced;
    old.resolve(remote);
    await first;
    expect(cachedActiveVoice()).toEqual(box);
    await expect(fetchActiveVoice()).resolves.toEqual(box);
    expect(getActive).toHaveBeenCalledTimes(2);
  });

  it("an older failure cannot clear a newer forced request", async () => {
    const old = pending();
    const latest = pending();
    getActive
      .mockReturnValueOnce(old.promise)
      .mockReturnValueOnce(latest.promise);
    const first = fetchActiveVoice();
    const forced = fetchActiveVoice(true);
    old.reject(new Error("old request failed"));
    await expect(first).rejects.toThrow("old request failed");
    const shared = fetchActiveVoice();
    expect(getActive).toHaveBeenCalledTimes(2);
    latest.resolve(box);
    await expect(forced).resolves.toEqual(box);
    await expect(shared).resolves.toEqual(box);
  });

  it("invalidation detaches the old request without clearing its replacement", async () => {
    const old = pending();
    const latest = pending();
    getActive
      .mockReturnValueOnce(old.promise)
      .mockReturnValueOnce(latest.promise);
    const first = fetchActiveVoice();
    invalidateVoiceConfigCache();
    const replacement = fetchActiveVoice();
    old.resolve(remote);
    await first;
    expect(cachedActiveVoice()).toBeNull();
    const shared = fetchActiveVoice();
    expect(getActive).toHaveBeenCalledTimes(2);
    latest.resolve(box);
    await expect(replacement).resolves.toEqual(box);
    await expect(shared).resolves.toEqual(box);
    expect(cachedActiveVoice()).toEqual(box);
  });
});
