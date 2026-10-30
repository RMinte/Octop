import { afterEach, expect, it, vi } from "vitest";
import { randomUUID } from "./randomUUID";

afterEach(() => vi.unstubAllGlobals());

it("uses native UUID generation when available", () => {
  const native = vi.fn(() => "a6aad9cd-401b-4734-93c0-099f547441d3");
  vi.stubGlobal("crypto", { randomUUID: native });
  expect(randomUUID()).toBe("a6aad9cd-401b-4734-93c0-099f547441d3");
  expect(native).toHaveBeenCalledOnce();
});

it("preserves random bits and sets UUID v4 version and variant over HTTP", () => {
  const getRandomValues = vi.fn((bytes: Uint8Array) => {
    bytes.set([0, 1, 2, 3, 4, 5, 0xf6, 7, 0xf8, 9, 10, 11, 12, 13, 14, 15]);
    return bytes;
  });
  vi.stubGlobal("crypto", { getRandomValues });
  expect(randomUUID()).toBe("00010203-0405-4607-b809-0a0b0c0d0e0f");
  expect(getRandomValues).toHaveBeenCalledOnce();
});

it("generates fresh UUIDs using browser randomness without randomUUID", () => {
  vi.stubGlobal("crypto", {
    getRandomValues: globalThis.crypto.getRandomValues.bind(globalThis.crypto),
  });
  const ids = Array.from({ length: 100 }, () => randomUUID());
  expect(new Set(ids).size).toBe(ids.length);
  for (const id of ids)
    expect(id).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
    );
});
