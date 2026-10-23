import { useTaskHost } from "../../context/TaskHostContext";
import { lazy, Suspense, useEffect, useState } from "react";
import {
  Alert,
  Button,
  Drawer,
  Modal,
  Space,
  Spin,
  Tabs,
  Typography,
} from "antd";
import { PanelsTopLeft, RefreshCw } from "lucide-react";
import { useTranslation } from "react-i18next";
import { type TaskWorkbenchScope } from "../../api/modules/taskWorkbench";
import type { TaskDetail } from "../../api/modules/projects";
import { apiErrorMessage } from "../../utils/apiError";
const TaskFiles = lazy(() => import("./TaskFiles"));
const BrowserWorkspace = lazy(
  () => import("../../components/BrowserWorkspace"),
);
const TaskTerminal = lazy(() => import("./TaskTerminal"));
const TaskSkillRecording = lazy(() => import("./TaskSkillRecording"));
const TaskToolUi = lazy(() => import("./TaskToolUi"));

export default function TaskWorkbench({
  task,
  requestedTool,
}: {
  task: TaskDetail;
  requestedTool?: { callID: string; sequence: number } | null;
}) {
  const { taskWorkbenchApi, hostID } = useTaskHost();
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [scope, setScope] = useState<TaskWorkbenchScope | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [active, setActive] = useState("files");
  const [callID, setCallID] = useState<string>();
  const [version, setVersion] = useState(0);
  const [filesDirty, setFilesDirty] = useState(false);
  const leaveFiles = (next: () => void) => {
    if (!filesDirty) {
      next();
      return;
    }
    Modal.confirm({
      title: t("projects.tasks.workbench.unsaved"),
      content: t("projects.tasks.workbench.discardHint"),
      okText: t("projects.tasks.workbench.discard"),
      cancelText: t("projects.cancel"),
      onOk: next,
    });
  };
  useEffect(() => {
    if (requestedTool) {
      setCallID(requestedTool.callID);
      setActive("tools");
      setOpen(true);
    }
  }, [requestedTool]);
  useEffect(() => {
    if (!open) {
      setScope(null);
      return;
    }
    const controller = new AbortController();
    setLoading(true);
    setScope(null);
    setError(null);
    void taskWorkbenchApi
      .scope(task.task_id, controller.signal)
      .then((value) => {
        if (!controller.signal.aborted) setScope(value);
      })
      .catch((err: unknown) => {
        if (!controller.signal.aborted)
          setError(apiErrorMessage(err, t("projects.tasks.failure"), t));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [open, task.task_id, version, t, taskWorkbenchApi]);
  const tabs = scope
    ? [
        scope.capabilities.files && {
          key: "files",
          label: t("projects.tasks.workbench.files"),
          children: <TaskFiles scope={scope} onDirtyChange={setFilesDirty} />,
        },
        scope.capabilities.browser &&
          !scope.read_only && {
            key: "browser",
            label: t("projects.tasks.workbench.browser"),
            children: (
              <div style={{ height: 620 }}>
                <BrowserWorkspace
                  taskHostID={hostID}
                  taskID={scope.task_id}
                  scopeRevision={scope.scope_revision}
                />
              </div>
            ),
          },
        scope.capabilities.terminal &&
          !scope.read_only && {
            key: "terminal",
            label: t("projects.tasks.workbench.terminal"),
            children: <TaskTerminal scope={scope} />,
          },
        scope.capabilities.recording &&
          !scope.read_only && {
            key: "recording",
            label: t("projects.tasks.workbench.skillRecording"),
            children: (
              <TaskSkillRecording
                scope={scope}
                hasProject={!!task.project_id}
              />
            ),
          },
        scope.capabilities.tool_ui && {
          key: "tools",
          label: t("projects.tasks.workbench.tools"),
          children: (
            <TaskToolUi
              scope={scope}
              task={task}
              selectedCallID={callID}
              onSelectCall={setCallID}
              onClose={() => leaveFiles(() => setOpen(false))}
            />
          ),
        },
      ].filter((tab): tab is Exclude<typeof tab, false> => !!tab)
    : [];
  return (
    <>
      <Button
        size="small"
        icon={<PanelsTopLeft size={15} />}
        onClick={() => setOpen(true)}
      >
        {t("projects.tasks.workbench.title")}
      </Button>
      <Drawer
        open={open}
        onClose={() => leaveFiles(() => setOpen(false))}
        title={t("projects.tasks.workbench.title")}
        width="min(1180px, 96vw)"
        destroyOnHidden
        extra={
          <Button
            size="small"
            icon={<RefreshCw size={14} />}
            onClick={() => leaveFiles(() => setVersion((v) => v + 1))}
          >
            {t("projects.tasks.workbench.refresh")}
          </Button>
        }
      >
        {loading ? (
          <Spin />
        ) : error ? (
          <Alert type="error" showIcon message={error} />
        ) : (
          scope && (
            <Space direction="vertical" style={{ width: "100%" }} size="middle">
              <Typography.Text type="secondary">
                {t("projects.tasks.directoryRange", {
                  directory:
                    scope.server_directory ?? t("projects.tasks.taskSpace"),
                  clientDirectory:
                    scope.client_directory ?? t("projects.tasks.notConfigured"),
                })}
              </Typography.Text>
              {scope.read_only && (
                <Alert
                  type="info"
                  showIcon
                  message={t("projects.tasks.workbench.readOnly")}
                />
              )}
              <Suspense fallback={<Spin />}>
                <Tabs
                  activeKey={
                    tabs.some((tab) => tab.key === active)
                      ? active
                      : tabs[0]?.key
                  }
                  onChange={setActive}
                  items={tabs}
                />
              </Suspense>
            </Space>
          )
        )}
      </Drawer>
    </>
  );
}
