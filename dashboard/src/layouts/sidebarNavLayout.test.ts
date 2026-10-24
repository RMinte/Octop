import { describe, expect, it } from "vitest";
import type { OctopUser } from "../api/modules/auth";
import { buildNavSections } from "./sidebarNav";
import {
  createGroup,
  deleteGroup,
  editorFromCatalog,
  layoutFromEditor,
  placeItem,
  sectionsFromLayout,
  visibleEditorGroups,
} from "./sidebarNavLayout";

const adminUser = {
  id: 1,
  username: "admin",
  role: "admin",
  permissions: ["*"],
} as OctopUser;

const limitedUser = {
  id: 2,
  username: "ada",
  role: "user",
  permissions: [],
} as OctopUser;

function catalog() {
  return buildNavSections(adminUser, { mobileEnabled: true });
}

describe("sidebarNavLayout", () => {
  it("defaults to one conversation entry while retaining legacy chat in the customizer", () => {
    const sections = catalog();
    expect(
      sectionsFromLayout(sections, null)[0].items.map((item) => item.key),
    ).toEqual(["box-projects", "resident", "experts", "tasks", "token-usage"]);
    expect(sections[0].items.some((item) => item.key === "chat")).toBe(true);
    const editor = editorFromCatalog(sections, null);
    expect(editor.ungrouped).toEqual([
      "box-projects",
      "resident",
      "experts",
      "tasks",
      "token-usage",
    ]);
    expect(editor.groups.map((group) => group.id)).toEqual([
      "settings",
      "control",
      "admin",
    ]);
    expect(editor.hidden).toEqual(["chat"]);
  });

  it("omits empty groups and hidden items from the sidebar", () => {
    const sections = catalog();
    const applied = sectionsFromLayout(sections, {
      groups: [
        { id: "settings", name: "配置" },
        { id: "control" },
        { id: "c_emptygrp1", name: "空组" },
      ],
      items: [
        { key: "chat" },
        { key: "personalization", group: "settings" },
        { key: "token-usage", hidden: true },
        { key: "workbench", group: "control" },
      ],
    });
    expect(applied.map((section) => section.id ?? "front")).toEqual([
      "front",
      "settings",
      "control",
    ]);
    expect(applied[0]?.items.map((item) => item.key)).toEqual([
      "chat",
      "box-projects",
      "resident",
    ]);
    expect(applied[1]?.title).toBe("配置");
    expect(applied[1]?.items.map((item) => item.key)).toEqual([
      "personalization",
    ]);
    expect(applied[2]?.groupKey).toBe("nav.control");
  });

  it("sends keys missing from a saved layout to hidden", () => {
    const editor = editorFromCatalog(catalog(), {
      groups: [{ id: "settings" }],
      items: [{ key: "chat" }],
    });
    expect(editor.ungrouped).toEqual(["chat", "box-projects", "resident"]);
    expect(editor.hidden).toContain("experts");
    expect(editor.hidden).toContain("personalization");
    expect(editor.ungrouped).toContain("box-projects");
    expect(
      sectionsFromLayout(catalog(), {
        groups: [{ id: "settings" }],
        items: [{ key: "chat" }],
      })
        .flatMap((section) => section.items.map((item) => item.key))
        .includes("experts"),
    ).toBe(false);
    expect(
      sectionsFromLayout(catalog(), {
        groups: [{ id: "settings" }],
        items: [{ key: "chat" }],
      })
        .flatMap((section) => section.items.map((item) => item.key))
        .includes("box-projects"),
    ).toBe(true);
  });

  it("respects an explicitly hidden box projects entry", () => {
    const editor = editorFromCatalog(catalog(), {
      groups: [],
      items: [{ key: "box-projects", hidden: true }],
    });
    expect(editor.hidden).toContain("box-projects");
    expect(editor.ungrouped).not.toContain("box-projects");
  });

  it("adds the private assistant to saved navigation without restoring other hidden items", () => {
    const layout = { groups: [], items: [{ key: "experts", hidden: true }] };
    const editor = editorFromCatalog(catalog(), layout);
    expect(editor.ungrouped).toContain("resident");
    expect(editor.hidden).toContain("experts");
    const visible = sectionsFromLayout(catalog(), layout).flatMap((section) =>
      section.items.map((item) => item.key),
    );
    expect(visible).toContain("resident");
    expect(visible).not.toContain("experts");
  });

  it("moves a deleted group's items to the front of the ungrouped list", () => {
    const editor = editorFromCatalog(catalog(), null);
    const next = deleteGroup(editor, "settings");
    expect(next.groups.map((group) => group.id)).toEqual(["control", "admin"]);
    expect(next.ungrouped.slice(0, 2)).toEqual(["personalization", "channels"]);
    expect(next.hidden).toContain("chat");
  });

  it("places an item before another and can hide it", () => {
    const editor = editorFromCatalog(catalog(), null);
    const moved = placeItem(editor, "tasks", { kind: "ungrouped" }, "experts");
    expect(moved.ungrouped.slice(0, 4)).toEqual([
      "box-projects",
      "resident",
      "tasks",
      "experts",
    ]);
    const hidden = placeItem(moved, "tasks", { kind: "hidden" }, null);
    expect(hidden.ungrouped).not.toContain("tasks");
    expect(hidden.hidden).toContain("tasks");
    const layout = layoutFromEditor(hidden);
    expect(layout.items.find((item) => item.key === "tasks")?.hidden).toBe(
      true,
    );
  });

  it("can restore the default-hidden legacy entry without losing its route", () => {
    const sections = catalog();
    const editor = placeItem(
      editorFromCatalog(sections, null),
      "chat",
      { kind: "ungrouped" },
      "experts",
    );
    const restored = sectionsFromLayout(sections, layoutFromEditor(editor));
    expect(restored[0].items.find((item) => item.key === "chat")).toMatchObject(
      { path: "/chat", labelKey: "nav.legacyChat" },
    );
    expect(editor.hidden).not.toContain("chat");
  });

  it("omits nav items and built-in groups the user cannot access", () => {
    const sections = buildNavSections(limitedUser, { mobileEnabled: false });
    const editor = editorFromCatalog(sections, {
      groups: [
        { id: "settings" },
        { id: "admin" },
        { id: "c_aabbccdd", name: "常用" },
      ],
      items: [
        { key: "chat" },
        { key: "admin-users", group: "admin" },
        { key: "channels", group: "settings" },
        { key: "personalization", group: "settings" },
      ],
    });
    const shown = [
      ...editor.ungrouped,
      ...Object.values(editor.itemsByGroup).flat(),
      ...editor.hidden,
    ];
    expect(shown).not.toContain("admin-users");
    expect(shown).not.toContain("channels");
    expect(shown).toContain("chat");
    expect(shown).toContain("personalization");
    expect(
      visibleEditorGroups(editor, sections).map((group) => group.id),
    ).toEqual(["settings", "c_aabbccdd"]);
  });

  it("keeps a new empty group only in the editor", () => {
    const editor = createGroup(
      editorFromCatalog(catalog(), null),
      "c_aabbccdd",
      "常用",
    );
    expect(editor.groups.at(-1)?.name).toBe("常用");
    const applied = sectionsFromLayout(catalog(), layoutFromEditor(editor));
    expect(applied.some((section) => section.id === "c_aabbccdd")).toBe(false);
  });
});
