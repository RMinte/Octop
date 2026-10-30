import { randomUUID } from "../../utils/randomUUID";
import { useTaskHost } from "../../context/TaskHostContext";
import { useEffect, useRef, useState } from "react";
import { Alert, Button, Modal, Space, Typography } from "antd";
import { useTranslation } from "react-i18next";
import { type TaskWorkbenchScope } from "../../api/modules/taskWorkbench";
import TerminalView, {
  type TerminalViewHandle,
} from "../Control/Terminal/components/TerminalView";
import { getTerminalTheme } from "../Control/Terminal/terminalThemes";
import { apiErrorMessage } from "../../utils/apiError";

export default function TaskTerminal({ scope }: { scope: TaskWorkbenchScope }) {
  const { taskWorkbenchApi, hostID } = useTaskHost();
  const { t } = useTranslation();
  const storageKey = `octop:task-terminal:${hostID ?? "local"}:${
    scope.task_id
  }:${scope.scope_revision}`;
  const [sessionID, setSessionID] = useState(
    () => sessionStorage.getItem(storageKey) ?? "",
  );
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [ending, setEnding] = useState(false);
  const [version, setVersion] = useState(0);
  const terminal = useRef<TerminalViewHandle | null>(null);
  const socket = useRef<WebSocket | null>(null);
  const pending = useRef("");
  const dimensions = useRef({ cols: 100, rows: 24 });
  const send = (body: object) => {
    if (socket.current?.readyState === WebSocket.OPEN)
      socket.current.send(JSON.stringify(body));
  };
  useEffect(() => {
    if (!sessionID || scope.read_only) return;
    const ws = new WebSocket(
      taskWorkbenchApi.terminalUrl(
        scope.task_id,
        sessionID,
        scope.scope_revision,
      ),
    );
    socket.current = ws;
    ws.onopen = () => {
      setConnected(true);
      setError(null);
      ws.send(JSON.stringify({ type: "resize", ...dimensions.current }));
    };
    ws.onmessage = (event) => {
      try {
        const body = JSON.parse(String(event.data)) as {
          type: string;
          data?: string;
          message?: string;
          code?: number;
        };
        if (body.type === "output" || body.type === "history") {
          if (terminal.current) terminal.current.write(body.data ?? "");
          else pending.current += body.data ?? "";
        }
        if (body.type === "error")
          setError(body.message ?? t("projects.tasks.failure"));
        if (body.type === "exit") {
          setConnected(false);
          setError(
            t("projects.tasks.workbench.terminalExited", { code: body.code }),
          );
          sessionStorage.removeItem(storageKey);
          setSessionID("");
        }
      } catch {
        setError(t("projects.tasks.failure"));
      }
    };
    ws.onerror = () =>
      setError(t("projects.tasks.workbench.terminalDisconnected"));
    ws.onclose = () => {
      setConnected(false);
    };
    return () => {
      ws.onopen = null;
      ws.onmessage = null;
      ws.onerror = null;
      ws.onclose = null;
      ws.close();
      if (socket.current === ws) socket.current = null;
    };
  }, [
    scope.task_id,
    scope.scope_revision,
    scope.read_only,
    sessionID,
    storageKey,
    version,
    t,
    taskWorkbenchApi,
  ]);
  const start = () => {
    const next = randomUUID();
    sessionStorage.setItem(storageKey, next);
    terminal.current?.reset();
    pending.current = "";
    setError(null);
    setSessionID(next);
  };
  const end = () =>
    Modal.confirm({
      title: t("projects.tasks.workbench.endTerminal"),
      content: t("projects.tasks.workbench.endTerminalConfirm"),
      okButtonProps: { danger: true },
      cancelText: t("projects.cancel"),
      onOk: async () => {
        setEnding(true);
        try {
          await taskWorkbenchApi.closeTerminal(
            scope.task_id,
            sessionID,
            scope.scope_revision,
          );
          socket.current?.close();
          sessionStorage.removeItem(storageKey);
          setSessionID("");
          setConnected(false);
          terminal.current?.reset();
        } catch (err) {
          setError(apiErrorMessage(err, t("projects.tasks.failure"), t));
        } finally {
          setEnding(false);
        }
      },
    });
  return (
    <Space direction="vertical" style={{ width: "100%" }}>
      <Typography.Paragraph type="secondary">
        {t("projects.tasks.workbench.terminalHint")}
      </Typography.Paragraph>
      {error && <Alert type="warning" showIcon message={error} />}
      <Space>
        <Button onClick={start} disabled={!!sessionID || scope.read_only}>
          {t("projects.tasks.workbench.openTerminal")}
        </Button>
        {sessionID && (
          <>
            <Button
              disabled={connected || ending || scope.read_only}
              onClick={() => {
                terminal.current?.reset();
                pending.current = "";
                setVersion((v) => v + 1);
              }}
            >
              {t("projects.tasks.workbench.reconnect")}
            </Button>
            <Button danger loading={ending} onClick={end}>
              {t("projects.tasks.workbench.endTerminal")}
            </Button>
          </>
        )}
      </Space>
      {!!sessionID && (
        <div style={{ height: 420 }}>
          <TerminalView
            terminalRef={terminal}
            themeDefinition={getTerminalTheme(null, true)}
            onReady={() => {
              terminal.current?.write(pending.current);
              pending.current = "";
            }}
            onData={(data) => send({ type: "input", data })}
            onResize={(cols, rows) => {
              dimensions.current = { cols, rows };
              send({ type: "resize", cols, rows });
            }}
          />
        </div>
      )}
    </Space>
  );
}
