import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import TaskEngineSelect from "./TaskEngineSelect";
import { engineCapabilities, OCTOP_ENGINE } from "./taskEngine";
import type { EngineCatalogEntry } from "../../api/modules/projects";
const currentUser = vi.hoisted(() => ({ role: "admin" as string | null }));
vi.mock("../../hooks/useCurrentUser", () => ({
  useCurrentUser: () => (currentUser.role ? { role: currentUser.role } : null),
}));
beforeEach(() => {
  currentUser.role = "admin";
});
const acp = {
  engine_id: "acp",
  connection_id: "codex",
  model_id: null,
} as const;
const catalog: EngineCatalogEntry[] = [
  {
    engine: OCTOP_ENGINE,
    label: "Octop",
    available: true,
    unavailable_reason: null,
    capabilities: engineCapabilities(OCTOP_ENGINE),
    models: [],
  },
  {
    engine: acp,
    label: "Codex",
    available: true,
    unavailable_reason: null,
    capabilities: engineCapabilities(acp),
    models: [],
  },
];
it("uses host-scoped engine entries and never offers local setup for remote Tasks", async () => {
  const change = vi.fn();
  render(
    <TaskEngineSelect
      value={OCTOP_ENGINE}
      catalog={catalog}
      disabled={false}
      hostID="peer"
      onChange={change}
    />,
  );
  expect(
    screen.queryByRole("link", { name: "projects.tasks.manageEngines" }),
  ).not.toBeInTheDocument();
  await userEvent.click(
    screen.getByRole("combobox", { name: "projects.tasks.engine" }),
  );
  await userEvent.click(
    screen.getByText("Codex", { selector: ".ant-select-item-option-content" }),
  );
  expect(change).toHaveBeenCalledWith(acp);
});
it("offers local ACP management only for the local box", () => {
  render(
    <TaskEngineSelect
      value={OCTOP_ENGINE}
      catalog={catalog}
      disabled={false}
      hostID={null}
      onChange={vi.fn()}
    />,
  );
  expect(
    screen.getByRole("link", { name: "projects.tasks.manageEngines" }),
  ).toHaveAttribute("href", "/acp");
});

it.each(["user", null])(
  "explains local engine management access without an inaccessible link for role %s",
  (role) => {
    currentUser.role = role;
    render(
      <TaskEngineSelect
        value={OCTOP_ENGINE}
        catalog={catalog}
        disabled={false}
        hostID={null}
        onChange={vi.fn()}
      />,
    );
    expect(
      screen.queryByRole("link", { name: "projects.tasks.manageEngines" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByText("projects.tasks.engineManagementAdminOnly"),
    ).toBeInTheDocument();
  },
);

it("shows remote setup guidance before an unavailable external engine can be selected", () => {
  render(
    <TaskEngineSelect
      value={OCTOP_ENGINE}
      catalog={[
        catalog[0],
        { ...catalog[1], available: false, unavailable_reason: "disabled" },
      ]}
      disabled={false}
      hostID="peer"
      onChange={vi.fn()}
    />,
  );
  expect(
    screen.getByText("projects.tasks.remoteEngineManagement"),
  ).toBeVisible();
  expect(
    screen.queryByRole("link", { name: "projects.tasks.manageEngines" }),
  ).not.toBeInTheDocument();
  expect(
    screen.queryByText("projects.tasks.engineUnavailable"),
  ).not.toBeInTheDocument();
});
