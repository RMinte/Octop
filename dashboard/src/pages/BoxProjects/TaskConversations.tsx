import { useCallback, useEffect, useRef, useState } from "react";
import {
  Alert,
  Button,
  Empty,
  Image,
  Input,
  Modal,
  Segmented,
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
  MessageSquare,
  Plus,
  Pause,
  Play,
  Send,
  Square,
  Trash2,
  X,
} from "lucide-react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";
import {
  projectTasksApi,
  type TaskMessage,
  type TaskCreate,
  type TaskApprovalMode,
  type TaskExecutionOptions,
  type TaskArtifact,
  type Project,
  type TaskSummary,
} from "../../api/modules/projects";
import { useServerTimezone } from "../../hooks/useServerTimezone";
import { apiErrorMessage } from "../../utils/apiError";
import { formatServerIsoDateTime } from "../../utils/formatMessageTime";
import styles from "./index.module.less";
import { useTaskList } from "./useProjectLists";
import { useTaskConversation } from "./useTaskConversation";
import TaskExecutionSettings from "./TaskExecutionSettings";
import TaskTurnContent from "./TaskTurnContent";
import TaskInteractions from "./TaskInteractions";
import TaskApprovalModePicker from "./TaskApprovalMode";
import TaskWebSearch from "./TaskWebSearch";
import TaskInputImages from "./TaskInputImages";
import { MAX_TASK_IMAGES, useTaskImages } from "./useTaskImages";
import {
  useTaskMessageQueue,
  type TaskMessageQueue,
} from "./useTaskMessageQueue";

export default function TaskConversations({
  projectID,
  project,
  queue: sharedQueue,
}: {
  projectID: string | null;
  project?: Project;
  queue?: TaskMessageQueue;
}) {
  const { t } = useTranslation();
  const localQueue = useTaskMessageQueue();
  const queue = sharedQueue ?? localQueue;
  const clearQueue = queue.clear;
  const timeZone = useServerTimezone();
  const [params, setParams] = useSearchParams();
  const taskID = params.get("task");
  const queued = taskID ? queue.queues[taskID] : undefined;
  const queuedItems = queued?.items ?? [];
  const queueDelivery = taskID ? queue.delivered[taskID] : undefined;
  const archivedView = params.get("archived") === "true";
  const {
    items: list,
    refresh: refreshList,
    loadMore,
    hasMore,
    loadingMore,
    upsert: upsertTask,
    remove: removeTask,
    isDeleted,
  } = useTaskList(projectID, archivedView);
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
  const [error, setError] = useState("");
  const [loadError, setLoadError] = useState("");
  const alive = useRef(true);
  const selectedTaskID = useRef(taskID);
  selectedTaskID.current = taskID;
  const selectedProjectID = useRef(projectID);
  selectedProjectID.current = projectID;
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  const [draft, setDraft] = useState("");
  const images = useTaskImages(`${projectID ?? ""}:${taskID ?? "new"}`);
  const imageInput = useRef<HTMLInputElement>(null);
  const [options, setOptions] = useState<TaskExecutionOptions>({});
  const [approvalMode, setApprovalMode] =
    useState<TaskApprovalMode>("ask_for_approval");
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
  const control = async () => {
    if (!detail || !activeTurn || controlInFlight.current) return;
    controlInFlight.current = true;
    setControlling(true);
    const paused = !(
      activeTurn.pause_requested || activeTurn.status === "paused"
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
  const composerDisabled =
    sending ||
    loading ||
    !!detail?.archived ||
    projectArchived ||
    !!(taskID && !detail);
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
    const next = new URLSearchParams(params);
    next.delete("task");
    setParams(next, { replace: true });
  }, [taskDeleted, taskID, clearQueue, params, setParams]);

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
    setDraft("");
    setOptions({});
    setApprovalMode("ask_for_approval");
    setApprovalSaving(false);
    setWebSearchEnabled(true);
    pending.current = null;
    retryRequest.current = null;
    followOutput.current = true;
  }, [taskID, projectID]);

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
      detail?.archived ||
      projectArchived ||
      deleteTarget ||
      sending ||
      approvalSaving ||
      webSearchSaving ||
      inFlight.current ||
      (taskID && !detail)
    )
      return;
    const target = taskID ?? `new:${projectID ?? ""}`;
    if (
      pending.current?.target !== target ||
      pending.current.body.prompt !== prompt ||
      JSON.stringify(pending.current.body.attachment_ids ?? []) !==
        JSON.stringify(attachmentIDs) ||
      JSON.stringify(pending.current.body.options ?? {}) !==
        JSON.stringify(options) ||
      (!taskID &&
        ((pending.current.body as TaskCreate).approval_mode !== approvalMode ||
          (pending.current.body as TaskCreate).web_search_enabled !==
            webSearchEnabled))
    ) {
      pending.current = {
        target,
        body: {
          request_id: crypto.randomUUID(),
          prompt,
          ...(attachmentIDs.length ? { attachment_ids: attachmentIDs } : {}),
          ...(!taskID
            ? {
                approval_mode: approvalMode,
                web_search_enabled: webSearchEnabled,
              }
            : {}),
          ...(JSON.stringify(options) !== "{}" ? { options } : {}),
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
      images.clear();
      setOptions({});
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
      images.clear();
      setOptions({});
      pending.current = null;
      if (!taskID) selectTask(result.task_id);
      void refreshList().catch((err) => {
        if (alive.current && selectedProjectID.current === projectID)
          setLoadError(apiErrorMessage(err, t("projects.tasks.failure"), t));
      });
    } catch (err) {
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
      deleteTarget ||
      sending ||
      approvalSaving ||
      webSearchSaving ||
      inFlight.current
    )
      return;
    const optionsKey = JSON.stringify(options);
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
        options,
      );
      if (
        !alive.current ||
        selectedTaskID.current !== taskID ||
        selectedProjectID.current !== projectID
      )
        return;
      applyWrite(result);
      setOptions({});
      retryRequest.current = null;
    } catch (err) {
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
    changes: {
      title?: string;
      archived?: boolean;
    },
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
        const next = new URLSearchParams(params);
        next.delete("task");
        setParams(next);
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

  return (
    <section
      className={`${styles.taskSection} ${
        projectID ? "" : styles.standaloneTasks
      }`}
      aria-label={t("projects.tasks.title")}
    >
      <div className={styles.taskSidebar}>
        <Segmented
          block
          aria-label={t("projects.tasks.listScope")}
          value={archivedView ? "archived" : "active"}
          options={[
            { label: t("projects.tasks.activeList"), value: "active" },
            { label: t("projects.tasks.archivedList"), value: "archived" },
          ]}
          onChange={(value) => {
            const next = new URLSearchParams(params);
            next.delete("task");
            if (value === "archived") next.set("archived", "true");
            else next.delete("archived");
            setParams(next);
          }}
        />
        <Button
          icon={<Plus size={16} />}
          onClick={() => {
            if (taskID) queue.clear(taskID);
            selectTask(null);
          }}
          disabled={sending || !!queued?.sendingID || projectArchived}
        >
          {t("projects.tasks.new")}
        </Button>
        {list.length === 0 && !loading && (
          <Typography.Text type="secondary">
            {t("projects.tasks.emptyList")}
          </Typography.Text>
        )}
        {list.map((task) => (
          <button
            key={task.task_id}
            type="button"
            className={`${styles.taskItem} ${
              taskID === task.task_id ? styles.active : ""
            }`}
            onClick={() => selectTask(task.task_id)}
            disabled={sending}
          >
            <MessageSquare size={16} />
            <span>
              <strong>{task.title}</strong>
              <small>
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
      <div className={styles.conversation}>
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
            followOutput.current = scrollHeight - scrollTop - clientHeight < 80;
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
                <Space wrap>
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
                    title={busy ? t("projects.tasks.archiveBusy") : undefined}
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
                    title={busy ? t("projects.tasks.deleteBusy") : undefined}
                    onClick={() => {
                      setDeleteError("");
                      setDeleteTarget(detail);
                    }}
                  >
                    {t("projects.tasks.delete")}
                  </Button>
                </Space>
              </Space>
              {busy && (
                <Typography.Paragraph type="secondary">
                  {t("projects.tasks.deleteBusy")}
                </Typography.Paragraph>
              )}
              {detail.archived && (
                <Alert type="info" message={t("projects.tasks.archivedHint")} />
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
                    <Space size="small">
                      <Typography.Text type="secondary">
                        {t("projects.tasks.assistant")}
                      </Typography.Text>
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
                      {t("projects.tasks.modelUsed", { model: turn.model_ref })}
                    </Typography.Paragraph>
                    <TaskTurnContent turn={turn} />
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
                        direction="vertical"
                        aria-label={t("projects.tasks.artifacts")}
                      >
                        {turn.artifacts.map((artifact) => (
                          <Button
                            key={artifact.artifact_id}
                            icon={<Download size={14} />}
                            loading={downloading === artifact.artifact_id}
                            disabled={downloading !== null}
                            onClick={() => void downloadArtifact(artifact)}
                          >
                            {artifact.name}
                          </Button>
                        ))}
                      </Space>
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
                              webSearchSaving
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
              images.add(Array.from(event.dataTransfer.files));
          }}
        >
          <TaskApprovalModePicker
            key={`${projectID ?? ""}:${taskID ?? "new"}`}
            task={detail}
            draft={approvalMode}
            onDraftChange={setApprovalMode}
            onChange={applyWrite}
            onBusyChange={setApprovalSaving}
            disabled={
              sending ||
              webSearchSaving ||
              reconnecting ||
              !!detail?.archived ||
              projectArchived ||
              !!(taskID && !detail)
            }
          />
          <TaskWebSearch
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
          <TaskExecutionSettings
            value={options}
            onChange={setOptions}
            project={project}
            disabled={sending || !!detail?.archived || projectArchived}
          />
          <Input.TextArea
            aria-label={t("projects.tasks.message")}
            value={draft}
            maxLength={32768}
            autoSize={{ minRows: 3, maxRows: 8 }}
            disabled={composerDisabled}
            placeholder={t(
              detail
                ? "projects.tasks.continuePlaceholder"
                : "projects.tasks.placeholder",
            )}
            onChange={(event) => setDraft(event.target.value)}
            onPaste={(event) => {
              const files = Array.from(event.clipboardData.files);
              if (files.length) {
                event.preventDefault();
                if (!composerDisabled) images.add(files);
              }
            }}
            onKeyDown={(event) => {
              if (
                event.key === "Enter" &&
                !event.shiftKey &&
                !event.nativeEvent.isComposing
              ) {
                event.preventDefault();
                void send();
              }
            }}
          />
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
                      <Spin size="small" /> {t("projects.tasks.imageUploading")}
                    </Typography.Text>
                  ) : null}
                </div>
              ))}
            </div>
          )}
          {images.error && (
            <Alert type="error" showIcon message={images.error} />
          )}
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
                    disabled={queued?.sendingID === item.id || composerDisabled}
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
                        <Paperclip size={12} /> {item.attachments.length}
                      </span>
                    )}
                  </Button>
                  <Button
                    type="text"
                    size="small"
                    icon={<X size={14} />}
                    aria-label={t("chat.queue.remove")}
                    disabled={queued?.sendingID === item.id}
                    onClick={() => taskID && queue.remove(taskID, item.id)}
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
                      disabled={composerDisabled || !!queued.sendingID}
                      onClick={() => taskID && queue.retry(taskID)}
                    >
                      {t("projects.tasks.queueRetry")}
                    </Button>
                  }
                />
              )}
            </div>
          )}
          <div className={styles.imageUpload}>
            <input
              ref={imageInput}
              className={styles.imageFileInput}
              type="file"
              multiple
              aria-label={t("projects.tasks.addImages")}
              disabled={
                composerDisabled || images.items.length >= MAX_TASK_IMAGES
              }
              onChange={(event) => {
                images.add(Array.from(event.target.files ?? []));
                event.target.value = "";
              }}
            />
            <Button
              size="small"
              icon={<Paperclip size={15} />}
              disabled={
                composerDisabled || images.items.length >= MAX_TASK_IMAGES
              }
              onClick={() => imageInput.current?.click()}
            >
              {t("projects.tasks.addImages")}
            </Button>
            <Typography.Text type="secondary">
              {t("projects.tasks.imageHint")}
            </Typography.Text>
          </div>
          <div className={styles.composerFooter}>
            <Typography.Text type="secondary">
              {t(
                projectID
                  ? "projects.tasks.inherit"
                  : "projects.tasks.standaloneHint",
              )}
            </Typography.Text>
            <Space>
              {busy && (
                <>
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
                      reconnecting || stopping || activeTurn?.cancel_requested
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
                  <Button
                    icon={<Square size={15} />}
                    loading={stopping}
                    disabled={
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
              <Button
                type="primary"
                icon={<Send size={16} />}
                loading={sending}
                disabled={
                  (!draft.trim() && !images.attachmentIDs.length) ||
                  images.blocked ||
                  approvalSaving ||
                  webSearchSaving ||
                  !!deleteTarget ||
                  loading ||
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
            </Space>
          </div>
        </div>
      </div>
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
