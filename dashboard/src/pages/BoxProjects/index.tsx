import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Empty,
  Form,
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
  type BoxPage,
  type BoxProject,
  type BoxRunOutputSnapshot,
  type BoxRunToolActivity,
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
const DOCUMENT_LIMIT_BYTES = 16_384;

function validDocumentFilename(filename: string): boolean {
  return (
    filename.length >= 1 &&
    filename.length <= 128 &&
    !filename.startsWith(".") &&
    /\.(md|txt)$/.test(filename) &&
    /^[\p{L}\p{N}._-]+$/u.test(filename)
  );
}

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
  const [runOutput, setRunOutput] = useState<BoxRunOutputSnapshot | null>(null);
  const [runOutputError, setRunOutputError] = useState<string | null>(null);
  const [turnRefreshError, setTurnRefreshError] = useState<string | null>(null);
  const [expandedToolRunID, setExpandedToolRunID] = useState<string | null>(
    null,
  );
  const [toolActivityByRun, setToolActivityByRun] = useState<
    Record<string, BoxRunToolActivity>
  >({});
  const [toolActivityLoading, setToolActivityLoading] = useState(false);
  const [toolActivityError, setToolActivityError] = useState<string | null>(
    null,
  );
  const [documents, setDocuments] = useState<string[]>([]);
  const [documentsLoading, setDocumentsLoading] = useState(false);
  const [documentsRevision, setDocumentsRevision] = useState(0);
  const [documentError, setDocumentError] = useState<string | null>(null);
  const [selectedDocument, setSelectedDocument] = useState<{
    filename: string;
    content: string;
  } | null>(null);
  const [documentLoading, setDocumentLoading] = useState(false);
  const [documentModal, setDocumentModal] = useState(false);
  const [documentFilename, setDocumentFilename] = useState("");
  const [documentContent, setDocumentContent] = useState("");
  const [documentActionError, setDocumentActionError] = useState<string | null>(
    null,
  );
  const [documentOutcomeUncertain, setDocumentOutcomeUncertain] =
    useState(false);
  const [documentChecking, setDocumentChecking] = useState(false);
  const [documentSaving, setDocumentSaving] = useState(false);
  const documentRequestID = useRef(0);
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
        setProjects((previous) =>
          previous.some((item) => item.projectID === verified.projectID)
            ? previous.map((item) =>
                item.projectID === verified.projectID ? verified : item,
              )
            : [verified, ...previous],
        );
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
    if (
      !projectID ||
      project?.projectID !== projectID ||
      (project.preparationState !== "pending" &&
        project.preparationState !== "preparing")
    )
      return;
    let current = true;
    let inFlight = false;
    const timer = window.setInterval(() => {
      if (inFlight) return;
      inFlight = true;
      void agentComputerApi
        .project(source, projectID)
        .then((verified) => {
          if (!current) return;
          setProject(verified);
          setProjectError(null);
          setProjects((previous) =>
            previous.some((item) => item.projectID === verified.projectID)
              ? previous.map((item) =>
                  item.projectID === verified.projectID ? verified : item,
                )
              : [verified, ...previous],
          );
        })
        .catch((cause: unknown) => {
          if (current)
            setProjectError(
              apiErrorMessage(cause, t("boxProjects.projectRefreshFailed"), t),
            );
        })
        .finally(() => {
          inFlight = false;
        });
    }, 2500);
    return () => {
      current = false;
      window.clearInterval(timer);
    };
  }, [source, projectID, project?.projectID, project?.preparationState, t]);

  const documentsReady = Boolean(
    projectID &&
      project?.projectID === projectID &&
      project.preparationState === "ready",
  );
  useEffect(() => {
    if (!projectID || !documentsReady) return;
    let current = true;
    setDocumentsLoading(true);
    setDocumentError(null);
    agentComputerApi
      .listProjectDocuments(source, projectID)
      .then((listing) => {
        if (current) setDocuments(listing.items);
      })
      .catch((cause: unknown) => {
        if (current)
          setDocumentError(
            apiErrorMessage(cause, t("boxProjects.documentsFailed"), t),
          );
      })
      .finally(() => {
        if (current) setDocumentsLoading(false);
      });
    return () => {
      current = false;
    };
  }, [source, projectID, documentsReady, documentsRevision, t]);

  useEffect(() => {
    let current = true;
    setTask(null);
    setTurns([]);
    setTurnsCursor(null);
    setTaskError(null);
    setPendingRunID(null);
    setRunOutput(null);
    setRunOutputError(null);
    setTurnRefreshError(null);
    setExpandedToolRunID(null);
    setToolActivityByRun({});
    setToolActivityLoading(false);
    setToolActivityError(null);
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
    let listing: BoxPage<BoxTurn>;
    try {
      listing = await agentComputerApi.turns(source, taskID);
    } catch (cause) {
      if (mounted.current)
        setTurnRefreshError(
          apiErrorMessage(cause, t("boxProjects.turnRefreshFailed"), t),
        );
      throw cause;
    }
    if (!mounted.current) return;
    setTurnRefreshError(null);
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
  }, [source, taskID, pendingRunID, t]);

  const needsPolling = Boolean(
    pendingRunID || turns.some((item) => !TERMINAL_STATES.has(item.run.state)),
  );
  const activeRunID =
    pendingRunID ??
    turns.find((item) => !TERMINAL_STATES.has(item.run.state))?.run.runID ??
    null;
  useEffect(() => {
    if (!taskID || !needsPolling) return;
    const timer = window.setInterval(() => {
      void refreshTurns().catch(() => undefined);
    }, 3000);
    return () => window.clearInterval(timer);
  }, [taskID, needsPolling, refreshTurns]);

  useEffect(() => {
    if (!activeRunID) {
      setRunOutput(null);
      setRunOutputError(null);
      return;
    }
    let current = true;
    let inFlight = false;
    const refreshOutput = async () => {
      if (inFlight) return;
      inFlight = true;
      try {
        const snapshot = await agentComputerApi.runOutput(source, activeRunID);
        if (!current) return;
        if (snapshot.runID !== activeRunID)
          throw new Error(t("boxProjects.outputMismatch"));
        setRunOutput(snapshot);
        setRunOutputError(null);
        if (snapshot.state === "succeeded")
          await refreshTurns().catch(() => undefined);
      } catch (cause) {
        if (current)
          setRunOutputError(
            apiErrorMessage(cause, t("boxProjects.outputRefreshFailed"), t),
          );
      } finally {
        inFlight = false;
      }
    };
    void refreshOutput();
    const timer = window.setInterval(() => void refreshOutput(), 3000);
    return () => {
      current = false;
      window.clearInterval(timer);
    };
  }, [source, activeRunID, refreshTurns, t]);

  async function loadToolActivity(runID: string) {
    setToolActivityLoading(true);
    setToolActivityError(null);
    try {
      const activity = await agentComputerApi.runToolActivity(source, runID);
      if (activity.runID !== runID)
        throw new Error(t("boxProjects.toolActivityMismatch"));
      if (mounted.current)
        setToolActivityByRun((previous) => ({
          ...previous,
          [runID]: activity,
        }));
    } catch (cause) {
      if (mounted.current)
        setToolActivityError(
          apiErrorMessage(cause, t("boxProjects.toolActivityFailed"), t),
        );
    } finally {
      if (mounted.current) setToolActivityLoading(false);
    }
  }

  function toggleToolActivity(runID: string) {
    if (expandedToolRunID === runID) {
      setExpandedToolRunID(null);
      return;
    }
    setExpandedToolRunID(runID);
    setToolActivityError(null);
    if (!toolActivityByRun[runID]) void loadToolActivity(runID);
  }

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

  async function readDocument(filename: string) {
    if (!projectID) return;
    const requestID = ++documentRequestID.current;
    setDocumentLoading(true);
    setDocumentError(null);
    setSelectedDocument(null);
    try {
      const result = await agentComputerApi.getProjectDocument(
        source,
        projectID,
        filename,
      );
      if (mounted.current && requestID === documentRequestID.current)
        setSelectedDocument(result);
    } catch (cause) {
      if (mounted.current && requestID === documentRequestID.current)
        setDocumentError(
          apiErrorMessage(cause, t("boxProjects.documentReadFailed"), t),
        );
    } finally {
      if (mounted.current && requestID === documentRequestID.current)
        setDocumentLoading(false);
    }
  }

  async function createDocument() {
    const filename = documentFilename.trim();
    if (
      !projectID ||
      !documentsReady ||
      !validDocumentFilename(filename) ||
      documents.includes(filename) ||
      new TextEncoder().encode(documentContent).length > DOCUMENT_LIMIT_BYTES ||
      documentSaving
    )
      return;
    setDocumentSaving(true);
    setDocumentActionError(null);
    setDocumentOutcomeUncertain(false);
    try {
      const created = await agentComputerApi.createProjectDocument(
        source,
        projectID,
        filename,
        documentContent,
      );
      if (!mounted.current) return;
      setDocuments((previous) =>
        [...new Set([...previous, created.filename])].sort(),
      );
      setDocumentsRevision((value) => value + 1);
      documentRequestID.current += 1;
      setDocumentLoading(false);
      setSelectedDocument(created);
      setDocumentModal(false);
      setDocumentFilename("");
      setDocumentContent("");
    } catch (cause) {
      if (mounted.current) {
        setDocumentOutcomeUncertain(!isDefiniteRejection(cause));
        setDocumentActionError(
          apiErrorMessage(cause, t("boxProjects.documentCreateFailed"), t),
        );
      }
    } finally {
      if (mounted.current) setDocumentSaving(false);
    }
  }

  async function checkDocuments() {
    if (!projectID || documentChecking) return;
    setDocumentChecking(true);
    try {
      const listing = await agentComputerApi.listProjectDocuments(
        source,
        projectID,
      );
      if (mounted.current) setDocuments(listing.items);
    } catch (cause) {
      if (mounted.current)
        setDocumentActionError(
          apiErrorMessage(cause, t("boxProjects.documentsFailed"), t),
        );
    } finally {
      if (mounted.current) setDocumentChecking(false);
    }
  }

  const filename = documentFilename.trim();
  const filenameValid = validDocumentFilename(filename);
  const filenameExists = documents.includes(filename);
  const documentBytes = new TextEncoder().encode(documentContent).length;
  const documentTooLarge = documentBytes > DOCUMENT_LIMIT_BYTES;

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
              {turnRefreshError && (
                <Alert
                  className={styles.alert}
                  type="warning"
                  showIcon
                  message={t("boxProjects.turnRefreshStale")}
                  description={turnRefreshError}
                />
              )}
              {runOutputError && (
                <Alert
                  className={styles.alert}
                  type="warning"
                  showIcon
                  message={runOutputError}
                />
              )}
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
                      {item.result.text ??
                        (runOutput?.runID === item.run.runID &&
                        runOutput.provisional &&
                        runOutput.text !== null &&
                        !TERMINAL_STATES.has(item.run.state) &&
                        !TERMINAL_STATES.has(runOutput.state) ? (
                          <>
                            <Tag color="processing">
                              {t("boxProjects.provisionalOutput")}
                            </Tag>
                            <div>{runOutput.text}</div>
                          </>
                        ) : (
                          <Tag color={statusColor(item.run.state)}>
                            {stateLabel(item.run.state)}
                          </Tag>
                        ))}
                      <div className={styles.toolActivityActions}>
                        <Button
                          size="small"
                          type="link"
                          disabled={
                            toolActivityLoading &&
                            expandedToolRunID !== item.run.runID
                          }
                          onClick={() => toggleToolActivity(item.run.runID)}
                        >
                          {expandedToolRunID === item.run.runID
                            ? t("boxProjects.hideToolActivity")
                            : t("boxProjects.showToolActivity")}
                        </Button>
                      </div>
                      {expandedToolRunID === item.run.runID && (
                        <div className={styles.toolActivity}>
                          {toolActivityError && (
                            <Alert
                              type="warning"
                              showIcon
                              message={toolActivityError}
                            />
                          )}
                          {toolActivityByRun[item.run.runID] ? (
                            toolActivityByRun[item.run.runID].items.length ? (
                              toolActivityByRun[item.run.runID].items.map(
                                (activity) => (
                                  <div key={activity.path}>
                                    <Typography.Text>
                                      {activity.path}
                                    </Typography.Text>{" "}
                                    <Tag
                                      color={
                                        activity.status === "ok"
                                          ? "success"
                                          : "warning"
                                      }
                                    >
                                      {t(
                                        `boxProjects.documentReadStatuses.${activity.status}`,
                                      )}
                                    </Tag>
                                    <Tag>
                                      {t(
                                        `boxProjects.toolOutcomes.${activity.outcome}`,
                                      )}
                                    </Tag>
                                  </div>
                                ),
                              )
                            ) : (
                              <Typography.Text type="secondary">
                                {t(
                                  TERMINAL_STATES.has(item.run.state)
                                    ? "boxProjects.noToolActivity"
                                    : "boxProjects.noToolActivityYet",
                                )}
                              </Typography.Text>
                            )
                          ) : toolActivityLoading ? (
                            <Typography.Text type="secondary">
                              {t("boxProjects.loading")}
                            </Typography.Text>
                          ) : null}
                          <Button
                            size="small"
                            type="link"
                            loading={toolActivityLoading}
                            onClick={() =>
                              void loadToolActivity(item.run.runID)
                            }
                          >
                            {t("boxProjects.refresh")}
                          </Button>
                        </div>
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
      {documentsReady && (
        <Card
          className={styles.documentsPanel}
          title={t("boxProjects.documents")}
          extra={
            <Button size="small" onClick={() => setDocumentModal(true)}>
              {t("boxProjects.addDocument")}
            </Button>
          }
        >
          {documentError && (
            <Alert
              className={styles.alert}
              type="error"
              showIcon
              message={documentError}
            />
          )}
          <div className={styles.documentsLayout}>
            <div className={styles.documentList}>
              {documentsLoading ? (
                <Typography.Text>{t("boxProjects.loading")}</Typography.Text>
              ) : documents.length === 0 ? (
                <Empty description={t("boxProjects.noDocuments")} />
              ) : (
                documents.map((name) => (
                  <button
                    key={name}
                    type="button"
                    className={`${styles.item} ${
                      selectedDocument?.filename === name ? styles.active : ""
                    }`}
                    onClick={() => void readDocument(name)}
                  >
                    <span className={styles.itemTitle}>{name}</span>
                  </button>
                ))
              )}
            </div>
            <div className={styles.documentPreview}>
              {documentLoading ? (
                <Typography.Text>{t("boxProjects.loading")}</Typography.Text>
              ) : selectedDocument ? (
                <>
                  <Typography.Text strong>
                    {selectedDocument.filename}
                  </Typography.Text>
                  <pre className={styles.documentContent}>
                    {selectedDocument.content}
                  </pre>
                </>
              ) : (
                <Empty description={t("boxProjects.chooseDocument")} />
              )}
            </div>
          </div>
        </Card>
      )}
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
      <Modal
        title={t("boxProjects.addDocument")}
        open={documentModal}
        okText={t("boxProjects.create")}
        okButtonProps={{
          disabled: !filenameValid || filenameExists || documentTooLarge,
          loading: documentSaving,
        }}
        onOk={() => void createDocument()}
        onCancel={() => {
          if (documentSaving) return;
          setDocumentModal(false);
          setDocumentFilename("");
          setDocumentContent("");
          setDocumentActionError(null);
          setDocumentOutcomeUncertain(false);
        }}
      >
        <Space direction="vertical" className={styles.form}>
          <Typography.Text type="secondary">
            {t("boxProjects.documentCreateHint")}
          </Typography.Text>
          {documentActionError && (
            <Alert
              type="error"
              showIcon
              message={documentActionError}
              description={
                documentOutcomeUncertain
                  ? t("boxProjects.documentUnknownOutcome")
                  : undefined
              }
            />
          )}
          {documentOutcomeUncertain && (
            <Button
              loading={documentChecking}
              onClick={() => void checkDocuments()}
            >
              {t("boxProjects.refreshDocuments")}
            </Button>
          )}
          <Input
            aria-label={t("boxProjects.documentFilename")}
            value={documentFilename}
            maxLength={128}
            placeholder={t("boxProjects.documentFilename")}
            onChange={(event) => setDocumentFilename(event.target.value)}
          />
          {filename && !filenameValid && (
            <Typography.Text type="danger">
              {t("boxProjects.documentFilenameInvalid")}
            </Typography.Text>
          )}
          {filenameExists && (
            <Typography.Text type="danger">
              {t("boxProjects.documentExists")}
            </Typography.Text>
          )}
          <Input.TextArea
            aria-label={t("boxProjects.documentContent")}
            value={documentContent}
            rows={8}
            placeholder={t("boxProjects.documentContent")}
            onChange={(event) => setDocumentContent(event.target.value)}
          />
          <Typography.Text type={documentTooLarge ? "danger" : "secondary"}>
            {t("boxProjects.documentSize", {
              count: documentBytes,
              limit: DOCUMENT_LIMIT_BYTES,
            })}
          </Typography.Text>
        </Space>
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
  const [connectionForm] = Form.useForm<{
    display_name: string;
    peer_base_url: string;
    peer_username: string;
    password: string;
  }>();
  const [connections, setConnections] = useState<BridgeConnection[]>([]);
  const [connectionsLoading, setConnectionsLoading] = useState(true);
  const [connectionsError, setConnectionsError] = useState<string | null>(null);
  const [connectionFormOpen, setConnectionFormOpen] = useState(false);
  const [creatingConnection, setCreatingConnection] = useState(false);
  const [connectionCreateError, setConnectionCreateError] = useState<
    string | null
  >(null);
  const creatingConnectionRef = useRef(false);
  const [refreshRevision, setRefreshRevision] = useState(0);

  useEffect(() => {
    let current = true;
    bridgeApi
      .list()
      .then((rows) => {
        if (current) {
          setConnections(rows.filter((row) => !row.inbound));
          setConnectionsError(null);
        }
      })
      .catch((cause: unknown) => {
        if (current)
          setConnectionsError(
            apiErrorMessage(cause, t("boxProjects.status"), t),
          );
      })
      .finally(() => {
        if (current) setConnectionsLoading(false);
      });
    return () => {
      current = false;
    };
  }, [t]);

  async function createConnection() {
    if (creatingConnectionRef.current) return;
    creatingConnectionRef.current = true;
    setConnectionCreateError(null);
    try {
      const values = await connectionForm.validateFields();
      setCreatingConnection(true);
      const created = await bridgeApi.create({
        display_name: values.display_name.trim(),
        peer_base_url: values.peer_base_url.trim(),
        peer_username: values.peer_username.trim(),
        password: values.password,
        connect: true,
      });
      connectionForm.resetFields();
      setConnectionFormOpen(false);
      setConnections((previous) =>
        mergeById([created], previous, (row) => row.connection_id),
      );
      setConnectionsError(null);
      navigate(boxUrl(created.connection_id));
    } catch (cause) {
      if (!(cause && typeof cause === "object" && "errorFields" in cause)) {
        setConnectionCreateError(
          apiErrorMessage(cause, t("boxProjects.connectionFailed"), t),
        );
      }
    } finally {
      creatingConnectionRef.current = false;
      setCreatingConnection(false);
    }
  }

  function closeConnectionForm() {
    connectionForm.resetFields();
    setConnectionCreateError(null);
    setConnectionFormOpen(false);
  }

  const showConnectionForm =
    !connectionsLoading && (connections.length === 0 || connectionFormOpen);

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
          {connections.length > 0 && (
            <Button onClick={() => setConnectionFormOpen(true)}>
              {t("boxProjects.addConnection")}
            </Button>
          )}
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
      {showConnectionForm && (
        <Card
          className={styles.connectionSetup}
          title={t("boxProjects.addConnection")}
          extra={<Link to="/bridge">{t("boxProjects.manageConnections")}</Link>}
        >
          <Typography.Paragraph type="secondary">
            {t("boxProjects.connectionSetupHint")}
          </Typography.Paragraph>
          <Typography.Paragraph type="secondary">
            {t("boxProjects.connectionCredentialHint")}
          </Typography.Paragraph>
          {connectionCreateError && (
            <Alert
              className={styles.alert}
              type="error"
              showIcon
              message={connectionCreateError}
              description={t("boxProjects.connectionRetryHint")}
            />
          )}
          <Form
            form={connectionForm}
            layout="vertical"
            className={styles.connectionForm}
            onFinish={() => void createConnection()}
          >
            <Form.Item
              name="display_name"
              label={t("advancedSettings.bridge.displayName")}
              rules={[
                {
                  required: true,
                  whitespace: true,
                  message: t("advancedSettings.bridge.displayNameRequired"),
                },
              ]}
            >
              <Input maxLength={64} autoComplete="off" />
            </Form.Item>
            <Form.Item
              name="peer_base_url"
              label={t("advancedSettings.bridge.peerUrl")}
              rules={[
                {
                  required: true,
                  whitespace: true,
                  message: t("advancedSettings.bridge.peerUrlRequired"),
                },
              ]}
            >
              <Input placeholder="http://127.0.0.1:8090" autoComplete="url" />
            </Form.Item>
            <Form.Item
              name="peer_username"
              label={t("advancedSettings.bridge.username")}
              rules={[
                {
                  required: true,
                  whitespace: true,
                  message: t("advancedSettings.bridge.usernameRequired"),
                },
              ]}
            >
              <Input autoComplete="username" />
            </Form.Item>
            <Form.Item
              name="password"
              label={t("advancedSettings.bridge.password")}
              rules={[
                {
                  required: true,
                  message: t("advancedSettings.bridge.passwordRequired"),
                },
              ]}
            >
              <Input.Password
                autoComplete="current-password"
                visibilityToggle={false}
              />
            </Form.Item>
            <Space>
              <Button
                type="primary"
                htmlType="submit"
                loading={creatingConnection}
              >
                {t("boxProjects.connect")}
              </Button>
              {connections.length > 0 && (
                <Button
                  onClick={closeConnectionForm}
                  disabled={creatingConnection}
                >
                  {t("common.cancel")}
                </Button>
              )}
            </Space>
          </Form>
        </Card>
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
