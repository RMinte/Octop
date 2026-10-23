import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { ToolDetailsInline } from "./MessageBubble";
import { LegacyHistoryContext } from "../LegacyHistoryContext";
import {
  registerToolRenderer,
  unregisterPluginRenderers,
} from "../../../plugins/toolRenderers";
afterEach(() => unregisterPluginRenderers("history-test"));
describe("Legacy tool history", () => {
  it("renders preserved results without mounting the interactive plugin in read-only history", () => {
    registerToolRenderer({
      id: "history-card",
      pluginId: "history-test",
      component: () => <button>mutate workspace</button>,
    });
    render(
      <LegacyHistoryContext.Provider value>
        <ToolDetailsInline
          toolData={{
            name: "demo",
            pluginId: "history-test",
            callId: "old_call",
            output: JSON.stringify({
              octop_ui: { renderer: "history-card" },
              data: { title: "preserved result" },
            }),
          }}
          isStreaming={false}
        />
      </LegacyHistoryContext.Provider>,
    );
    expect(screen.getByText("preserved result")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "mutate workspace" }),
    ).not.toBeInTheDocument();
  });
  it("keeps the custom renderer available for dedicated remote/resident chats", () => {
    registerToolRenderer({
      id: "history-card",
      pluginId: "history-test",
      component: () => <button>dedicated action</button>,
    });
    render(
      <ToolDetailsInline
        toolData={{
          name: "demo",
          pluginId: "history-test",
          callId: "old_call",
          output: JSON.stringify({
            octop_ui: { renderer: "history-card" },
            data: {},
          }),
        }}
        isStreaming={false}
      />,
    );
    expect(
      screen.getByRole("button", { name: "dedicated action" }),
    ).toBeInTheDocument();
  });
});
