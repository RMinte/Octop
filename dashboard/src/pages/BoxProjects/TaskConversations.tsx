import { isImeComposing } from "../../utils/ime";
import { Dropdown } from "antd";
import TaskMemory from "./TaskMemory";
import TaskPromptPolish from "./TaskPromptPolish";
import TaskMedia from "./TaskMedia";
import { useTaskHost } from "../../context/TaskHostContext";
import { taskExpertLabel } from "./taskExpertLabel";
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import {
  Alert,
  Button,
  Empty,
  Image,
  Input,
  Modal,
  Popover,
  Segmented,
  Select,
  Space,
  Spin,
  Tag,
  Typography,
} from "antd";
import {
  Archive,
  ArchiveRestore,
  Download,
  FileText,
  Paperclip,
  Plus,
  Pause,
  Play,
  Send,
  Shrink,
  Square,
  Pin,
  PinOff,
  GitBranch,
  Pencil,
  RefreshCw,
  Trash2,
  X,
  SlidersHorizontal,
  PanelLeft,
  MoreHorizontal,
} from "lucide-react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";
import type { TextAreaRef } from "antd/es/input/TextArea";
import {
  type TaskMessage,
  type TaskCreate,
  type TaskExecutionOptions,
  type TaskArtifact,
  type Project,
  type TaskSummary,
  type TaskPatch,
  type TaskActionMode,
  type TaskResources,
  type TaskStatus,
  type TaskFork,
  type TaskTurn,
  type EngineSelection,
} from "../../api/modules/projects";
import TaskEngineSelect from "./TaskEngineSelect";
import {
  engineCapabilities,
  engineEntry,
  engineKey,
  engineLabel,
  OCTOP_ENGINE,
  taskOptionsForEngine,
} from "./taskEngine";
import { useServerTimezone } from "../../hooks/useServerTimezone";
import { apiErrorMessage, parseApiError } from "../../utils/apiError";
import { formatServerIsoDateTime } from "../../utils/formatMessageTime";
import styles from "./index.module.less";
import { useTaskList } from "./useProjectLists";
import { useTaskConversation } from "./useTaskConversation";
import TaskComposer from "./TaskComposer";
import TaskComposerResources, {
  TaskComposerResourceTriggers,
} from "./TaskComposerResources";
import { useTaskComposerResources } from "./useTaskComposerResources";
import TaskRecoveryNotice from "./TaskRecoveryNotice";
import TaskTurnContent from "./TaskTurnContent";
import TaskInteractions from "./TaskInteractions";
import TaskApprovalModePicker from "./TaskApprovalMode";
import TaskWebSearch from "./TaskWebSearch";
import TaskInputImages from "./TaskInputImages";
import TaskExpertMemory from "./TaskExpertMemory";
import TaskWorkbench from "./TaskWorkbench";
import { TaskVoiceInput, TaskVoiceRead } from "./TaskVoice";
import TaskContextUsage, { TaskTurnUsage } from "./TaskContextUsage";
import { MAX_TASK_IMAGES, useTaskImages } from "./useTaskImages";
import {
  useTaskMessageQueue,
  type TaskMessageQueue,
} from "./useTaskMessageQueue";

export interface TaskConversationListScope {
  projectID?: string | null;
  status?: TaskStatus;
  pinned?: boolean;
  active?: boolean;
}

export default function TaskConversations({
  projectID,
  project: initialProject,
  queue: sharedQueue,
  selectedTaskID: routedTaskID,
  onSelectTask,
  allTasks = false,
  defaultExpertID = null,
  projectOptions = [],
  navigation,
  overview,
  workspace = false,
  listScope,
  onWorkbenchDirtyChange,
}: {
  projectID: string | null;
  project?: Project;
  queue?: TaskMessageQueue;
  selectedTaskID?: string | null;
  onSelectTask?: (id: string | null) => void;
  allTasks?: boolean;
  defaultExpertID?: string | null;
  projectOptions?: Project[];
  navigation?: ReactNode;
  overview?: ReactNode;
  workspace?: boolean;
  listScope?: TaskConversationListScope;
  onWorkbenchDirtyChange?: (dirty: boolean) => void;
}) {
  const { projectTasksApi, projectsApi, taskExtrasApi, hostID } = useTaskHost();
  const { t } = useTranslation();
  const [project, setProject] = useState(initialProject);
  useEffect(() => setProject(initialProject), [initialProject]);
  const localQueue = useTaskMessageQueue();
  const queue = sharedQueue ?? localQueue;
  const clearQueue = queue.clear;
  const timeZone = useServerTimezone();
  const [params, setParams] = useSearchParams();
  const taskID = routedTaskID !== undefined ? routedTaskID : params.get("task");
  const [resources, setResources] = useState<TaskResources | null>(null);
  const [resourceError, setResourceError] = useState("");
  const [resourceVersion, setResourceVersion] = useState(0);
  const [expertID, setExpertID] = useState<string | null>(defaultExpertID);
  const [engine, setEngine] = useState<EngineSelection>(OCTOP_ENGINE);
  const [inspectingEngine, setInspectingEngine] = useState(false);
  const [actionMode, setActionMode] = useState<TaskActionMode>("craft");
  const [search, setSearch] = useState("");
  const [query, setQuery] = useState("");
  const [expertFilter, setExpertFilter] = useState<string | undefined>();
  const [statusFilter, setStatusFilter] = useState<TaskStatus | undefined>();
  const [projectFilter, setProjectFilter] = useState<string | undefined>();
  const [mobileListOpen, setMobileListOpen] = useState(false);
  const showingOverview = !!overview;
  useEffect(() => {
    setMobileListOpen(false);
  }, [taskID, projectID, showingOverview]);
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [branch, setBranch] = useState<Omit<TaskFork, "request_id"> | null>(
    null,
  );
  const [forking, setForking] = useState(false);
  const branchRequest = useRef<{ key: string; requestID: string } | null>(null);
  useEffect(() => {
    const timer = window.setTimeout(() => setQuery(search.trim()), 250);
    return () => window.clearTimeout(timer);
  }, [search]);
  useEffect(() => {
    let disposed = false;
    void projectTasksApi
      .resources()
      .then((result) => {
        if (!disposed) {
          setResources(result);
          setResourceError("");
        }
      })
      .catch((err) => {
        if (!disposed)
          setResourceError(
            apiErrorMessage(err, t("projects.tasks.failure"), t),
          );
      });
    return () => {
      disposed = true;
    };
  }, [projectTasksApi, resourceVersion, t]);
  const queued = taskID ? queue.queues[taskID] : undefined;
  const queuedItems = queued?.items ?? [];
  const queueDelivery = taskID ? queue.delivered[taskID] : undefined;
  const archivedView = params.get("archived") === "true";
  const listProjectID = listScope
    ? listScope.projectID ?? null
    : allTasks
    ? projectFilter ?? null
    : projectID;
  const listAll = listScope
    ? listScope.projectID === undefined
    : allTasks && !projectFilter;
  const {
    items: list,
    refresh: refreshList,
    loadMore,
    hasMore,
    loadingMore,
    upsert: upsertTask,
    remove: removeTask,
    isDeleted,
  } = useTaskList(
    listProjectID,
    archivedView,
    listAll || query || expertFilter || statusFilter || listScope?.status
      ? {
          scope: listAll ? "all" : undefined,
          q: query || undefined,
          expert_id: expertFilter,
          status: statusFilter ?? listScope?.status,
        }
      : undefined,
  );
  const visibleList = list.filter(
    (task) =>
      (!listScope?.pinned || task.pinned) &&
      (!listScope?.active ||
        [
          "queued",
          "running",
          "waiting_approval",
          "waiting_answer",
          "waiting_client_tool",
          "paused",
        ].includes(task.status)),
  );
  const {
    detail,
    loading,
    error: detailError,
    reconnecting,
    loadingHistory,
    refresh: refreshDetail,
    applyWrite,
    loadHistory,
  } = useTaskConversation(taskID, projectID);
  const [workbenchTool, setWorkbenchTool] = useState<{
    taskID: string;
    callID: string;
    sequence: number;
  } | null>(null);
  const [error, setError] = useState("");
  const [loadError, setLoadError] = useState("");
  const alive = useRef(true);
  const selectedTaskID = useRef(taskID);
  selectedTaskID.current = taskID;
  const selectedProjectID = useRef(projectID);
  const selectedHostID = useRef(hostID);
  selectedHostID.current = hostID;
  selectedProjectID.current = projectID;
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  const [draft, setDraft] = useState("");
  const draftKey = taskID ?? `draft:${projectID ?? "standalone"}`;
  const images = useTaskImages(`${projectID ?? ""}:${taskID ?? "new"}`);
  const imageInput = useRef<HTMLInputElement>(null);
  const composerInput = useRef<TextAreaRef>(null);
  const getComposerInput = useCallback(
    () => composerInput.current?.resizableTextArea?.textArea ?? null,
    [],
  );
  const [options, setOptions] = useState<TaskExecutionOptions>({});
  const createdTaskSelection = useRef<{
    taskID: string;
    projectID: string | null;
    model_ref?: string;
  } | null>(null);
  const [approvalSaving, setApprovalSaving] = useState(false);
  const [webSearchEnabled, setWebSearchEnabled] = useState(true);
  const webSearchSaving = !!taskID && queue.isHeld(taskID);
  const [updating, setUpdating] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<TaskSummary | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState("");
  const [sending, setSending] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [downloading, setDownloading] = useState<string | null>(null);
  const inFlight = useRef(false);
  const pending = useRef<{
    target: string;
    body: TaskMessage | TaskCreate;
  } | null>(null);
  const compactRequest = useRef<{
    target: string;
    body: TaskMessage;
  } | null>(null);
  const end = useRef<HTMLDivElement>(null);
  const followOutput = useRef(true);
  const retryRequest = useRef<{
    turnID: string;
    requestID: string;
    optionsKey: string;
  } | null>(null);
  const [controlling, setControlling] = useState(false);
  const controlRequest = useRef<{ key: string; requestID: string } | null>(
    null,
  );
  const controlInFlight = useRef(false);
  const activeTurn = detail?.turns.find(
    (turn) => turn.turn_id === detail.active_turn_id,
  );
  const busy = !!detail?.active_turn_id;
  const compacting =
    activeTurn?.prompt === "/compact" && !activeTurn.attachments?.length;
  const control = async () => {
    if (
      !detail ||
      !activeTurn ||
      activeTurn.cancel_requested ||
      (compacting && activeTurn.status !== "interrupted") ||
      controlInFlight.current
    )
      return;
    if (
      activeTurn.status === "interrupted" &&
      (!activeTurn.recovery?.can_resume ||
        activeTurn.recovery.needs_reconciliation)
    )
      return;
    controlInFlight.current = true;
    setControlling(true);
    const paused = !(
      activeTurn.pause_requested ||
      activeTurn.status === "paused" ||
      activeTurn.status === "interrupted"
    );
    const key = `${detail.task_id}:${activeTurn.turn_id}:${paused}`;
    if (controlRequest.current?.key !== key)
      controlRequest.current = { key, requestID: crypto.randomUUID() };
    try {
      const incoming = await projectTasksApi.control(
        detail.task_id,
        activeTurn.turn_id,
        paused,
        controlRequest.current.requestID,
      );
      if (selectedTaskID.current === incoming.task_id) applyWrite(incoming);
      setError("");
    } catch (err) {
      setError(apiErrorMessage(err, t("projects.tasks.failure"), t));
    } finally {
      setControlling(false);
      controlInFlight.current = false;
    }
  };
  const projectArchived = !!project?.archived;
  const selectionDisabled =
    inspectingEngine ||
    busy ||
    sending ||
    loading ||
    updating ||
    reconnecting ||
    queuedItems.length > 0 ||
    !!queued?.sendingID ||
    !!detail?.archived ||
    projectArchived ||
    webSearchSaving ||
    approvalSaving;
  const selectedExpert = taskID ? detail?.selected_expert_id ?? null : expertID;
  const selectedEngine = taskID ? detail?.engine ?? OCTOP_ENGINE : engine;
  const selectedEngineEntry = engineEntry(
    selectedEngine,
    resources?.execution_engines,
  );
  const capabilities = engineCapabilities(
    selectedEngine,
    resources?.execution_engines,
  );
  const activeCapabilities =
    activeTurn?.execution_engine?.capabilities ?? capabilities;
  const engineUnavailable =
    !!resources?.execution_engines && !selectedEngineEntry?.available;
  const externalEngine = selectedEngine.engine_id !== "octop";
  const engineClientDirectoryConflict =
    externalEngine && !!project?.client_directory;
  const submissionOptions = taskOptionsForEngine(options, selectedEngine);
  const engineImageConflict =
    !capabilities.images && images.items.some((image) => image.isImage);
  const addAttachments = (files: File[]) => {
    if (
      !capabilities.images &&
      files.some((file) => file.type.startsWith("image/"))
    ) {
      setError(t("projects.tasks.engineImagesUnsupported"));
      images.add(files.filter((file) => !file.type.startsWith("image/")));
    } else images.add(files);
  };
  const selectedExpertUnavailable =
    !!selectedExpert &&
    !resourceError &&
    !!resources &&
    Array.isArray(resources?.experts) &&
    !resources.experts.some((expert) => expert.expert_id === selectedExpert);
  const selectedMode = taskID ? detail?.action_mode ?? "craft" : actionMode;
  const revisions = {
    expected_configuration_revision: detail?.configuration_revision ?? 1,
    expected_project_revision: project?.revision ?? null,
  };
  const [confirmedDirectory, setConfirmedDirectory] = useState<string | null>(
    null,
  );
  const directoryKey = JSON.stringify([
    taskID,
    projectID,
    project?.directory,
    project?.client_directory,
  ]);
  const lastScope = detail?.turns.at(-1);
  const directoryChanged =
    !!lastScope &&
    "server_directory" in lastScope &&
    ((lastScope.server_directory ?? null) !== (project?.directory ?? null) ||
      (lastScope.client_directory ?? null) !==
        (project?.client_directory ?? null)) &&
    confirmedDirectory !== directoryKey;
  const recoverConflict = async (err: unknown) => {
    const code = parseApiError(err)?.code;
    if (
      code !== "TASK_REVISION_CONFLICT" &&
      code !== "PROJECT_REVISION_CONFLICT"
    )
      return;
    compactRequest.current = null;
    retryRequest.current = null;
    pending.current = null;
    await refreshDetail();
    if (projectID) {
      const incoming = await projectsApi.get(projectID);
      if (alive.current && selectedProjectID.current === projectID)
        setProject(incoming);
    }
  };
  const composerDisabled =
    sending ||
    loading ||
    !!detail?.archived ||
    directoryChanged ||
    projectArchived ||
    !!(taskID && !detail);
  const editDraft = useCallback(
    (text: string) => {
      const next = text.slice(0, 32768);
      setDraft(next);
      queue.drafts.current.set(draftKey, next);
    },
    [draftKey, queue.drafts],
  );
  const composerResources = useTaskComposerResources({
    taskID,
    project,
    selectedExpertID: selectedExpert,
    text: draft,
    onTextChange: editDraft,
    value: options,
    onChange: setOptions,
    resources,
    skillsEnabled: capabilities.skills,
    disabled: composerDisabled || updating,
    getInputElement: getComposerInput,
  });
  const compactDisabled =
    !capabilities.compact ||
    composerDisabled ||
    busy ||
    queuedItems.length > 0 ||
    !!queued?.sendingID ||
    updating ||
    approvalSaving ||
    webSearchSaving ||
    !!deleteTarget;
  const listSummary = useRef("");
  const renameRevision = useRef<number | null>(null);
  const taskDeleted = !!taskID && isDeleted(taskID);

  useEffect(() => {
    if (
      queueDelivery?.task_id === taskID &&
      queueDelivery.project_id === projectID &&
      !taskDeleted
    ) {
      applyWrite(queueDelivery);
      upsertTask(queueDelivery);
    }
  }, [queueDelivery, taskID, projectID, taskDeleted, applyWrite, upsertTask]);

  useEffect(() => {
    if (!taskDeleted) return;
    if (taskID) clearQueue(taskID);
    if (onSelectTask) {
      onSelectTask(null);
      return;
    }
    const next = new URLSearchParams(params);
    next.delete("task");
    setParams(next, { replace: true });
  }, [taskDeleted, taskID, clearQueue, params, setParams, onSelectTask]);

  const refresh = useCallback(async () => {
    try {
      await Promise.all([refreshList(), refreshDetail()]);
      setLoadError("");
    } catch (err) {
      if (alive.current)
        setLoadError(apiErrorMessage(err, t("projects.tasks.failure"), t));
    }
  }, [refreshList, refreshDetail, t]);

  useEffect(() => {
    setError("");
    setWorkbenchTool(null);
    setDraft(queue.drafts.current.get(draftKey) ?? "");
    setExpertID(defaultExpertID);
    setEngine(OCTOP_ENGINE);
    setActionMode("craft");
    const created = createdTaskSelection.current;
    setOptions(
      created?.taskID === taskID &&
        created.projectID === projectID &&
        created.model_ref
        ? { model_ref: created.model_ref }
        : {},
    );
    createdTaskSelection.current = null;
    setWebSearchEnabled(true);
    pending.current = null;
    compactRequest.current = null;
    retryRequest.current = null;
    followOutput.current = true;
  }, [taskID, projectID, defaultExpertID, draftKey, queue.drafts]);

  useEffect(() => {
    let disposed = false;
    const updateList = () => {
      void refreshList()
        .then(() => {
          if (!disposed) setLoadError("");
        })
        .catch((err) => {
          if (!disposed)
            setLoadError(apiErrorMessage(err, t("projects.tasks.failure"), t));
        });
    };
    updateList();
    const visible = () => {
      if (!document.hidden) updateList();
    };
    const timer = window.setInterval(visible, 30_000);
    document.addEventListener("visibilitychange", visible);
    window.addEventListener("online", visible);
    return () => {
      disposed = true;
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", visible);
      window.removeEventListener("online", visible);
    };
  }, [refreshList, t]);

  useEffect(() => {
    const key = detail ? `${detail.task_id}:${detail.list_revision}` : "";
    if (detail && listSummary.current !== key) upsertTask(detail);
    listSummary.current = key;
  }, [detail, upsertTask]);

  useEffect(() => {
    const request = compactRequest.current;
    if (
      request &&
      detail?.task_id === request.target &&
      detail.turns.some((turn) => turn.request_id === request.body.request_id)
    )
      compactRequest.current = null;
  }, [detail]);

  const latestReply = detail?.turns.at(-1)?.reply;
  const latestTurnID = detail?.turns.at(-1)?.turn_id;
  useEffect(() => {
    if (followOutput.current)
      end.current?.scrollIntoView?.({ block: "nearest" });
  }, [latestReply, latestTurnID]);

  const downloadArtifact = async (artifact: TaskArtifact) => {
    setDownloading(artifact.artifact_id);
    try {
      const blob = await projectTasksApi.downloadArtifact(
        artifact.task_id,
        artifact.artifact_id,
      );
      if (!alive.current || selectedTaskID.current !== artifact.task_id) return;
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = artifact.name;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(url);
    } catch (err) {
      if (alive.current && selectedTaskID.current === artifact.task_id)
        setError(
          apiErrorMessage(err, t("projects.tasks.artifactDownloadFailed"), t),
        );
    } finally {
      if (alive.current) setDownloading(null);
    }
  };

  const selectTask = (id: string | null) => {
    setMobileListOpen(false);
    if (onSelectTask) {
      onSelectTask(id);
      return;
    }
    const next = new URLSearchParams(params);
    if (id) next.set("task", id);
    else {
      next.delete("task");
      next.delete("archived");
    }
    setParams(next);
  };

  const send = async () => {
    const prompt = draft.trim();
    const attachmentIDs = images.attachmentIDs;
    if (
      (!prompt && !attachmentIDs.length) ||
      images.blocked ||
      engineUnavailable ||
      engineImageConflict ||
      engineClientDirectoryConflict ||
      inspectingEngine ||
      detail?.archived ||
      projectArchived ||
      deleteTarget ||
      sending ||
      approvalSaving ||
      webSearchSaving ||
      updating ||
      inFlight.current ||
      (taskID && !detail) ||
      directoryChanged
    )
      return;
    const target = taskID ?? `new:${projectID ?? ""}`;
    if (
      pending.current?.target !== target ||
      pending.current.body.prompt !== prompt ||
      pending.current.body.expected_configuration_revision !==
        revisions.expected_configuration_revision ||
      pending.current.body.expected_project_revision !==
        revisions.expected_project_revision ||
      (!taskID &&
        ((pending.current.body as TaskCreate).selected_expert_id !== expertID ||
          JSON.stringify(
            (pending.current.body as TaskCreate).engine ?? OCTOP_ENGINE,
          ) !== JSON.stringify(selectedEngine) ||
          (pending.current.body as TaskCreate).action_mode !== actionMode)) ||
      JSON.stringify(pending.current.body.attachment_ids ?? []) !==
        JSON.stringify(attachmentIDs) ||
      JSON.stringify(pending.current.body.options ?? {}) !==
        JSON.stringify(submissionOptions) ||
      (!taskID &&
        (pending.current.body as TaskCreate).web_search_enabled !==
          webSearchEnabled)
    ) {
      pending.current = {
        target,
        body: {
          request_id: crypto.randomUUID(),
          prompt,
          ...revisions,
          ...(!taskID
            ? { selected_expert_id: expertID, action_mode: actionMode }
            : {}),
          ...(!taskID && selectedEngine.engine_id !== "octop"
            ? { engine: selectedEngine }
            : {}),
          ...(attachmentIDs.length ? { attachment_ids: attachmentIDs } : {}),
          ...(!taskID
            ? {
                web_search_enabled: webSearchEnabled,
              }
            : {}),
          ...(JSON.stringify(submissionOptions) !== "{}"
            ? { options: submissionOptions }
            : {}),
        },
      };
    }
    if (taskID && (busy || queuedItems.length > 0 || queued?.sendingID)) {
      const enqueued = queue.enqueue(
        taskID,
        projectID,
        pending.current.body,
        images.items.flatMap((item) =>
          item.attachment ? [item.attachment] : [],
        ),
      );
      if (enqueued === "full") {
        setError(t("chat.queue.full"));
        return;
      }
      setDraft("");
      queue.drafts.current.delete(draftKey);
      images.clear();
      setOptions((current) =>
        current.model_ref ? { model_ref: current.model_ref } : {},
      );
      pending.current = null;
      setError("");
      return;
    }
    inFlight.current = true;
    setSending(true);
    setError("");
    try {
      const result = taskID
        ? await projectTasksApi.send(taskID, pending.current.body)
        : await projectTasksApi.create(
            projectID,
            pending.current.body as TaskCreate,
          );
      if (
        !alive.current ||
        selectedTaskID.current !== taskID ||
        selectedProjectID.current !== projectID
      )
        return;
      upsertTask(result);
      applyWrite(result);
      setDraft("");
      queue.drafts.current.delete(draftKey);
      images.clear();
      setOptions((current) =>
        current.model_ref ? { model_ref: current.model_ref } : {},
      );
      pending.current = null;
      if (!taskID) {
        createdTaskSelection.current = {
          taskID: result.task_id,
          projectID,
          model_ref: options.model_ref,
        };
        selectTask(result.task_id);
      }
      void refreshList().catch((err) => {
        if (alive.current && selectedProjectID.current === projectID)
          setLoadError(apiErrorMessage(err, t("projects.tasks.failure"), t));
      });
    } catch (err) {
      await recoverConflict(err).catch(() => {});
      if (
        alive.current &&
        selectedTaskID.current === taskID &&
        selectedProjectID.current === projectID
      )
        setError(apiErrorMessage(err, t("projects.tasks.failure"), t));
    } finally {
      inFlight.current = false;
      setSending(false);
    }
  };

  const compact = async () => {
    if (!taskID || !detail || compactDisabled || inFlight.current) return;
    if (compactRequest.current?.target !== taskID)
      compactRequest.current = {
        target: taskID,
        body: {
          request_id: crypto.randomUUID(),
          prompt: "/compact",
          ...revisions,
          ...(options.model_ref
            ? { options: { model_ref: options.model_ref } }
            : {}),
        },
      };
    inFlight.current = true;
    setSending(true);
    setError("");
    try {
      const result = await projectTasksApi.send(
        taskID,
        compactRequest.current.body,
      );
      if (
        !alive.current ||
        selectedTaskID.current !== taskID ||
        selectedProjectID.current !== projectID
      )
        return;
      upsertTask(result);
      applyWrite(result);
      compactRequest.current = null;
    } catch (err) {
      await recoverConflict(err).catch(() => {});
      if (
        alive.current &&
        selectedTaskID.current === taskID &&
        selectedProjectID.current === projectID
      )
        setError(apiErrorMessage(err, t("projects.tasks.failure"), t));
    } finally {
      inFlight.current = false;
      setSending(false);
    }
  };

  const reclaimQueued = (id: string) => {
    if (!taskID) return;
    const apply = () => {
      if (
        !alive.current ||
        selectedTaskID.current !== taskID ||
        selectedProjectID.current !== projectID
      )
        return;
      const item = queue.reclaim(taskID, id);
      if (!item) return;
      setDraft(item.body.prompt ?? "");
      queue.drafts.current.set(draftKey, item.body.prompt ?? "");
      images.restore(item.attachments);
      setOptions(item.body.options ?? {});
      pending.current = { target: taskID, body: item.body };
      setError("");
    };
    if (draft.trim() || images.items.length) {
      Modal.confirm({
        title: t("chat.queue.reclaimOverwriteTitle"),
        content: t("chat.queue.reclaimOverwrite"),
        okText: t("common.confirm"),
        cancelText: t("common.cancel"),
        onOk: apply,
      });
    } else apply();
  };

  const stop = async () => {
    if (!taskID || !detail?.active_turn_id || stopping) return;
    setStopping(true);
    try {
      const result = await projectTasksApi.cancel(
        taskID,
        detail.active_turn_id,
      );
      if (
        !alive.current ||
        selectedTaskID.current !== taskID ||
        selectedProjectID.current !== projectID
      )
        return;
      upsertTask(result);
      applyWrite(result);
    } catch (err) {
      if (
        alive.current &&
        selectedTaskID.current === taskID &&
        selectedProjectID.current === projectID
      )
        setError(apiErrorMessage(err, t("projects.tasks.failure"), t));
    } finally {
      setStopping(false);
    }
  };

  const retry = async (turnID: string) => {
    if (
      !taskID ||
      busy ||
      queuedItems.length > 0 ||
      queued?.sendingID ||
      detail?.archived ||
      projectArchived ||
      directoryChanged ||
      deleteTarget ||
      sending ||
      approvalSaving ||
      webSearchSaving ||
      inFlight.current
    )
      return;
    const optionsKey = JSON.stringify([options, revisions]);
    if (
      retryRequest.current?.turnID !== turnID ||
      retryRequest.current.optionsKey !== optionsKey
    )
      retryRequest.current = {
        turnID,
        requestID: crypto.randomUUID(),
        optionsKey,
      };
    inFlight.current = true;
    setSending(true);
    setError("");
    try {
      const result = await projectTasksApi.retry(
        taskID,
        turnID,
        retryRequest.current.requestID,
        submissionOptions,
        revisions,
      );
      if (
        !alive.current ||
        selectedTaskID.current !== taskID ||
        selectedProjectID.current !== projectID
      )
        return;
      applyWrite(result);
      setOptions((current) =>
        current.model_ref ? { model_ref: current.model_ref } : {},
      );
      retryRequest.current = null;
    } catch (err) {
      await recoverConflict(err).catch(() => {});
      if (
        alive.current &&
        selectedTaskID.current === taskID &&
        selectedProjectID.current === projectID
      )
        setError(apiErrorMessage(err, t("projects.tasks.failure"), t));
    } finally {
      inFlight.current = false;
      setSending(false);
    }
  };

  const updateTask = async (
    changes: Omit<TaskPatch, "expected_revision">,
    expectedRevision?: number,
  ) => {
    if (!taskID || !detail || updating || webSearchSaving || deleteTarget)
      return;
    if (changes.title !== undefined && !changes.title.trim()) {
      setError(t("projects.tasks.titleRequired"));
      return;
    }
    setUpdating(true);
    setError("");
    try {
      const result = await projectTasksApi.update(taskID, {
        ...changes,
        expected_revision: expectedRevision ?? detail.list_revision,
        ...(changes.engine !== undefined ||
        changes.selected_expert_id !== undefined ||
        changes.action_mode !== undefined
          ? {
              expected_configuration_revision:
                detail.configuration_revision ?? 1,
            }
          : {}),
      });
      if (
        !alive.current ||
        selectedTaskID.current !== taskID ||
        selectedProjectID.current !== projectID
      )
        return;
      upsertTask(result);
      applyWrite(result);
      if (changes.archived !== undefined) {
        const next = new URLSearchParams(params);
        if (result.archived) next.set("archived", "true");
        else next.delete("archived");
        setParams(next);
      }
    } catch (err) {
      await recoverConflict(err).catch(() => {});
      if (
        alive.current &&
        selectedTaskID.current === taskID &&
        selectedProjectID.current === projectID
      )
        setError(apiErrorMessage(err, t("projects.tasks.failure"), t));
    } finally {
      setUpdating(false);
    }
  };

  const changeEngine = (next: EngineSelection) => {
    const modes = engineCapabilities(
      next,
      resources?.execution_engines,
    ).action_modes;
    const nextMode = modes.includes(selectedMode)
      ? selectedMode
      : modes[0] ?? "craft";
    if (taskID)
      void updateTask({
        engine: next,
        ...(nextMode !== selectedMode ? { action_mode: nextMode } : {}),
      });
    else {
      setEngine(next);
      setActionMode(nextMode);
    }
  };
  const inspectEngine = async () => {
    if (inspectingEngine) return;
    setInspectingEngine(true);
    try {
      const entry = await projectTasksApi.inspectEngine(selectedEngine);
      if (!alive.current || selectedHostID.current !== hostID) return;
      setResources((previous) =>
        previous
          ? {
              ...previous,
              execution_engines: [
                ...(previous.execution_engines ?? []).filter(
                  (item) => engineKey(item.engine) !== engineKey(entry.engine),
                ),
                entry,
              ],
            }
          : previous,
      );
      setResourceVersion((version) => version + 1);
    } catch (error) {
      setError(apiErrorMessage(error, t("projects.tasks.failure"), t));
    } finally {
      setInspectingEngine(false);
    }
  };

  const deleteTask = async () => {
    if (!deleteTarget || deleting) return;
    setDeleting(true);
    setDeleteError("");
    try {
      await projectTasksApi.delete(deleteTarget.task_id);
      queue.clear(deleteTarget.task_id);
      removeTask(deleteTarget.task_id, deleteTarget.list_revision);
      if (!alive.current) return;
      setDeleteTarget(null);
      if (selectedTaskID.current === deleteTarget.task_id) {
        onWorkbenchDirtyChange?.(false);
        selectTask(null);
      }
    } catch (err) {
      if (alive.current)
        setDeleteError(
          apiErrorMessage(err, t("projects.tasks.deleteFailed"), t),
        );
    } finally {
      if (alive.current) setDeleting(false);
    }
  };

  const openBranch = (turn: TaskTurn, operation: TaskFork["operation"]) => {
    setBranch({
      operation,
      turn_id: turn.turn_id,
      project_id: projectID,
      selected_expert_id: detail?.selected_expert_id ?? null,
      ...(detail?.engine ? { engine: detail.engine } : {}),
      ...(operation !== "fork" ? { prompt: turn.prompt } : {}),
    });
    branchRequest.current = null;
  };
  const createBranch = async () => {
    if (
      !taskID ||
      !branch ||
      forking ||
      (branch.operation === "edit" && !branch.prompt?.trim())
    )
      return;
    const key = JSON.stringify([taskID, branch]);
    if (branchRequest.current?.key !== key)
      branchRequest.current = { key, requestID: crypto.randomUUID() };
    setForking(true);
    setError("");
    try {
      const request: TaskFork = {
        ...branch,
        request_id: branchRequest.current.requestID,
      };
      if (request.operation === "regenerate") delete request.prompt;
      const result = await projectTasksApi.fork(taskID, request);
      if (!alive.current || selectedTaskID.current !== taskID) return;
      setBranch(null);
      upsertTask(result);
      selectTask(result.task_id);
    } catch (err) {
      if (alive.current)
        setError(apiErrorMessage(err, t("projects.tasks.failure"), t));
    } finally {
      if (alive.current) setForking(false);
    }
  };

  return (
    <section
      className={`${styles.taskSection} ${
        projectID ? "" : styles.standaloneTasks
      } ${workspace ? styles.workspaceTasks : ""}`}
      data-list-open={mobileListOpen}
      aria-label={t("projects.tasks.title")}
    >
      <div className={styles.taskSidebar}>
        {navigation}
        <div className={styles.taskListHeading}>
          <Typography.Text strong>
            {t("projects.workspace.conversations")}
          </Typography.Text>
          <Button
            type="text"
            size="small"
            icon={<Plus size={16} />}
            aria-label={t("projects.tasks.new")}
            onClick={() => selectTask(null)}
            disabled={sending || !!queued?.sendingID || projectArchived}
          />
          <Button
            className={styles.mobileListButton}
            type="text"
            size="small"
            icon={<X size={16} />}
            aria-label={t("projects.workspace.closeNavigation")}
            onClick={() => setMobileListOpen(false)}
          />
        </div>
        <div className={styles.taskSearchRow}>
          <Input.Search
            aria-label={t("projects.tasks.search")}
            placeholder={t("projects.tasks.search")}
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            allowClear
          />
          <Popover
            open={filtersOpen}
            onOpenChange={setFiltersOpen}
            trigger="click"
            placement="bottomLeft"
            title={t("projects.workspace.filters")}
            content={
              <div className={styles.taskFilterPanel}>
                {allTasks && !navigation && (
                  <Select
                    aria-label={t("projects.tasks.filterProject")}
                    placeholder={t("projects.tasks.filterProject")}
                    allowClear
                    value={projectFilter}
                    options={projectOptions.map((item) => ({
                      value: item.project_id,
                      label: item.name,
                    }))}
                    onChange={setProjectFilter}
                  />
                )}
                <Select
                  aria-label={t("projects.tasks.filterExpert")}
                  placeholder={t("projects.tasks.filterExpert")}
                  allowClear
                  value={expertFilter}
                  options={(resources?.experts ?? []).map((expert) => ({
                    value: expert.expert_id,
                    label: taskExpertLabel(expert, t),
                  }))}
                  onChange={setExpertFilter}
                />
                <Select
                  aria-label={t("projects.tasks.filterStatus")}
                  placeholder={t("projects.tasks.filterStatus")}
                  allowClear
                  value={statusFilter}
                  options={[
                    "queued",
                    "running",
                    "waiting_approval",
                    "waiting_answer",
                    "waiting_client_tool",
                    "paused",
                    "interrupted",
                    "completed",
                    "failed",
                    "cancelled",
                  ].map((status) => ({
                    value: status,
                    label: t(`projects.tasks.status.${status}`),
                  }))}
                  onChange={setStatusFilter}
                />
                <Segmented
                  block
                  aria-label={t("projects.tasks.listScope")}
                  value={archivedView ? "archived" : "active"}
                  options={[
                    { label: t("projects.tasks.activeList"), value: "active" },
                    {
                      label: t("projects.tasks.archivedList"),
                      value: "archived",
                    },
                  ]}
                  onChange={(value) => {
                    const next = new URLSearchParams(params);
                    next.delete("task");
                    if (value === "archived") next.set("archived", "true");
                    else next.delete("archived");
                    setParams(next);
                  }}
                />
              </div>
            }
          >
            <Button
              type="text"
              size="small"
              icon={<SlidersHorizontal size={16} />}
              aria-label={t("projects.workspace.filters")}
              aria-pressed={!!expertFilter || !!statusFilter || archivedView}
            />
          </Popover>
        </div>
        <div className={styles.taskRows}>
          {visibleList.length === 0 && !loading && (
            <Typography.Text type="secondary">
              {t("projects.tasks.emptyList")}
            </Typography.Text>
          )}
          {visibleList.map((task) => (
            <button
              key={task.task_id}
              type="button"
              aria-label={`${task.title} ${t(
                `projects.tasks.status.${task.status}`,
              )} ${
                task.selected_expert_id ?? t("projects.tasks.generalAssistant")
              }`}
              className={`${styles.taskItem} ${
                taskID === task.task_id ? styles.active : ""
              }`}
              onClick={() => selectTask(task.task_id)}
              disabled={sending}
            >
              <span>
                <strong>
                  {task.pinned && <Pin size={12} />} {task.title}
                </strong>
                <small>
                  {(resources?.experts ?? []).find(
                    (expert) => expert.expert_id === task.selected_expert_id,
                  )?.name ??
                    (task.selected_expert_id ||
                      t("projects.tasks.generalAssistant"))}
                  {" · "}
                  {t(`projects.tasks.status.${task.status}`)}
                  {" · "}
                  {formatServerIsoDateTime(task.updated_at, timeZone)}
                </small>
              </span>
            </button>
          ))}
          {hasMore && (
            <Button
              loading={loadingMore}
              onClick={() =>
                void loadMore().catch((err) =>
                  setLoadError(
                    apiErrorMessage(err, t("projects.tasks.failure"), t),
                  ),
                )
              }
            >
              {t("projects.tasks.loadMore")}
            </Button>
          )}
        </div>
      </div>
      <div className={styles.conversation}>
        <div
          className={`${styles.conversationNavigation} ${
            overview ? styles.overviewNavigation : ""
          }`}
        >
          <Button
            type="text"
            size="small"
            icon={<PanelLeft size={16} />}
            className={styles.mobileListButton}
            onClick={() => setMobileListOpen(true)}
          >
            {t("projects.workspace.navigation")}
          </Button>
          <span>{project?.name ?? t("projects.tasks.noProject")}</span>
        </div>
        {overview}
        <div className={styles.conversationBody} hidden={!!overview}>
          {detail && (
            <>
              <TaskWorkbench
                key={`workbench:${hostID ?? "local"}:${detail.task_id}`}
                docked={workspace}
                launcherClassName={styles.workbenchLauncher}
                projectRevision={project?.revision}
                onDirtyChange={onWorkbenchDirtyChange}
                task={detail}
                requestedTool={
                  workbenchTool?.taskID === detail.task_id
                    ? workbenchTool
                    : null
                }
              />
            </>
          )}
          {reconnecting && (
            <Typography.Text type="secondary">
              {t("projects.tasks.reconnecting")}
            </Typography.Text>
          )}
          {(error || loadError || detailError) && (
            <Alert
              type="error"
              showIcon
              message={error || loadError || detailError}
              action={
                <Button size="small" onClick={() => void refresh()}>
                  {t("projects.refresh")}
                </Button>
              }
            />
          )}
          <div
            className={styles.messages}
            onScroll={(event) => {
              const { scrollTop, clientHeight, scrollHeight } =
                event.currentTarget;
              followOutput.current =
                scrollHeight - scrollTop - clientHeight < 80;
            }}
            role="log"
            aria-label={t("projects.tasks.history")}
          >
            {loading ? (
              <Spin />
            ) : !detail ? (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description={t("projects.tasks.emptyConversation")}
              />
            ) : (
              <>
                <Space
                  className={styles.conversationHeader}
                  style={{ width: "100%", justifyContent: "space-between" }}
                  align="start"
                >
                  <Typography.Title
                    level={5}
                    editable={
                      !updating && {
                        onStart: () => {
                          renameRevision.current = detail.list_revision;
                        },
                        onChange: (title) =>
                          void updateTask(
                            { title },
                            renameRevision.current ?? detail.list_revision,
                          ),
                        maxLength: 128,
                        tooltip: t("projects.tasks.rename"),
                      }
                    }
                  >
                    {detail.title}
                  </Typography.Title>
                  <Space size="small" className={styles.headerActions}>
                    <Popover
                      trigger="click"
                      placement="bottomRight"
                      title={t("projects.workspace.conversationTools")}
                      content={
                        <Space
                          direction="vertical"
                          className={styles.conversationTools}
                        >
                          <Dropdown
                            menu={{
                              items: [
                                {
                                  key: "json",
                                  label: t("projects.tasks.exportJson"),
                                },
                                {
                                  key: "markdown",
                                  label: t("projects.tasks.exportMarkdown"),
                                },
                              ],
                              onClick: ({ key }) => {
                                void taskExtrasApi
                                  .export(
                                    detail.task_id,
                                    key as "json" | "markdown",
                                  )
                                  .then((blob) => {
                                    const url = URL.createObjectURL(blob);
                                    const anchor = document.createElement("a");
                                    anchor.href = url;
                                    anchor.download = `${detail.title.replace(
                                      /[\\/:*?"<>|]/g,
                                      "_",
                                    )}.${key === "json" ? "json" : "md"}`;
                                    anchor.click();
                                    setTimeout(
                                      () => URL.revokeObjectURL(url),
                                      1000,
                                    );
                                  })
                                  .catch((err) =>
                                    setError(
                                      apiErrorMessage(
                                        err,
                                        t("projects.tasks.failure"),
                                        t,
                                      ),
                                    ),
                                  );
                              },
                            }}
                          >
                            <Button size="small" icon={<Download size={14} />}>
                              {t("projects.tasks.export")}
                            </Button>
                          </Dropdown>
                          {!selectedExpert && (
                            <TaskMemory
                              key={`memory:${detail.task_id}`}
                              taskID={detail.task_id}
                              readOnly={detail.archived || projectArchived}
                            />
                          )}
                          {!!selectedExpert && (
                            <TaskExpertMemory
                              key={`${detail.task_id}:${selectedExpert}`}
                              taskID={detail.task_id}
                              expertID={selectedExpert}
                              projectID={projectID}
                              disabled={sending || updating}
                            />
                          )}
                          <Button
                            size="small"
                            icon={<GitBranch size={15} />}
                            disabled={
                              busy ||
                              sending ||
                              !detail.turns.some(
                                (turn) => turn.status === "completed",
                              )
                            }
                            onClick={() => {
                              const turn = [...detail.turns]
                                .reverse()
                                .find((item) => item.status === "completed");
                              if (turn) openBranch(turn, "fork");
                            }}
                          >
                            {t("projects.tasks.fork")}
                          </Button>
                          <Button
                            size="small"
                            icon={
                              detail.pinned ? (
                                <PinOff size={15} />
                              ) : (
                                <Pin size={15} />
                              )
                            }
                            disabled={updating || sending}
                            onClick={() =>
                              void updateTask({ pinned: !detail.pinned })
                            }
                          >
                            {t(
                              detail.pinned
                                ? "projects.tasks.unpin"
                                : "projects.tasks.pin",
                            )}
                          </Button>
                          <Button
                            size="small"
                            icon={<Shrink size={15} />}
                            disabled={compactDisabled}
                            onClick={() => void compact()}
                          >
                            {t("projects.tasks.compact")}
                          </Button>
                          <Button
                            size="small"
                            icon={
                              detail.archived ? (
                                <ArchiveRestore size={15} />
                              ) : (
                                <Archive size={15} />
                              )
                            }
                            disabled={busy || sending}
                            loading={updating}
                            title={
                              busy ? t("projects.tasks.archiveBusy") : undefined
                            }
                            onClick={() =>
                              void updateTask({ archived: !detail.archived })
                            }
                          >
                            {t(
                              detail.archived
                                ? "projects.tasks.restore"
                                : "projects.tasks.archive",
                            )}
                          </Button>
                          <Button
                            danger
                            size="small"
                            icon={<Trash2 size={15} />}
                            disabled={busy || sending || updating}
                            title={
                              busy ? t("projects.tasks.deleteBusy") : undefined
                            }
                            onClick={() => {
                              setDeleteError("");
                              setDeleteTarget(detail);
                            }}
                          >
                            {t("projects.tasks.delete")}
                          </Button>
                          <TaskContextUsage
                            engine={selectedEngine}
                            taskID={detail.task_id}
                            active={busy}
                            turns={detail.turns}
                          />
                        </Space>
                      }
                    >
                      <Button
                        size="small"
                        type="text"
                        icon={<MoreHorizontal size={17} />}
                        aria-label={t("projects.workspace.conversationTools")}
                      />
                    </Popover>
                  </Space>
                </Space>
                {detail.archived && (
                  <Alert
                    type="info"
                    message={t("projects.tasks.archivedHint")}
                  />
                )}
                {!!detail.configuration_events?.length && (
                  <Space direction="vertical" style={{ marginBottom: 12 }}>
                    {detail.configuration_events.map((event) => (
                      <Typography.Text key={event.event_id} type="secondary">
                        {t("projects.tasks.configurationChange", {
                          expert:
                            resources?.experts?.find(
                              (expert) =>
                                expert.expert_id === event.selected_expert_id,
                            )?.name ??
                            event.selected_expert_id ??
                            t("projects.tasks.generalAssistant"),
                          mode: t(
                            `projects.tasks.actionModes.${event.action_mode}`,
                          ),
                          revision: event.configuration_revision,
                        })}
                        {" · "}
                        {formatServerIsoDateTime(event.created_at, timeZone)}
                      </Typography.Text>
                    ))}
                  </Space>
                )}
                {detail.next_page_cursor && (
                  <Button
                    loading={loadingHistory}
                    onClick={() => void loadHistory()}
                  >
                    {t("projects.tasks.loadHistory")}
                  </Button>
                )}
                {detail.turns.map((turn) => (
                  <div className={styles.turn} key={turn.turn_id}>
                    <div className={styles.userMessage}>
                      <Typography.Text type="secondary">
                        {t("projects.tasks.you")}
                      </Typography.Text>
                      <div>{turn.prompt}</div>
                      <TaskInputImages attachments={turn.attachments ?? []} />
                    </div>
                    <div className={styles.assistantMessage}>
                      {turn.history_only && (
                        <Space wrap style={{ marginBottom: 8 }}>
                          <Tag>{t("projects.tasks.historyOnly")}</Tag>
                          {turn.source_task_id && (
                            <Button
                              type="link"
                              size="small"
                              onClick={() => selectTask(turn.source_task_id!)}
                            >
                              {t("projects.tasks.viewSource")}
                            </Button>
                          )}
                        </Space>
                      )}
                      <Space size="small">
                        <Typography.Text type="secondary">
                          {turn.expert_name ??
                            (turn.selected_expert_id ||
                              t("projects.tasks.generalAssistant"))}
                        </Typography.Text>
                        <Tag title={t("projects.tasks.engine")}>
                          {turn.execution_engine?.label ||
                            engineLabel(turn.execution_engine ?? OCTOP_ENGINE)}
                        </Tag>
                        {turn.action_mode && (
                          <Tag title={t("projects.tasks.actionMode")}>
                            {t(
                              `projects.tasks.actionModes.${turn.action_mode}`,
                            )}
                          </Tag>
                        )}
                        <Tag>
                          {t(
                            turn.cancel_requested &&
                              (turn.status === "queued" ||
                                turn.status === "running")
                              ? "projects.tasks.stopping"
                              : `projects.tasks.status.${turn.status}`,
                          )}
                        </Tag>
                      </Space>
                      <Typography.Paragraph type="secondary">
                        {turn.execution_engine?.actual_model_id ??
                        turn.execution_engine?.model_id ??
                        (turn.execution_engine?.engine_id === "acp"
                          ? null
                          : turn.model_ref)
                          ? t("projects.tasks.modelUsed", {
                              model:
                                turn.execution_engine?.actual_model_id ??
                                turn.execution_engine?.model_id ??
                                (turn.execution_engine?.engine_id === "acp"
                                  ? null
                                  : turn.model_ref),
                            })
                          : t("projects.tasks.engineDefaultModel")}
                        <TaskTurnUsage turn={turn} />
                      </Typography.Paragraph>
                      <TaskTurnContent
                        turn={turn}
                        onOpenToolUI={(callID) =>
                          setWorkbenchTool((previous) => ({
                            taskID: detail.task_id,
                            callID,
                            sequence: (previous?.sequence ?? 0) + 1,
                          }))
                        }
                        onOpenArtifacts={() =>
                          document
                            .getElementById(`task-artifacts-${turn.turn_id}`)
                            ?.scrollIntoView({ block: "nearest" })
                        }
                      />
                      {turn.status !== "running" &&
                        (turn.reply ||
                          turn.content_blocks.some(
                            (block) => block.type === "text" && block.text,
                          )) && (
                          <TaskVoiceRead
                            id={`${hostID ?? "local"}:${detail.task_id}:${
                              turn.turn_id
                            }`}
                            text={
                              turn.content_blocks
                                .filter((block) => block.type === "text")
                                .map((block) => block.text)
                                .join("\n\n") ||
                              turn.reply ||
                              ""
                            }
                          />
                        )}
                      {turn.status === "completed" && (
                        <Space wrap style={{ marginBottom: 10 }}>
                          <Button
                            size="small"
                            icon={<GitBranch size={14} />}
                            disabled={busy || sending || forking}
                            onClick={() => openBranch(turn, "fork")}
                          >
                            {t("projects.tasks.fork")}
                          </Button>
                          <Button
                            size="small"
                            icon={<Pencil size={14} />}
                            disabled={
                              busy || sending || forking || detail.archived
                            }
                            onClick={() => openBranch(turn, "edit")}
                          >
                            {t("projects.tasks.editBranch")}
                          </Button>
                          <Button
                            size="small"
                            icon={<RefreshCw size={14} />}
                            disabled={
                              busy || sending || forking || detail.archived
                            }
                            onClick={() => openBranch(turn, "regenerate")}
                          >
                            {t("projects.tasks.regenerate")}
                          </Button>
                        </Space>
                      )}
                      {!!turn.interactions?.length && (
                        <TaskInteractions
                          taskID={detail.task_id}
                          items={turn.interactions}
                          disabled={
                            reconnecting ||
                            turn.cancel_requested ||
                            turn.turn_id !== detail.active_turn_id
                          }
                          onChange={(incoming) => {
                            if (selectedTaskID.current === incoming.task_id)
                              applyWrite(incoming);
                          }}
                        />
                      )}
                      {turn.pause_requested &&
                        turn.status !== "paused" &&
                        turn.turn_id === detail.active_turn_id && (
                          <Typography.Paragraph type="secondary">
                            {t("projects.tasks.pausing")}
                          </Typography.Paragraph>
                        )}
                      {turn.artifacts?.length > 0 && (
                        <Space
                          id={`task-artifacts-${turn.turn_id}`}
                          direction="vertical"
                          aria-label={t("projects.tasks.artifacts")}
                        >
                          {turn.artifacts.map((artifact) => (
                            <Space
                              direction="vertical"
                              key={artifact.artifact_id}
                            >
                              <TaskMedia
                                taskID={detail.task_id}
                                artifact={artifact}
                              />
                              <Button
                                key={artifact.artifact_id}
                                icon={<Download size={14} />}
                                loading={downloading === artifact.artifact_id}
                                disabled={downloading !== null}
                                onClick={() => void downloadArtifact(artifact)}
                              >
                                {artifact.name}
                              </Button>
                            </Space>
                          ))}
                        </Space>
                      )}
                      {turn.recovery && (
                        <TaskRecoveryNotice
                          turn={turn}
                          onResume={
                            turn.turn_id === detail.active_turn_id &&
                            turn.execution_engine?.engine_id !== "acp"
                              ? () => void control()
                              : undefined
                          }
                          disabled={
                            reconnecting ||
                            stopping ||
                            controlling ||
                            turn.cancel_requested ||
                            !!detail.archived ||
                            projectArchived
                          }
                        />
                      )}
                      {turn.error_code && (
                        <Alert
                          type="error"
                          showIcon
                          message={t(`apiErrors.${turn.error_code}`)}
                        />
                      )}
                      {!busy &&
                        !detail.archived &&
                        !projectArchived &&
                        turn.turn_id === latestTurnID &&
                        !turn.history_only &&
                        (turn.status === "failed" ||
                          turn.status === "cancelled") && (
                          <Space direction="vertical" size="small">
                            <Typography.Text type="secondary">
                              {t("projects.tasks.retryHint")}
                            </Typography.Text>
                            <Button
                              loading={sending}
                              disabled={
                                queuedItems.length > 0 ||
                                !!queued?.sendingID ||
                                approvalSaving ||
                                webSearchSaving ||
                                directoryChanged
                              }
                              onClick={() => void retry(turn.turn_id)}
                            >
                              {t("projects.tasks.retry")}
                            </Button>
                          </Space>
                        )}
                    </div>
                  </div>
                ))}
              </>
            )}
            <div ref={end} />
          </div>
          <div
            className={styles.composer}
            onDragOver={(event) => {
              if (event.dataTransfer.types.includes("Files"))
                event.preventDefault();
            }}
            onDrop={(event) => {
              if (!event.dataTransfer.files.length) return;
              event.preventDefault();
              if (!composerDisabled)
                addAttachments(Array.from(event.dataTransfer.files));
            }}
          >
            <TaskComposer
              executionSettings={{
                scopeKey: draftKey,
                value: options,
                onChange: setOptions,
                project,
                disabled:
                  sending || updating || !!detail?.archived || projectArchived,
                selectedExpertID: selectedExpert,
                engine: selectedEngine,
                engineCatalog: resources?.execution_engines,
                onEngineChange: changeEngine,
              }}
              status={
                <>
                  {engineUnavailable ? (
                    <Alert
                      type="warning"
                      showIcon
                      message={t("projects.tasks.engineUnavailable")}
                      description={
                        hostID
                          ? t("projects.tasks.remoteEngineManagement")
                          : t("projects.tasks.engineSetupHint")
                      }
                    />
                  ) : null}
                  {engineImageConflict ? (
                    <Alert
                      type="warning"
                      showIcon
                      message={t("projects.tasks.engineImagesUnsupported")}
                    />
                  ) : null}
                  {externalEngine && capabilities.action_modes.length === 1 ? (
                    <Typography.Text type="secondary">
                      {t("projects.tasks.engineCraftOnly")}
                    </Typography.Text>
                  ) : null}
                  {engineClientDirectoryConflict ? (
                    <Alert
                      type="warning"
                      showIcon
                      message={t(
                        "projects.tasks.engineClientDirectoryUnsupported",
                      )}
                    />
                  ) : null}
                  {selectedExpertUnavailable && (
                    <Alert
                      type="warning"
                      showIcon
                      message={t("projects.tasks.expertUnavailable")}
                      description={t("projects.tasks.expertUnavailableHint")}
                    />
                  )}
                  {resourceError && (
                    <Alert
                      type="error"
                      showIcon
                      message={resourceError}
                      action={
                        <Button
                          size="small"
                          onClick={() =>
                            setResourceVersion((version) => version + 1)
                          }
                        >
                          {t("projects.refresh")}
                        </Button>
                      }
                    />
                  )}
                  {directoryChanged && (
                    <Alert
                      type="warning"
                      showIcon
                      message={t("projects.tasks.directoryChanged")}
                      description={
                        <Space direction="vertical">
                          <Typography.Text>
                            {t("projects.tasks.directoryRange", {
                              directory:
                                project?.directory ??
                                t("projects.tasks.taskSpace"),
                              clientDirectory:
                                project?.client_directory ??
                                t("projects.tasks.notConfigured"),
                            })}
                          </Typography.Text>
                          <Button
                            onClick={() => setConfirmedDirectory(directoryKey)}
                          >
                            {t("projects.tasks.confirmDirectory")}
                          </Button>
                        </Space>
                      }
                    />
                  )}
                  <div className={styles.composerRuntime}>
                    {busy && (
                      <>
                        {activeCapabilities.pause &&
                          activeTurn?.status !== "interrupted" && (
                            <Button
                              icon={
                                activeTurn?.pause_requested ||
                                activeTurn?.status === "paused" ? (
                                  <Play size={15} />
                                ) : (
                                  <Pause size={15} />
                                )
                              }
                              loading={controlling}
                              disabled={
                                compacting ||
                                reconnecting ||
                                stopping ||
                                activeTurn?.cancel_requested
                              }
                              title={t("projects.tasks.controlHint")}
                              onClick={() => void control()}
                            >
                              {t(
                                activeTurn?.pause_requested ||
                                  activeTurn?.status === "paused"
                                  ? "projects.tasks.resume"
                                  : "projects.tasks.pause",
                              )}
                            </Button>
                          )}
                        <Button
                          icon={<Square size={15} />}
                          loading={stopping}
                          disabled={
                            !activeCapabilities.cancel ||
                            !!queued?.sendingID ||
                            detail?.turns.some(
                              (turn) =>
                                turn.turn_id === detail.active_turn_id &&
                                turn.cancel_requested,
                            )
                          }
                          onClick={() => void stop()}
                        >
                          {t("projects.tasks.stop")}
                        </Button>
                      </>
                    )}
                    {(queuedItems.length > 0 || queued?.error) && (
                      <Popover
                        trigger="click"
                        placement="topLeft"
                        title={t("chat.queue.label")}
                        content={
                          <div className={styles.queuePopover}>
                            {!!queuedItems.length && (
                              <div
                                className={styles.queuedMessages}
                                role="list"
                                aria-label={t("chat.queue.label")}
                              >
                                {queuedItems.map((item, index) => (
                                  <div
                                    className={styles.queuedMessageItem}
                                    role="listitem"
                                    key={item.id}
                                  >
                                    <Typography.Text type="secondary">
                                      {index + 1}
                                    </Typography.Text>
                                    <Button
                                      type="text"
                                      className={styles.queuedMessageBody}
                                      aria-label={t("chat.queue.reclaim")}
                                      title={t("chat.queue.reclaim")}
                                      disabled={
                                        queued?.sendingID === item.id ||
                                        composerDisabled
                                      }
                                      onClick={() => reclaimQueued(item.id)}
                                    >
                                      <Typography.Text ellipsis>
                                        {item.body.prompt ||
                                          t("chat.queue.attachmentsOnly", {
                                            count: item.attachments.length,
                                          })}
                                      </Typography.Text>
                                      {!!item.attachments.length && (
                                        <span>
                                          <Paperclip size={12} />{" "}
                                          {item.attachments.length}
                                        </span>
                                      )}
                                    </Button>
                                    <Button
                                      type="text"
                                      size="small"
                                      icon={<X size={14} />}
                                      aria-label={t("chat.queue.remove")}
                                      disabled={queued?.sendingID === item.id}
                                      onClick={() =>
                                        taskID && queue.remove(taskID, item.id)
                                      }
                                    />
                                  </div>
                                ))}
                                {queued?.error && (
                                  <Alert
                                    type="error"
                                    showIcon
                                    message={apiErrorMessage(
                                      queued.error,
                                      t("chat.queue.flushFailed"),
                                      t,
                                    )}
                                    action={
                                      <Button
                                        size="small"
                                        disabled={
                                          composerDisabled || !!queued.sendingID
                                        }
                                        onClick={() =>
                                          taskID && queue.retry(taskID)
                                        }
                                      >
                                        {t("projects.tasks.queueRetry")}
                                      </Button>
                                    }
                                  />
                                )}
                              </div>
                            )}
                          </div>
                        }
                      >
                        <Button
                          size="small"
                          type="text"
                          danger={!!queued?.error}
                        >
                          {t("projects.composer.pendingCount", {
                            count: queuedItems.length,
                          })}
                        </Button>
                      </Popover>
                    )}
                  </div>
                </>
              }
              identity={
                <>
                  <Space wrap size="small">
                    <Select
                      aria-label={t("projects.tasks.expert")}
                      size="small"
                      style={{ width: 145 }}
                      loading={!resources && !resourceError}
                      onOpenChange={(open) => {
                        if (open) setResourceVersion((version) => version + 1);
                      }}
                      disabled={selectionDisabled}
                      value={selectedExpert ?? ""}
                      options={[
                        {
                          value: "",
                          label: t("projects.tasks.noExpert"),
                        },
                        ...(resources?.experts ?? []).map((expert) => ({
                          value: expert.expert_id,
                          label: taskExpertLabel(expert, t),
                        })),
                        ...(selectedExpert &&
                        !resources?.experts?.some(
                          (e) => e.expert_id === selectedExpert,
                        )
                          ? [
                              {
                                value: selectedExpert,
                                label: selectedExpertUnavailable
                                  ? t("projects.tasks.expertUnavailable")
                                  : selectedExpert,
                                disabled: true,
                              },
                            ]
                          : []),
                      ]}
                      onChange={(id) => {
                        if (taskID)
                          void updateTask({ selected_expert_id: id || null });
                        else setExpertID(id || null);
                      }}
                    />
                    <TaskEngineSelect
                      value={selectedEngine}
                      catalog={resources?.execution_engines}
                      disabled={selectionDisabled || inspectingEngine}
                      loading={!resources && !resourceError}
                      hostID={hostID}
                      onChange={changeEngine}
                      onRefresh={() =>
                        setResourceVersion((version) => version + 1)
                      }
                    />
                    {externalEngine ? (
                      <Button
                        size="small"
                        type="text"
                        loading={inspectingEngine}
                        title={t("projects.tasks.inspectEngineHint")}
                        disabled={selectionDisabled || engineUnavailable}
                        onClick={() => void inspectEngine()}
                      >
                        {t("projects.tasks.inspectEngine")}
                      </Button>
                    ) : null}
                  </Space>
                </>
              }
              mode={
                <Segmented
                  aria-label={t("projects.tasks.actionMode")}
                  disabled={selectionDisabled}
                  value={selectedMode}
                  options={["ask", "plan", "craft"].map((mode) => ({
                    value: mode,
                    disabled: !capabilities.action_modes.includes(
                      mode as TaskActionMode,
                    ),
                    label: t(`projects.tasks.actionModes.${mode}`),
                  }))}
                  onChange={(mode) => {
                    if (taskID)
                      void updateTask({
                        action_mode: mode as TaskActionMode,
                      });
                    else setActionMode(mode as TaskActionMode);
                  }}
                />
              }
              webSearch={
                <>
                  {externalEngine ? (
                    <Typography.Text type="secondary">
                      {t("projects.tasks.engineWebSearch")}
                    </Typography.Text>
                  ) : (
                    <TaskWebSearch
                      compact
                      key={`web-search:${projectID ?? ""}:${taskID ?? "new"}`}
                      task={detail}
                      draft={webSearchEnabled}
                      onDraftChange={setWebSearchEnabled}
                      onChange={(incoming) => {
                        upsertTask(incoming);
                        applyWrite(incoming);
                      }}
                      onBusyChange={(saving) => {
                        if (taskID) queue.hold(taskID, saving);
                      }}
                      disabled={
                        sending ||
                        webSearchSaving ||
                        approvalSaving ||
                        updating ||
                        !!queued?.sendingID ||
                        reconnecting ||
                        !!detail?.archived ||
                        projectArchived ||
                        !!(taskID && !detail)
                      }
                    />
                  )}
                </>
              }
              approval={
                <>
                  <TaskApprovalModePicker
                    compact
                    onBusyChange={setApprovalSaving}
                    disabled={
                      sending || webSearchSaving || !capabilities.permissions
                    }
                  />
                </>
              }
              attachments={
                <>
                  {!!images.items.length && (
                    <div className={styles.inputImages}>
                      {images.items.map((image) => (
                        <div className={styles.inputImage} key={image.id}>
                          {image.isImage && image.url ? (
                            <Image
                              src={image.url}
                              alt={image.name}
                              width={112}
                              height={88}
                            />
                          ) : (
                            <div className={styles.inputFilePreview}>
                              <FileText size={32} />
                            </div>
                          )}
                          <Button
                            className={styles.removeImage}
                            size="small"
                            icon={<X size={14} />}
                            aria-label={t("projects.tasks.removeImage", {
                              name: image.name,
                            })}
                            disabled={sending}
                            onClick={() => images.remove(image.id)}
                          />
                          <Typography.Text ellipsis title={image.name}>
                            {image.name}
                          </Typography.Text>
                          {!image.isImage && (
                            <Button
                              size="small"
                              icon={<Download size={14} />}
                              href={image.url}
                              download={image.name}
                              disabled={!image.url}
                            >
                              {t("common.download")}
                            </Button>
                          )}
                          {image.error ? (
                            <Typography.Text type="danger" role="alert">
                              {image.error}
                            </Typography.Text>
                          ) : !image.attachment ? (
                            <Typography.Text type="secondary">
                              <Spin size="small" />{" "}
                              {t("projects.tasks.imageUploading")}
                            </Typography.Text>
                          ) : null}
                        </div>
                      ))}
                    </div>
                  )}
                  {images.error && (
                    <Alert type="error" showIcon message={images.error} />
                  )}
                </>
              }
              actions={
                <>
                  <div className={styles.composerAdd}>
                    <input
                      ref={imageInput}
                      className={styles.imageFileInput}
                      type="file"
                      multiple
                      aria-label={t("projects.tasks.addImages")}
                      disabled={
                        composerDisabled ||
                        images.items.length >= MAX_TASK_IMAGES
                      }
                      onChange={(event) => {
                        addAttachments(Array.from(event.target.files ?? []));
                        event.target.value = "";
                      }}
                    />
                    <Button
                      size="small"
                      icon={<Paperclip size={15} />}
                      disabled={
                        composerDisabled ||
                        images.items.length >= MAX_TASK_IMAGES
                      }
                      onClick={() => imageInput.current?.click()}
                    >
                      {t("projects.tasks.addImages")}
                    </Button>
                  </div>
                  <TaskComposerResourceTriggers
                    controller={composerResources}
                  />
                  <TaskPromptPolish
                    compact
                    key={`polish:${hostID ?? "local"}:${taskID ?? "new"}`}
                    text={draft}
                    taskID={taskID ?? undefined}
                    expertID={selectedExpert}
                    modelRef={externalEngine ? undefined : options.model_ref}
                    disabled={composerDisabled || externalEngine}
                    onApply={(text) => {
                      setDraft(text);
                      queue.drafts.current.set(draftKey, text);
                    }}
                  />
                  <TaskVoiceInput
                    compact
                    key={`voice:${taskID ?? "new"}:${
                      projectID ?? "standalone"
                    }`}
                    disabled={composerDisabled}
                    onText={(text) =>
                      setDraft((current) => {
                        const next = `${current}${
                          current ? "\n" : ""
                        }${text}`.slice(0, 32768);
                        queue.drafts.current.set(draftKey, next);
                        return next;
                      })
                    }
                  />
                </>
              }
              send={
                <>
                  <Button
                    type="primary"
                    icon={<Send size={16} />}
                    loading={sending}
                    disabled={
                      (!draft.trim() && !images.attachmentIDs.length) ||
                      images.blocked ||
                      engineUnavailable ||
                      engineImageConflict ||
                      engineClientDirectoryConflict ||
                      inspectingEngine ||
                      approvalSaving ||
                      webSearchSaving ||
                      updating ||
                      !!deleteTarget ||
                      loading ||
                      directoryChanged ||
                      !!detail?.archived ||
                      projectArchived ||
                      !!(taskID && !detail)
                    }
                    onClick={() => void send()}
                  >
                    {t(
                      busy || queuedItems.length > 0 || queued?.sendingID
                        ? "chat.queue.action"
                        : "projects.tasks.send",
                    )}
                  </Button>
                </>
              }
            >
              <TaskComposerResources controller={composerResources} />
              <Input.TextArea
                ref={composerInput}
                aria-label={t("projects.tasks.message")}
                value={draft}
                maxLength={32768}
                autoSize={{ minRows: 2, maxRows: 8 }}
                disabled={composerDisabled}
                placeholder={t(
                  detail
                    ? "projects.tasks.continuePlaceholder"
                    : "projects.tasks.placeholder",
                )}
                onChange={(event) => {
                  editDraft(event.target.value);
                }}
                onSelect={composerResources.onCursorChange}
                onClick={composerResources.onCursorChange}
                onPaste={(event) => {
                  const files = Array.from(event.clipboardData.files);
                  if (files.length) {
                    event.preventDefault();
                    if (!composerDisabled) addAttachments(files);
                  }
                }}
                onKeyDown={(event) => {
                  if (composerResources.onKeyDown(event)) return;
                  if (
                    event.key === "Enter" &&
                    !event.shiftKey &&
                    !isImeComposing(event)
                  ) {
                    event.preventDefault();
                    void send();
                  }
                }}
              />
            </TaskComposer>
          </div>
        </div>
      </div>
      <Modal
        title={t("projects.tasks.branchTitle")}
        okText={t("common.confirm")}
        cancelText={t("common.cancel")}
        open={branch !== null}
        onCancel={() => {
          if (!forking) setBranch(null);
        }}
        onOk={() => void createBranch()}
        confirmLoading={forking}
        okButtonProps={{
          disabled: branch?.operation === "edit" && !branch.prompt?.trim(),
        }}
        cancelButtonProps={{ disabled: forking }}
        closable={!forking}
        maskClosable={!forking}
      >
        <Typography.Paragraph>
          {t("projects.tasks.forkHint")}
        </Typography.Paragraph>
        <Space direction="vertical" style={{ width: "100%" }}>
          <Typography.Text>{t("projects.tasks.branchProject")}</Typography.Text>
          <Select
            aria-label={t("projects.tasks.branchProject")}
            style={{ width: "100%" }}
            disabled={forking}
            value={branch?.project_id ?? ""}
            options={[
              { value: "", label: t("projects.tasks.noProject") },
              ...(project &&
              !projectOptions.some(
                (item) => item.project_id === project.project_id,
              )
                ? [project]
                : []
              )
                .concat(projectOptions)
                .filter((item) => !item.archived)
                .map((item) => ({ value: item.project_id, label: item.name })),
            ]}
            onChange={(id) =>
              setBranch((current) =>
                current ? { ...current, project_id: id || null } : current,
              )
            }
          />
          <Typography.Text>{t("projects.tasks.expert")}</Typography.Text>
          <Select
            aria-label={t("projects.tasks.branchExpert")}
            style={{ width: "100%" }}
            disabled={forking}
            value={branch?.selected_expert_id ?? ""}
            options={[
              { value: "", label: t("projects.tasks.noExpert") },
              ...(resources?.experts ?? []).map((expert) => ({
                value: expert.expert_id,
                label: taskExpertLabel(expert, t),
              })),
            ]}
            onChange={(id) =>
              setBranch((current) =>
                current
                  ? { ...current, selected_expert_id: id || null }
                  : current,
              )
            }
          />
          <Typography.Text>{t("projects.tasks.engine")}</Typography.Text>
          <TaskEngineSelect
            branch
            value={branch?.engine ?? detail?.engine ?? OCTOP_ENGINE}
            catalog={resources?.execution_engines}
            disabled={forking}
            hostID={hostID}
            onChange={(engine) =>
              setBranch((current) =>
                current
                  ? {
                      ...current,
                      engine,
                      action_mode: engineCapabilities(
                        engine,
                        resources?.execution_engines,
                      ).action_modes.includes(selectedMode)
                        ? selectedMode
                        : engineCapabilities(
                            engine,
                            resources?.execution_engines,
                          ).action_modes[0] ?? "craft",
                    }
                  : current,
              )
            }
          />
          {branch?.operation !== "fork" && (
            <>
              <Typography.Text>
                {t("projects.tasks.branchPrompt")}
              </Typography.Text>
              <Input.TextArea
                aria-label={t("projects.tasks.branchPrompt")}
                value={branch?.prompt}
                disabled={forking}
                readOnly={branch?.operation === "regenerate"}
                onChange={(event) =>
                  setBranch((current) =>
                    current
                      ? { ...current, prompt: event.target.value }
                      : current,
                  )
                }
                autoSize={{ minRows: 3, maxRows: 8 }}
              />
            </>
          )}
        </Space>
      </Modal>
      <Modal
        title={t("projects.tasks.delete")}
        open={deleteTarget !== null}
        onCancel={() => {
          if (!deleting) setDeleteTarget(null);
        }}
        onOk={() => void deleteTask()}
        okText={t("projects.confirmDelete")}
        cancelText={t("projects.cancel")}
        okButtonProps={{ danger: true }}
        confirmLoading={deleting}
        cancelButtonProps={{ disabled: deleting }}
        closable={!deleting}
        maskClosable={!deleting}
      >
        <Typography.Paragraph>
          {t("projects.tasks.deleteConfirm", { name: deleteTarget?.title })}
        </Typography.Paragraph>
        {deleteError && <Alert type="error" showIcon message={deleteError} />}
      </Modal>
    </section>
  );
}
