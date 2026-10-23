import { useTaskHost } from "../../context/TaskHostContext";
import {
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { Alert, Button, Drawer, Modal, Spin, Tabs, Typography } from "antd";
import { PanelsTopLeft, RefreshCw, X } from "lucide-react";
import { useTranslation } from "react-i18next";
import { type TaskWorkbenchScope } from "../../api/modules/taskWorkbench";
import type { TaskDetail } from "../../api/modules/projects";
import { apiErrorMessage } from "../../utils/apiError";
import styles from "./TaskWorkbench.module.less";
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
  docked = false,
  projectRevision,
  launcherClassName,
  onDirtyChange,
}: {
  task: TaskDetail;
  requestedTool?: { callID: string; sequence: number } | null;
  docked?: boolean;
  projectRevision?: number;
  launcherClassName?: string;
  onDirtyChange?: (dirty: boolean) => void;
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
  const [fileVersion, setFileVersion] = useState(0);
  const [scopeChanged, setScopeChanged] = useState(false);
  useEffect(() => {
    onDirtyChange?.(filesDirty);
    return () => onDirtyChange?.(false);
  }, [filesDirty, onDirtyChange]);
  const configurationKey = `${task.configuration_revision}:${projectRevision}:${task.archived}`;
  const observedConfiguration = useRef(configurationKey);
  const toolRequest = useRef<typeof requestedTool>(null);
  const leaveFiles = useCallback(
    (next: () => void) => {
      if (!filesDirty) {
        next();
        return;
      }
      Modal.confirm({
        title: t("projects.tasks.workbench.unsaved"),
        content: t("projects.tasks.workbench.discardHint"),
        okText: t("projects.tasks.workbench.discard"),
        cancelText: t("projects.cancel"),
        onOk: () => {
          setFilesDirty(false);
          setFileVersion((current) => current + 1);
          next();
        },
      });
    },
    [filesDirty, t],
  );
  useEffect(() => {
    if (requestedTool && toolRequest.current !== requestedTool) {
      toolRequest.current = requestedTool;
      leaveFiles(() => {
        setCallID(requestedTool.callID);
        setActive("tools");
        setOpen(true);
      });
    }
  }, [requestedTool, leaveFiles]);
  useEffect(() => {
    if (observedConfiguration.current === configurationKey) return;
    observedConfiguration.current = configurationKey;
    if (!open) return;
    if (filesDirty) setScopeChanged(true);
    else setVersion((current) => current + 1);
  }, [configurationKey, open, filesDirty]);
  useEffect(() => {
    if (!open) {
      setScope(null);
      setScopeChanged(false);
      return;
    }
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    void taskWorkbenchApi
      .scope(task.task_id, controller.signal)
      .then((value) => {
        if (!controller.signal.aborted) {
          setScope(value);
          setScopeChanged(false);
        }
      })
      .catch((err: unknown) => {
        if (!controller.signal.aborted) {
          setScope(null);
          setError(apiErrorMessage(err, t("projects.tasks.failure"), t));
        }
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
          children: (
            <TaskFiles
              key={`${scope.scope_revision}:${fileVersion}`}
              scope={scope}
              onDirtyChange={setFilesDirty}
            />
          ),
        },
        scope.capabilities.browser &&
          !scope.read_only && {
            key: "browser",
            label: t("projects.tasks.workbench.browser"),
            children: (
              <div className={styles.browser}>
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
  const close = () => leaveFiles(() => setOpen(false));
  const refresh = () =>
    leaveFiles(() => {
      setFileVersion((current) => current + 1);
      setVersion((current) => current + 1);
    });
  const refreshButton = (
    <Button
      size="small"
      type={docked ? "text" : "default"}
      icon={<RefreshCw size={14} />}
      loading={loading}
      aria-label={t("projects.tasks.workbench.refresh")}
      onClick={refresh}
    >
      {!docked && t("projects.tasks.workbench.refresh")}
    </Button>
  );
  const body = (
    <div className={styles.body}>
      {loading && <Spin className={styles.loading} />}
      {error && <Alert type="error" showIcon message={error} />}
      {scopeChanged && (
        <Alert
          type="warning"
          showIcon
          message={t("projects.workspace.workbenchScopeChanged")}
          action={
            <Button size="small" onClick={refresh}>
              {t("projects.tasks.workbench.refresh")}
            </Button>
          }
        />
      )}
      {scope?.task_id === task.task_id && (
        <>
          <Typography.Text type="secondary" className={styles.range}>
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
              key={scope.scope_revision}
              className={styles.tabs}
              size={docked ? "small" : "middle"}
              activeKey={
                tabs.some((tab) => tab.key === active) ? active : tabs[0]?.key
              }
              onChange={(next) => leaveFiles(() => setActive(next))}
              items={tabs}
            />
          </Suspense>
        </>
      )}
    </div>
  );
  return (
    <>
      <Button
        className={launcherClassName}
        size="small"
        type={docked ? "text" : "default"}
        icon={<PanelsTopLeft size={15} />}
        aria-pressed={open}
        onClick={() => (open ? close() : setOpen(true))}
      >
        {t("projects.tasks.workbench.title")}
      </Button>
      {docked ? (
        <aside
          data-task-workbench-docked
          hidden={!open}
          className={styles.docked}
          aria-label={t("projects.tasks.workbench.title")}
        >
          <div className={styles.header}>
            <Typography.Text strong>
              {t("projects.tasks.workbench.title")}
            </Typography.Text>
            <div>
              {refreshButton}
              <Button
                size="small"
                type="text"
                icon={<X size={15} />}
                aria-label={t("common.close")}
                onClick={close}
              />
            </div>
          </div>
          {body}
        </aside>
      ) : (
        <Drawer
          open={open}
          onClose={close}
          title={t("projects.tasks.workbench.title")}
          width="min(1180px, 96vw)"
          destroyOnHidden
          extra={refreshButton}
        >
          {body}
        </Drawer>
      )}
    </>
  );
}
