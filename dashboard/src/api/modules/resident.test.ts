import { beforeEach, expect, it, vi } from "vitest";
import { request } from "../request";
import { residentApi } from "./resident";

vi.mock("../request", () => ({ request: vi.fn().mockResolvedValue({}) }));
const scope = { agent_id: "private/agent", thread_id: "private thread" };
const base = "/agents/private%2Fagent/threads/private%20thread";
beforeEach(() => vi.clearAllMocks());

it("reads and repairs the server-managed identity through separate operations", async () => {
  await residentApi.binding();
  await residentApi.capabilities();
  await residentApi.repair();
  expect(vi.mocked(request).mock.calls).toEqual([
    ["/agents/resident-conversation"],
    ["/agents/resident-capabilities"],
    ["/agents/resident-conversation", { method: "PUT" }],
  ]);
});
it("preserves original send and exact cancellation identities and explicit false/null scope", async () => {
  const message = {
    request_id: "send-1",
    prompt: " original ",
    read_tools: false,
    knowledge_base_ids: [],
    task_context: null,
  };
  await residentApi.send(scope, message);
  await residentApi.request(scope, "send/1");
  await residentApi.cancel(scope, {
    request_id: "cancel-1",
    turn_id: "turn-1",
  });
  expect(request).toHaveBeenNthCalledWith(1, `${base}/messages`, {
    method: "POST",
    body: JSON.stringify(message),
  });
  expect(request).toHaveBeenNthCalledWith(2, `${base}/requests/send%2F1`);
  expect(request).toHaveBeenNthCalledWith(3, `${base}/cancel`, {
    method: "POST",
    body: '{"request_id":"cancel-1","turn_id":"turn-1"}',
  });
});
it("keeps history cursors opaque and search/location distinct from live snapshots", async () => {
  await residentApi.snapshot(scope);
  await residentApi.history(scope, {
    day_key: "2026-10-24",
    cursor: "opaque+/=",
    limit: 10,
  });
  await residentApi.searchHistory(scope, "a & b", "next?", 5);
  await residentApi.historyDays(scope, { at: "2026-10-24", limit: 7 });
  await residentApi.locateHistory(scope, "turn:item/1", 0, 2);
  expect(vi.mocked(request).mock.calls.map((call) => call[0])).toEqual([
    `${base}/conversation?limit=50`,
    `${base}/conversation/history?day_key=2026-10-24&cursor=opaque%2B%2F%3D&limit=10`,
    `${base}/conversation/history/search?q=a+%26+b&cursor=next%3F&limit=5`,
    `${base}/conversation/history/days?at=2026-10-24&limit=7`,
    `${base}/conversation/history/items/turn%3Aitem%2F1?before=0&after=2`,
  ]);
});
it("keeps memory reads, explicit capture CAS and bounded extraction as separate calls", async () => {
  await residentApi.memory(scope, { include_deprecated: false, limit: 20 });
  await residentApi.saveMemory(scope, {
    assertion: "User confirmed",
    kind: "Preference",
    source_turn_id: "turn",
  });
  await residentApi.memoryPolicy(scope);
  await residentApi.updateMemoryPolicy(scope, {
    capture_enabled: false,
    expected_revision: 3,
  });
  await residentApi.candidates(scope, { status: "pending", limit: 10 });
  await residentApi.extractCandidates(scope);
  expect(vi.mocked(request).mock.calls.map((call) => call[0])).toEqual([
    `${base}/memory?include_deprecated=false&limit=20`,
    `${base}/memory`,
    `${base}/memory/policy`,
    `${base}/memory/policy`,
    `${base}/memory/candidates?status=pending&limit=10`,
    `${base}/memory/candidates:extract`,
  ]);
  expect(request).toHaveBeenNthCalledWith(4, `${base}/memory/policy`, {
    method: "PUT",
    body: '{"capture_enabled":false,"expected_revision":3}',
  });
  expect(request).toHaveBeenNthCalledWith(
    6,
    `${base}/memory/candidates:extract`,
    { method: "POST" },
  );
});
it("encodes evidence and decision IDs and preserves audited corrections", async () => {
  await residentApi.candidate(scope, "candidate/1");
  await residentApi.approveCandidate(scope, "candidate/1");
  await residentApi.rejectCandidate(scope, "candidate/1", "not true");
  await residentApi.memorySources(scope, "atom/1");
  await residentApi.candidateSources(scope, "candidate/1");
  await residentApi.replaceMemory(scope, "atom/1", "Corrected");
  await residentApi.deprecateMemory(scope, "atom/1", "outdated");
  expect(vi.mocked(request).mock.calls.map((call) => call[0])).toEqual([
    `${base}/memory/candidates/candidate%2F1`,
    `${base}/memory/candidates/candidate%2F1:approve`,
    `${base}/memory/candidates/candidate%2F1:reject`,
    `${base}/memory/atom%2F1/sources`,
    `${base}/memory/candidates/candidate%2F1/sources`,
    `${base}/memory/atom%2F1:replace`,
    `${base}/memory/atom%2F1:deprecate`,
  ]);
  expect(request).toHaveBeenNthCalledWith(
    6,
    `${base}/memory/atom%2F1:replace`,
    { method: "POST", body: '{"assertion":"Corrected"}' },
  );
});
it("advances read state only through an explicit actual message anchor", async () => {
  await residentApi.inbox(scope);
  await residentApi.markRead(scope, "resident:turn:assistant");
  expect(request).toHaveBeenNthCalledWith(1, `${base}/conversation/inbox`);
  expect(request).toHaveBeenNthCalledWith(2, `${base}/conversation/read`, {
    method: "POST",
    body: '{"message_id":"resident:turn:assistant"}',
  });
});
