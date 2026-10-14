import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Empty,
  Input,
  Modal,
  Select,
  Space,
  Tag,
  Typography,
} from "antd";
import { useTranslation } from "react-i18next";
import { Link, useLocation, useNavigate, useParams } from "react-router-dom";
import {
  agentComputerApi,
  type BoxCapabilities,
  type BoxCommandReceipt,
  type BoxProject,
  type BoxSource,
  type BoxTask,
  type BoxTaskListItem,
  type BoxTurn,
  type CreateProjectCommand,
  type CreateTaskCommand,
  type SendTurnCommand,
} from "../../api/modules/agentComputer";
import { bridgeApi, type BridgeConnection } from "../../api/modules/bridge";
import PageShell from "../../layouts/PageShell";
import { apiErrorMessage } from "../../utils/apiError";
import styles from "./index.module.less";

const TERMINAL_STATES = new Set([
  "succeeded",
  "failed",
  "stopped",
  "interrupted",
]);

function boxUrl(
  connectionId: string | null,
  projectID?: string,
  taskID?: string,
): string {
  const path = projectID
    ? `/box-projects/${encodeURIComponent(projectID)}${
        taskID ? `/tasks/${encodeURIComponent(taskID)}` : ""
      }`
    : "/box-projects";
  return connectionId
    ? `${path}?connection=${encodeURIComponent(connectionId)}`
    : path;
}

function mergeById<T>(latest: T[], older: T[], id: (item: T) => string): T[] {
  const seen = new Set(latest.map(id));
  return [...latest, ...older.filter((item) => !seen.has(id(item)))];
}

function statusColor(state: string): string {
  if (state === "succeeded" || state === "ready") return "success";
  if (state === "failed" || state === "interrupted") return "error";
  if (state === "waitingApproval" || state === "waitingInput") return "warning";
  return "processing";
}

function isDefiniteRejection(cause: unknown): boolean {
  return (
    cause instanceof Error &&
    /^Request failed: 4(?!08\b|29\b)\d\d\b/.test(cause.message)
  );
}

interface WorkspaceProps {
  source: BoxSource;
  connectionId: string | null;
  projectID?: string;
  taskID?: string;
  refreshRevision: number;
}

function BoxWorkspace({
  source,
  connectionId,
  projectID,
  taskID,
  refreshRevision,
}: WorkspaceProps) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const mounted = useRef(true);
  const [capabilities, setCapabilities] = useState<BoxCapabilities | null>(
    null,
  );
  const [projects, setProjects] = useState<BoxProject[]>([]);
  const [projectsCursor, setProjectsCursor] = useState<string | null>(null);
  const [project, setProject] = useState<BoxProject | null>(null);
  const [tasks, setTasks] = useState<BoxTaskListItem[]>([]);
  const [tasksCursor, setTasksCursor] = useState<string | null>(null);
  const [task, setTask] = useState<BoxTask | null>(null);
  const [turns, setTurns] = useState<BoxTurn[]>([]);
  const [turnsCursor, setTurnsCursor] = useState<string | null>(null);
  const [pendingRunID, setPendingRunID] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [projectLoading, setProjectLoading] = useState(false);
  const [taskLoading, setTaskLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [projectError, setProjectError] = useState<string | null>(null);
  const [taskError, setTaskError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [actionUncertain, setActionUncertain] = useState(false);
  const [busy, setBusy] = useState(false);
  const [projectModal, setProjectModal] = useState(false);
  const [taskModal, setTaskModal] = useState(false);
  const [projectName, setProjectName] = useState("");
  const [projectInstructions, setProjectInstructions] = useState("");
  const [taskTitle, setTaskTitle] = useState("");
  const [prompt, setPrompt] = useState("");
  const [pendingProject, setPendingProject] =
    useState<CreateProjectCommand | null>(null);
  const [pendingTask, setPendingTask] = useState<CreateTaskCommand | null>(
    null,
  );
  const [pendingTurn, setPendingTurn] = useState<SendTurnCommand | null>(null);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const stateLabel = useCallback(
    (state: string) => t(`boxProjects.states.${state}`, state),
    [t],
  );

  useEffect(() => {
    let current = true;
    setLoading(true);
    setError(null);
    setCapabilities(null);
    setProjects([]);
    setProjectsCursor(null);
    Promise.all([
      agentComputerApi.capabilities(source),
      agentComputerApi.projects(source),
    ])
      .then(([nextCapabilities, listing]) => {
        if (!current) return;
        setCapabilities(nextCapabilities);
        setProjects(listing.items);
        setProjectsCursor(listing.nextCursor);
      })
      .catch((cause: unknown) => {
        if (current)
          setError(apiErrorMessage(cause, t("boxProjects.status"), t));
      })
      .finally(() => {
        if (current) setLoading(false);
      });
    return () => {
      current = false;
    };
  }, [source, refreshRevision, t]);

  useEffect(() => {
    let current = true;
    setProject(null);
    setTasks([]);
    setTasksCursor(null);
    setProjectError(null);
    if (!projectID)
      return () => {
        current = false;
      };
    setProjectLoading(true);
    agentComputerApi
      .project(source, projectID)
      .then(async (verified) => {
        if (!current) return;
        setProject(verified);
        const listing = await agentComputerApi.tasks(source, projectID);
        if (!current) return;
        setTasks(listing.items);
        setTasksCursor(listing.nextCursor);
      })
      .catch((cause: unknown) => {
        if (current)
          setProjectError(apiErrorMessage(cause, t("boxProjects.status"), t));
      })
      .finally(() => {
        if (current) setProjectLoading(false);
      });
    return () => {
      current = false;
    };
  }, [source, projectID, refreshRevision, t]);

  useEffect(() => {
    let current = true;
    setTask(null);
    setTurns([]);
    setTurnsCursor(null);
    setTaskError(null);
    setPendingRunID(null);
    if (!projectID || !taskID)
      return () => {
        current = false;
      };
    setTaskLoading(true);
    agentComputerApi
      .task(source, taskID)
      .then(async (verified) => {
        if (verified.projectID !== projectID)
          throw new Error(t("boxProjects.taskMismatch"));
        if (!current) return;
        setTask(verified);
        const listing = await agentComputerApi.turns(source, taskID);
        if (!current) return;
        setTurns(listing.items);
        setTurnsCursor(listing.nextCursor);
      })
      .catch((cause: unknown) => {
        if (current)
          setTaskError(apiErrorMessage(cause, t("boxProjects.status"), t));
      })
      .finally(() => {
        if (current) setTaskLoading(false);
      });
    return () => {
      current = false;
    };
  }, [source, projectID, taskID, refreshRevision, t]);

  const refreshTurns = useCallback(async () => {
    if (!taskID) return;
    const listing = await agentComputerApi.turns(source, taskID);
    if (!mounted.current) return;
    setTurns((previous) =>
      mergeById(listing.items, previous, (item) => item.turnID),
    );
    setTurnsCursor((previous) => previous ?? listing.nextCursor);
    if (
      pendingRunID &&
      listing.items.some(
        (item) =>
          item.run.runID === pendingRunID &&
          TERMINAL_STATES.has(item.run.state),
      )
    ) {
      setPendingRunID(null);
    }
  }, [source, taskID, pendingRunID]);

  const needsPolling = Boolean(
    pendingRunID || turns.some((item) => !TERMINAL_STATES.has(item.run.state)),
  );
  useEffect(() => {
    if (!taskID || !needsPolling) return;
    const timer = window.setInterval(() => {
      void refreshTurns().catch(() => undefined);
    }, 3000);
    return () => window.clearInterval(timer);
  }, [taskID, needsPolling, refreshTurns]);

  async function submitOrRecover(
    commandID: string,
    matches: (receipt: BoxCommandReceipt) => boolean,
    submit: () => Promise<BoxCommandReceipt>,
  ): Promise<BoxCommandReceipt> {
    try {
      const receipt = await submit();
      if (!matches(receipt)) throw new Error(t("boxProjects.receiptMismatch"));
      return receipt;
    } catch (cause) {
      try {
        const receipt = await agentComputerApi.command(source, commandID);
        if (matches(receipt)) return receipt;
      } catch {
        // Keep the original command for an exact retry after an uncertain response.
      }
      throw cause;
    }
  }

  async function createProject() {
    if (
      !capabilities?.capabilities.projects.available ||
      !capabilities.textModelRef
    )
      return;
    const name = projectName.trim();
    if (!name && !pendingProject) return;
    const command: CreateProjectCommand = pendingProject ?? {
      commandID: crypto.randomUUID(),
      idempotencyKey: crypto.randomUUID(),
      expectedRestoreEpoch: capabilities.restoreEpoch,
      projectID: crypto.randomUUID(),
      name,
      configuration: {
        schemaVersion: 1,
        instructions: projectInstructions,
        modelRef: capabilities.textModelRef,
      },
    };
    setPendingProject(command);
    setBusy(true);
    setActionError(null);
    setActionUncertain(false);
    try {
      const receipt = await submitOrRecover(
        command.commandID,
        (value) =>
          value.accepted &&
          value.commandID === command.commandID &&
          value.operation === "project.create" &&
          value.projectID === command.projectID,
        () => agentComputerApi.createProject(source, command),
      );
      if (!mounted.current) return;
      setPendingProject(null);
      setProjectModal(false);
      setProjectName("");
      setProjectInstructions("");
      navigate(boxUrl(connectionId, receipt.projectID ?? command.projectID));
    } catch (cause) {
      if (mounted.current) {
        const uncertain = !isDefiniteRejection(cause);
        if (!uncertain) setPendingProject(null);
        setActionUncertain(uncertain);
        setActionError(
          apiErrorMessage(cause, t("boxProjects.unknownOutcome"), t),
        );
      }
    } finally {
      if (mounted.current) setBusy(false);
    }
  }

  async function createTask() {
    if (!capabilities?.capabilities.tasks.available || !project?.executionReady)
      return;
    const title = taskTitle.trim();
    if (!title && !pendingTask) return;
    const command: CreateTaskCommand = pendingTask ?? {
      commandID: crypto.randomUUID(),
      idempotencyKey: crypto.randomUUID(),
      expectedRestoreEpoch: capabilities.restoreEpoch,
      expectedProjectRevision: project.projectRevision,
      projectID: project.projectID,
      taskID: crypto.randomUUID(),
      title,
    };
    setPendingTask(command);
    setBusy(true);
    setActionError(null);
    setActionUncertain(false);
    try {
      const receipt = await submitOrRecover(
        command.commandID,
        (value) =>
          value.accepted &&
          value.commandID === command.commandID &&
          value.operation === "task.create" &&
          value.projectID === project.projectID &&
          value.taskID === command.taskID,
        () => agentComputerApi.createTask(source, project.projectID, command),
      );
      if (!mounted.current) return;
      setPendingTask(null);
      setTaskModal(false);
      setTaskTitle("");
      navigate(
        boxUrl(
          connectionId,
          project.projectID,
          receipt.taskID ?? command.taskID,
        ),
      );
    } catch (cause) {
      if (mounted.current) {
        const uncertain = !isDefiniteRejection(cause);
        if (!uncertain) setPendingTask(null);
        setActionUncertain(uncertain);
        setActionError(
          apiErrorMessage(cause, t("boxProjects.unknownOutcome"), t),
        );
      }
    } finally {
      if (mounted.current) setBusy(false);
    }
  }

  async function sendTurn() {
    if (!capabilities || !project || !task) return;
    const text = prompt.trim();
    if (!text && !pendingTurn) return;
    const command: SendTurnCommand = pendingTurn ?? {
      commandID: crypto.randomUUID(),
      idempotencyKey: crypto.randomUUID(),
      expectedRestoreEpoch: capabilities.restoreEpoch,
      contextMode: "newMessage",
      userMessageID: crypto.randomUUID(),
      expectedProjectRevision: project.projectRevision,
      prompt: text,
      inputArtifactIDs: [],
      selectedSkillIDs: [],
      permissionMode: "askForApproval",
    };
    setPendingTurn(command);
    setBusy(true);
    setActionError(null);
    setActionUncertain(false);
    try {
      const receipt = await submitOrRecover(
        command.commandID,
        (value) =>
          value.accepted &&
          value.commandID === command.commandID &&
          value.taskID === task.taskID &&
          Boolean(value.turnID && value.runID),
        () => agentComputerApi.sendTurn(source, task.taskID, command),
      );
      if (!mounted.current) return;
      setPendingTurn(null);
      setPrompt("");
      if (receipt.runID) setPendingRunID(receipt.runID);
      await refreshTurns().catch(() => undefined);
    } catch (cause) {
      if (mounted.current) {
        const uncertain = !isDefiniteRejection(cause);
        if (!uncertain) setPendingTurn(null);
        setActionUncertain(uncertain);
        setActionError(
          apiErrorMessage(cause, t("boxProjects.unknownOutcome"), t),
        );
      }
    } finally {
      if (mounted.current) setBusy(false);
    }
  }

  async function loadMoreProjects() {
    if (!projectsCursor) return;
    try {
      const listing = await agentComputerApi.projects(source, projectsCursor);
      setProjects((previous) =>
        mergeById(previous, listing.items, (item) => item.projectID),
      );
      setProjectsCursor(listing.nextCursor);
    } catch (cause) {
      setError(apiErrorMessage(cause, t("boxProjects.status"), t));
    }
  }

  async function loadMoreTasks() {
    if (!projectID || !tasksCursor) return;
    try {
      const listing = await agentComputerApi.tasks(
        source,
        projectID,
        tasksCursor,
      );
      setTasks((previous) =>
        mergeById(previous, listing.items, (item) => item.task.taskID),
      );
      setTasksCursor(listing.nextCursor);
    } catch (cause) {
      setProjectError(apiErrorMessage(cause, t("boxProjects.status"), t));
    }
  }

  async function loadOlderTurns() {
    if (!taskID || !turnsCursor) return;
    try {
      const listing = await agentComputerApi.turns(source, taskID, turnsCursor);
      setTurns((previous) =>
        mergeById(previous, listing.items, (item) => item.turnID),
      );
      setTurnsCursor(listing.nextCursor);
    } catch (cause) {
      setTaskError(apiErrorMessage(cause, t("boxProjects.status"), t));
    }
  }

  const canCreateProject = Boolean(
    capabilities?.capabilities.projects.available && capabilities.textModelRef,
  );
  const canCreateTask = Boolean(
    capabilities?.capabilities.tasks.available && project?.executionReady,
  );
  const canSend = Boolean(
    capabilities?.capabilities.runs.available &&
      project?.executionReady &&
      task?.executionReady &&
      capabilities.supportedPermissionModes.includes("askForApproval") &&
      (!needsPolling || pendingTurn !== null),
  );

  return (
    <>
      {error && (
        <Alert className={styles.alert} type="error" showIcon message={error} />
      )}
      {capabilities && !capabilities.capabilities.runs.available && (
        <Alert
          className={styles.alert}
          type="warning"
          showIcon
          message={t("boxProjects.capabilityUnavailable")}
        />
      )}
      <div className={styles.columns}>
        <Card
          className={styles.panel}
          title={t("boxProjects.projects")}
          extra={
            <Button
              size="small"
              disabled={!canCreateProject}
              onClick={() => setProjectModal(true)}
            >
              {t("boxProjects.createProject")}
            </Button>
          }
        >
          {loading ? (
            <Typography.Text>{t("boxProjects.loading")}</Typography.Text>
          ) : projects.length === 0 ? (
            <Empty description={t("boxProjects.noProjects")} />
          ) : (
            <div className={styles.list}>
              {projects.map((item) => (
                <button
                  className={`${styles.item} ${
                    item.projectID === projectID ? styles.active : ""
                  }`}
                  type="button"
                  key={item.projectID}
                  onClick={() => navigate(boxUrl(connectionId, item.projectID))}
                >
                  <span className={styles.itemTitle}>{item.name}</span>
                  <Tag color={statusColor(item.preparationState)}>
                    {stateLabel(item.preparationState)}
                  </Tag>
                </button>
              ))}
            </div>
          )}
          {projectsCursor && (
            <Button onClick={() => void loadMoreProjects()}>
              {t("boxProjects.loadMore")}
            </Button>
          )}
        </Card>

        <Card
          className={styles.panel}
          title={project?.name ?? t("boxProjects.tasks")}
          extra={
            project && (
              <Button
                size="small"
                disabled={!canCreateTask}
                onClick={() => setTaskModal(true)}
              >
                {t("boxProjects.createTask")}
              </Button>
            )
          }
        >
          {!projectID ? (
            <Empty description={t("boxProjects.chooseProject")} />
          ) : projectLoading ? (
            <Typography.Text>{t("boxProjects.loading")}</Typography.Text>
          ) : projectError ? (
            <Alert type="error" showIcon message={projectError} />
          ) : (
            <>
              {project && (
                <div className={styles.meta}>
                  <Tag color={statusColor(project.preparationState)}>
                    {stateLabel(project.preparationState)}
                  </Tag>
                  {project.reasonCode && (
                    <Typography.Text type="secondary">
                      {project.reasonCode}
                    </Typography.Text>
                  )}
                </div>
              )}
              {tasks.length === 0 ? (
                <Empty description={t("boxProjects.noTasks")} />
              ) : (
                <div className={styles.list}>
                  {tasks.map((item) => (
                    <button
                      className={`${styles.item} ${
                        item.task.taskID === taskID ? styles.active : ""
                      }`}
                      type="button"
                      key={item.task.taskID}
                      onClick={() =>
                        navigate(
                          boxUrl(
                            connectionId,
                            item.task.projectID,
                            item.task.taskID,
                          ),
                        )
                      }
                    >
                      <span className={styles.itemTitle}>
                        {item.task.title}
                      </span>
                      {item.latestRun && (
                        <Tag color={statusColor(item.latestRun.state)}>
                          {stateLabel(item.latestRun.state)}
                        </Tag>
                      )}
                    </button>
                  ))}
                </div>
              )}
              {tasksCursor && (
                <Button onClick={() => void loadMoreTasks()}>
                  {t("boxProjects.loadMore")}
                </Button>
              )}
            </>
          )}
        </Card>

        <Card
          className={`${styles.panel} ${styles.conversation}`}
          title={task?.title ?? t("boxProjects.conversation")}
        >
          {!taskID ? (
            <Empty description={t("boxProjects.chooseTask")} />
          ) : taskLoading ? (
            <Typography.Text>{t("boxProjects.loading")}</Typography.Text>
          ) : taskError ? (
            <Alert type="error" showIcon message={taskError} />
          ) : (
            <>
              {turnsCursor && (
                <Button onClick={() => void loadOlderTurns()}>
                  {t("boxProjects.loadMore")}
                </Button>
              )}
              {turns.length === 0 && (
                <Empty description={t("boxProjects.conversation")} />
              )}
              <div className={styles.messages}>
                {[...turns].reverse().map((item) => (
                  <div className={styles.turn} key={item.turnID}>
                    <div className={styles.userMessage}>{item.prompt}</div>
                    <div className={styles.assistantMessage}>
                      {item.result.text ?? (
                        <Tag color={statusColor(item.run.state)}>
                          {stateLabel(item.run.state)}
                        </Tag>
                      )}
                    </div>
                  </div>
                ))}
              </div>
              {!canSend && (
                <Typography.Text type="secondary">
                  {t("boxProjects.readOnly")}
                </Typography.Text>
              )}
              <div className={styles.composer}>
                <Input.TextArea
                  value={pendingTurn?.prompt ?? prompt}
                  rows={3}
                  disabled={!canSend || busy || pendingTurn !== null}
                  placeholder={t("boxProjects.promptPlaceholder")}
                  onChange={(event) => setPrompt(event.target.value)}
                />
                <Button
                  type="primary"
                  loading={busy}
                  disabled={!canSend || (!prompt.trim() && !pendingTurn)}
                  onClick={() => void sendTurn()}
                >
                  {pendingTurn ? t("boxProjects.retry") : t("boxProjects.send")}
                </Button>
              </div>
            </>
          )}
        </Card>
      </div>
      {actionError && (
        <Alert
          className={styles.alert}
          type="warning"
          showIcon
          message={actionError}
          description={
            actionUncertain ? t("boxProjects.unknownOutcome") : undefined
          }
        />
      )}

      <Modal
        title={t("boxProjects.createProject")}
        open={projectModal}
        okText={
          pendingProject ? t("boxProjects.retry") : t("boxProjects.create")
        }
        okButtonProps={{
          disabled: !projectName.trim() && !pendingProject,
          loading: busy,
        }}
        onOk={() => void createProject()}
        onCancel={() => setProjectModal(false)}
      >
        <Space direction="vertical" className={styles.form}>
          <Input
            maxLength={128}
            value={pendingProject?.name ?? projectName}
            disabled={pendingProject !== null}
            placeholder={t("boxProjects.projectName")}
            onChange={(event) => setProjectName(event.target.value)}
          />
          <Input.TextArea
            maxLength={8192}
            rows={4}
            value={
              pendingProject?.configuration.instructions ?? projectInstructions
            }
            disabled={pendingProject !== null}
            placeholder={t("boxProjects.projectInstructions")}
            onChange={(event) => setProjectInstructions(event.target.value)}
          />
        </Space>
      </Modal>
      <Modal
        title={t("boxProjects.createTask")}
        open={taskModal}
        okText={pendingTask ? t("boxProjects.retry") : t("boxProjects.create")}
        okButtonProps={{
          disabled: !taskTitle.trim() && !pendingTask,
          loading: busy,
        }}
        onOk={() => void createTask()}
        onCancel={() => setTaskModal(false)}
      >
        <Input
          maxLength={256}
          value={pendingTask?.title ?? taskTitle}
          disabled={pendingTask !== null}
          placeholder={t("boxProjects.taskTitle")}
          onChange={(event) => setTaskTitle(event.target.value)}
        />
      </Modal>
    </>
  );
}

export default function BoxProjectsPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const location = useLocation();
  const { projectID, taskID } = useParams<{
    projectID?: string;
    taskID?: string;
  }>();
  const connectionId = new URLSearchParams(location.search).get("connection");
  const source = useMemo<BoxSource>(() => ({ connectionId }), [connectionId]);
  const [connections, setConnections] = useState<BridgeConnection[]>([]);
  const [connectionsError, setConnectionsError] = useState<string | null>(null);
  const [refreshRevision, setRefreshRevision] = useState(0);

  useEffect(() => {
    let current = true;
    bridgeApi
      .list()
      .then((rows) => {
        if (current) setConnections(rows.filter((row) => !row.inbound));
      })
      .catch((cause: unknown) => {
        if (current)
          setConnectionsError(
            apiErrorMessage(cause, t("boxProjects.status"), t),
          );
      });
    return () => {
      current = false;
    };
  }, [t]);

  return (
    <PageShell
      title={t("boxProjects.title")}
      subtitle={t("boxProjects.subtitle")}
      actions={
        <Space wrap>
          <Select
            className={styles.sourceSelect}
            aria-label={t("boxProjects.source")}
            value={connectionId ?? "local"}
            onChange={(value) =>
              navigate(boxUrl(value === "local" ? null : value))
            }
            options={[
              { value: "local", label: t("boxProjects.local") },
              ...connections.map((row) => ({
                value: row.connection_id,
                label: row.display_name,
              })),
            ]}
          />
          <Button onClick={() => setRefreshRevision((value) => value + 1)}>
            {t("boxProjects.refresh")}
          </Button>
        </Space>
      }
    >
      {connectionsError && (
        <Alert
          className={styles.alert}
          type="warning"
          showIcon
          message={connectionsError}
        />
      )}
      {connections.length === 0 && (
        <div className={styles.connectionHint}>
          <Typography.Text type="secondary">
            {t("boxProjects.remoteConnection")}
          </Typography.Text>{" "}
          <Link to="/bridge">{t("boxProjects.addConnection")}</Link>
        </div>
      )}
      <BoxWorkspace
        key={[connectionId ?? "local", projectID ?? "", taskID ?? ""].join(":")}
        source={source}
        connectionId={connectionId}
        projectID={projectID}
        taskID={taskID}
        refreshRevision={refreshRevision}
      />
    </PageShell>
  );
}
