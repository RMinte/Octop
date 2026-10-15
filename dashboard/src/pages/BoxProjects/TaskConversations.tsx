import { useCallback, useEffect, useRef, useState } from "react";
import {
  Alert,
  Button,
  Empty,
  Input,
  Segmented,
  Space,
  Spin,
  Tag,
  Typography,
} from "antd";
import {
  Archive,
  ArchiveRestore,
  MessageSquare,
  Plus,
  Send,
  Square,
} from "lucide-react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";
import {
  projectTasksApi,
  type TaskMessage,
  type TaskExecutionOptions,
  type Project,
} from "../../api/modules/projects";
import LazyMarkdown from "../../components/Markdown/LazyMarkdown";
import { useServerTimezone } from "../../hooks/useServerTimezone";
import { apiErrorMessage } from "../../utils/apiError";
import { formatServerIsoDateTime } from "../../utils/formatMessageTime";
import styles from "./index.module.less";
import { useTaskList } from "./useProjectLists";
import { useTaskConversation } from "./useTaskConversation";
import TaskExecutionSettings from "./TaskExecutionSettings";

export default function TaskConversations({
  projectID,
  project,
}: {
  projectID: string | null;
  project?: Project;
}) {
  const { t } = useTranslation();
  const timeZone = useServerTimezone();
  const [params, setParams] = useSearchParams();
  const taskID = params.get("task");
  const archivedView = params.get("archived") === "true";
  const {
    items: list,
    refresh: refreshList,
    loadMore,
    hasMore,
    loadingMore,
    upsert: upsertTask,
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
  const [options, setOptions] = useState<TaskExecutionOptions>({});
  const [updating, setUpdating] = useState(false);
  const [sending, setSending] = useState(false);
  const [stopping, setStopping] = useState(false);
  const inFlight = useRef(false);
  const pending = useRef<{ target: string; body: TaskMessage } | null>(null);
  const end = useRef<HTMLDivElement>(null);
  const followOutput = useRef(true);
  const retryRequest = useRef<{
    turnID: string;
    requestID: string;
    optionsKey: string;
  } | null>(null);
  const busy = !!detail?.active_turn_id;
  const listSummary = useRef("");
  const renameRevision = useRef<number | null>(null);

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
    if (
      !prompt ||
      busy ||
      detail?.archived ||
      sending ||
      inFlight.current ||
      (taskID && !detail)
    )
      return;
    inFlight.current = true;
    setSending(true);
    setError("");
    const target = taskID ?? `new:${projectID ?? ""}`;
    if (
      pending.current?.target !== target ||
      pending.current.body.prompt !== prompt ||
      JSON.stringify(pending.current.body.options ?? {}) !==
        JSON.stringify(options)
    ) {
      pending.current = {
        target,
        body: {
          request_id: crypto.randomUUID(),
          prompt,
          ...(JSON.stringify(options) !== "{}" ? { options } : {}),
        },
      };
    }
    try {
      const result = taskID
        ? await projectTasksApi.send(taskID, pending.current.body)
        : await projectTasksApi.create(projectID, pending.current.body);
      if (
        !alive.current ||
        selectedTaskID.current !== taskID ||
        selectedProjectID.current !== projectID
      )
        return;
      upsertTask(result);
      applyWrite(result);
      setDraft("");
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
    if (!taskID || busy || detail?.archived || sending || inFlight.current)
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
    if (!taskID || !detail || updating) return;
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
          onClick={() => selectTask(null)}
          disabled={sending}
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
              </Space>
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
                    {turn.reply && (
                      <LazyMarkdown
                        content={turn.reply}
                        isStreaming={turn.status === "running"}
                      />
                    )}
                    {turn.activity && (
                      <Typography.Text type="secondary">
                        {t("projects.tasks.tool", { name: turn.activity })}
                      </Typography.Text>
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
                      turn.turn_id === latestTurnID &&
                      (turn.status === "failed" ||
                        turn.status === "cancelled") && (
                        <Space direction="vertical" size="small">
                          <Typography.Text type="secondary">
                            {t("projects.tasks.retryHint")}
                          </Typography.Text>
                          <Button
                            loading={sending}
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
        <div className={styles.composer}>
          <TaskExecutionSettings
            value={options}
            onChange={setOptions}
            project={project}
            disabled={sending || busy || !!detail?.archived}
          />
          <Input.TextArea
            aria-label={t("projects.tasks.message")}
            value={draft}
            maxLength={32768}
            autoSize={{ minRows: 3, maxRows: 8 }}
            disabled={
              sending || loading || !!detail?.archived || !!(taskID && !detail)
            }
            placeholder={t(
              detail
                ? "projects.tasks.continuePlaceholder"
                : "projects.tasks.placeholder",
            )}
            onChange={(event) => setDraft(event.target.value)}
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
          <div className={styles.composerFooter}>
            <Typography.Text type="secondary">
              {t(
                projectID
                  ? "projects.tasks.inherit"
                  : "projects.tasks.standaloneHint",
              )}
            </Typography.Text>
            {busy ? (
              <Button
                icon={<Square size={15} />}
                loading={stopping}
                disabled={detail?.turns.some(
                  (turn) =>
                    turn.turn_id === detail.active_turn_id &&
                    turn.cancel_requested,
                )}
                onClick={() => void stop()}
              >
                {t("projects.tasks.stop")}
              </Button>
            ) : (
              <Button
                type="primary"
                icon={<Send size={16} />}
                loading={sending}
                disabled={
                  !draft.trim() ||
                  loading ||
                  !!detail?.archived ||
                  !!(taskID && !detail)
                }
                onClick={() => void send()}
              >
                {t("projects.tasks.send")}
              </Button>
            )}
          </div>
        </div>
      </div>
    </section>
  );
}
