import { useTaskHost } from "../../context/TaskHostContext";
import { apiErrorMessage } from "../../utils/apiError";
import { useEffect, useMemo, useRef, useState } from "react";
import { Alert, Empty, Select, Space, Typography } from "antd";
import { useTranslation } from "react-i18next";
import type { TaskContentBlock, TaskDetail } from "../../api/modules/projects";
import { pluginsApi } from "../../api/modules/plugins";
import {
  taskToolRequest,
  type TaskWorkbenchScope,
} from "../../api/modules/taskWorkbench";
import {
  ensureBuiltinToolRenderers,
  parseOctopToolOutput,
  resolvePluginUiData,
  resolveToolRenderer,
} from "../../plugins/toolRenderers";
import { loadScopedPluginToolUis } from "../../plugins/toolRenderers/loader";
import type {
  OctopPluginUIHost,
  ToolRendererRegistration,
  ToolRenderProps,
} from "../../plugins/toolRenderers/types";
import { ToolUiErrorBoundary } from "../../plugins/toolRenderers/ToolUiErrorBoundary";

interface Call {
  block: TaskContentBlock;
  turnID: string;
  history: boolean;
}
export default function TaskToolUi({
  scope,
  task,
  selectedCallID,
  onSelectCall,
  onClose,
}: {
  scope: TaskWorkbenchScope;
  task: TaskDetail;
  selectedCallID?: string;
  onSelectCall: (id: string) => void;
  onClose?: () => void;
}) {
  const { hostID } = useTaskHost();
  const { t, i18n } = useTranslation();
  const calls = useMemo(
    () =>
      task.turns.flatMap((turn) =>
        turn.content_blocks
          .filter((block) => block.type === "tool")
          .map(
            (block): Call => ({
              block,
              turnID: turn.turn_id,
              history: !!turn.history_only,
            }),
          ),
      ),
    [task],
  );
  const [registrations, setRegistrations] = useState<
    ToolRendererRegistration[]
  >([]);
  const [patches, setPatches] = useState<Record<string, unknown>>({});
  const [pluginByTool, setPluginByTool] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);
  const current =
    calls.find(
      (call) =>
        (call.block.tool_call_id ?? call.block.block_id) === selectedCallID,
    ) ?? calls[calls.length - 1];
  const cfg = useRef({
    scope,
    hostID,
    current,
    onSelectCall,
    onClose,
    locale: i18n.language,
    unsupported: t("projects.tasks.workbench.toolRequestUnsupported"),
  });
  cfg.current = {
    scope,
    hostID,
    current,
    onSelectCall,
    onClose,
    locale: i18n.language,
    unsupported: t("projects.tasks.workbench.toolRequestUnsupported"),
  };
  const hostFactory = useMemo(
    () =>
      (pluginId: string): OctopPluginUIHost => ({
        registerRenderer: (reg) =>
          setRegistrations((previous) => [
            ...previous.filter(
              (item) => !(item.pluginId === pluginId && item.id === reg.id),
            ),
            { ...reg, pluginId },
          ]),
        getToolContext: () => ({
          agentId: null,
          threadId: null,
          taskId: cfg.current.scope.task_id,
          turnId: cfg.current.current?.turnID,
          locale: cfg.current.locale,
          theme:
            document.documentElement.getAttribute("data-theme") === "dark"
              ? "dark"
              : "light",
        }),
        patchResult: (callId, nextData) => {
          const ownID =
            cfg.current.current?.block.tool_call_id ??
            cfg.current.current?.block.block_id;
          if (callId === ownID)
            setPatches((previous) => ({ ...previous, [callId]: nextData }));
        },
        request: async <T,>(requestedPath: string, init?: RequestInit) => {
          const context = cfg.current;
          try {
            return await taskToolRequest<T>(
              {
                hostID: context.hostID,
                taskID: context.scope.task_id,
                turnID: context.current?.turnID ?? "",
                callID: context.current?.block.tool_call_id ?? "",
                scopeRevision: context.scope.scope_revision,
                readOnly: context.scope.read_only || !!context.current?.history,
              },
              requestedPath,
              init,
            );
          } catch (err) {
            if (
              err instanceof Error &&
              err.message === "TASK_TOOL_REQUEST_UNSUPPORTED"
            ) {
              setError(context.unsupported);
              throw new Error(context.unsupported);
            }
            setError(apiErrorMessage(err, context.unsupported));
            throw err;
          }
        },
        openSidePanel: ({ callId }) => cfg.current.onSelectCall(callId),
        closeSidePanel: () => cfg.current.onClose?.(),
      }),
    [],
  );
  useEffect(() => {
    let cancelled = false;
    ensureBuiltinToolRenderers();
    if (hostID) return;
    void pluginsApi
      .list()
      .then((plugins) => {
        if (cancelled) return;
        setPluginByTool(
          Object.fromEntries(
            plugins
              .filter((plugin) => !plugin.error)
              .flatMap((plugin) =>
                (plugin.tools ?? []).map((tool) => [tool.name, plugin.id]),
              ),
          ),
        );
        return loadScopedPluginToolUis(plugins, (id) => {
          const host = hostFactory(id);
          return {
            ...host,
            registerRenderer: (reg) => {
              if (!cancelled) host.registerRenderer(reg);
            },
          };
        });
      })
      .catch(() => {
        if (!cancelled)
          setError(t("projects.tasks.workbench.pluginLoadFailed"));
      });
    return () => {
      cancelled = true;
    };
  }, [hostFactory, scope.task_id, t, hostID]);
  if (!current)
    return <Empty description={t("projects.tasks.workbench.noTools")} />;
  const { block } = current;
  const callID = block.tool_call_id ?? block.block_id;
  const parsed = parseOctopToolOutput(block.output);
  const pluginID = pluginByTool[block.tool_name ?? ""];
  const registration =
    (pluginID
      ? registrations.find(
          (reg) =>
            reg.pluginId === pluginID &&
            (parsed.octopUi?.renderer
              ? reg.id === parsed.octopUi.renderer
              : reg.tools?.includes(block.tool_name ?? "")),
        )
      : null) ??
    (() => {
      const candidate = resolveToolRenderer({
        toolName: block.tool_name ?? undefined,
        pluginId: "builtin",
        parsed,
      });
      return candidate?.pluginId === "builtin" ? candidate : null;
    })();
  const props: ToolRenderProps = {
    pluginId: registration?.pluginId ?? "builtin",
    toolName: block.tool_name ?? "",
    displayName: block.tool_label ?? undefined,
    callId: callID,
    status:
      block.status === "failed"
        ? "error"
        : block.status === "streaming"
        ? "running"
        : "done",
    args: block.arguments,
    data: Object.prototype.hasOwnProperty.call(patches, callID)
      ? patches[callID]
      : resolvePluginUiData(
          parsed,
          undefined,
          typeof block.output === "string" ? block.output : undefined,
        ),
    textFallback: parsed.text,
    output:
      typeof block.output === "string"
        ? block.output
        : JSON.stringify(block.output),
    host: hostFactory(registration?.pluginId ?? "builtin"),
    agentId: null,
    isStreaming: block.status === "streaming",
    hideMediaPreview: true,
  };
  const Renderer = registration?.component;
  return (
    <Space direction="vertical" style={{ width: "100%" }} size="middle">
      <Select
        style={{ width: "100%" }}
        aria-label={t("projects.tasks.workbench.toolCall")}
        value={callID}
        onChange={onSelectCall}
        options={calls.map(({ block: item }) => ({
          value: item.tool_call_id ?? item.block_id,
          label: item.tool_label ?? item.tool_name ?? item.block_id,
        }))}
      />
      {error && (
        <Alert
          type="warning"
          showIcon
          message={error}
          closable
          onClose={() => setError(null)}
        />
      )}
      {block.speaker_name && (
        <Typography.Text type="secondary">{block.speaker_name}</Typography.Text>
      )}
      {Renderer ? (
        <ToolUiErrorBoundary key={callID} propsForFallback={props}>
          <Renderer {...props} />
        </ToolUiErrorBoundary>
      ) : (
        <>
          <Typography.Text strong>
            {t("projects.tasks.actionDetails.arguments")}
          </Typography.Text>
          <pre style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
            {JSON.stringify(block.arguments, null, 2)}
          </pre>
          <Typography.Text strong>
            {t("projects.tasks.actionDetails.output")}
          </Typography.Text>
          <pre style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
            {typeof block.output === "string"
              ? block.output
              : JSON.stringify(block.output, null, 2)}
          </pre>
        </>
      )}
    </Space>
  );
}
