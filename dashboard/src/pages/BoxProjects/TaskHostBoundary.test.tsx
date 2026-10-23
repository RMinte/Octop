import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";
import {
  bridgeApi,
  type BridgeTaskCapabilities,
} from "../../api/modules/bridge";
import { useTaskHost } from "../../context/TaskHostContext";
import TaskHostBoundary from "./TaskHostBoundary";
import { useEffect } from "react";
vi.mock("../../api/modules/bridge", () => ({
  bridgeApi: { taskCapabilities: vi.fn() },
}));
vi.mock("./TaskHostDirectory", () => ({ default: () => null }));
vi.mock("react-i18next", () => {
  const t = (key: string) => key;
  return { useTranslation: () => ({ t }) };
});
const mounted = vi.fn(),
  disposed = vi.fn();
function Probe() {
  const { identity, hostID } = useTaskHost();
  useEffect(() => {
    mounted(identity);
    return () => disposed(identity);
  }, [identity]);
  return (
    <output>
      {hostID}:{identity}
    </output>
  );
}
const capability = (instance: string, supported = true) =>
  ({
    available: supported,
    protocol_version: 3,
    host_id: "peer",
    peer_identity: instance,
    capabilities: { tasks: supported, projects: true, task_ws: true },
  }) as BridgeTaskCapabilities;
beforeEach(() => vi.clearAllMocks());
it("blocks peers without the unified contract instead of opening legacy writes", async () => {
  vi.mocked(bridgeApi.taskCapabilities).mockResolvedValue(
    capability("one", false),
  );
  render(
    <MemoryRouter>
      <TaskHostBoundary hostID="peer">
        <Probe />
      </TaskHostBoundary>
    </MemoryRouter>,
  );
  await screen.findByText("projects.tasks.hosts.unavailable");
  expect(mounted).not.toHaveBeenCalled();
});
it("discards the previous host view when its verified peer identity changes", async () => {
  vi.mocked(bridgeApi.taskCapabilities).mockResolvedValue(capability("one"));
  render(
    <MemoryRouter>
      <TaskHostBoundary hostID="peer">
        <Probe />
      </TaskHostBoundary>
    </MemoryRouter>,
  );
  await waitFor(() => expect(mounted).toHaveBeenCalledTimes(1));
  const oldIdentity = JSON.stringify(capability("one").peer_identity);
  vi.mocked(bridgeApi.taskCapabilities).mockResolvedValue(capability("two"));
  fireEvent.focus(window);
  await waitFor(() => expect(mounted).toHaveBeenCalledTimes(2));
  expect(disposed).toHaveBeenCalledWith(oldIdentity);
  expect(screen.getByText(/peer:.*two/)).toBeInTheDocument();
});
