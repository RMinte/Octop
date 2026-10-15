import { useCallback, useEffect, useRef, useState } from "react";
import {
  Alert,
  Button,
  Empty,
  Input,
  Space,
  Spin,
  Tag,
  Typography,
} from "antd";
import { MessageSquare, Plus, Send, Square } from "lucide-react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";
import {
  projectTasksApi,
  type TaskDetail,
  type TaskMessage,
} from "../../api/modules/projects";
import LazyMarkdown from "../../components/Markdown/LazyMarkdown";
import { useServerTimezone } from "../../hooks/useServerTimezone";
import { apiErrorMessage } from "../../utils/apiError";
import { formatServerIsoDateTime } from "../../utils/formatMessageTime";
import styles from "./index.module.less";
import { useTaskList } from "./useProjectLists";

export default function TaskConversations({
  projectID,
}: {
  projectID: string | null;
}) {
  const { t } = useTranslation();
  const timeZone = useServerTimezone();
  const [params, setParams] = useSearchParams();
  const taskID = params.get("task");
  const {
    items: list,
    refresh: refreshList,
    loadMore,
    hasMore,
    loadingMore,
    upsert: upsertTask,
  } = useTaskList(projectID);
  const [detail, setDetail] = useState<TaskDetail | null>(null);
  const [loading, setLoading] = useState(true);
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
  const [sending, setSending] = useState(false);
  const [stopping, setStopping] = useState(false);
  const generation = useRef({ version: 0 });
  const inFlight = useRef(false);
  const pending = useRef<{ target: string; body: TaskMessage } | null>(null);
  const end = useRef<HTMLDivElement>(null);
  const busy = !!detail?.active_turn_id;

  const refreshing = useRef<{ key: string; promise: Promise<void> } | null>(
    null,
  );
  const refresh = useCallback(() => {
    const key = `${projectID ?? ""}:${taskID ?? ""}`;
    if (refreshing.current?.key === key) return refreshing.current.promise;
    const promise = (async () => {
      const version = ++generation.current.version;
      try {
        const [, selected] = await Promise.all([
          refreshList(),
          taskID ? projectTasksApi.get(taskID) : Promise.resolve(null),
        ]);
        if (version !== generation.current.version) return;
        if (selected && selected.project_id !== projectID)
          throw new Error(t("projects.tasks.wrongProject"));
        setDetail(selected);
        setLoadError("");
      } catch (err) {
        if (version === generation.current.version)
          setLoadError(apiErrorMessage(err, t("projects.tasks.failure"), t));
      } finally {
        if (version === generation.current.version) setLoading(false);
      }
    })().finally(() => {
      if (refreshing.current?.promise === promise) refreshing.current = null;
    });
    refreshing.current = { key, promise };
    return promise;
  }, [projectID, taskID, t, refreshList]);

  useEffect(() => {
    const current = generation.current;
    setDetail(null);
    setError("");
    setDraft("");
    pending.current = null;
    setLoading(true);
    void refresh();
    const timer = window.setInterval(() => {
      if (!document.hidden && !inFlight.current) void refresh();
    }, 1000);
    const visible = () => {
      if (!document.hidden) void refresh();
    };
    document.addEventListener("visibilitychange", visible);
    window.addEventListener("online", visible);
    return () => {
      current.version++;
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", visible);
      window.removeEventListener("online", visible);
    };
  }, [refresh]);

  const latestReply = detail?.turns.at(-1)?.reply;
  useEffect(() => {
    end.current?.scrollIntoView?.({ behavior: "smooth", block: "nearest" });
  }, [latestReply, detail?.turns.length]);

  const selectTask = (id: string | null) => {
    const next = new URLSearchParams(params);
    if (id) next.set("task", id);
    else next.delete("task");
    setParams(next);
  };

  const send = async () => {
    const prompt = draft.trim();
    if (!prompt || busy || sending || inFlight.current || (taskID && !detail))
      return;
    inFlight.current = true;
    setSending(true);
    setError("");
    const target = taskID ?? `new:${projectID ?? ""}`;
    if (
      pending.current?.target !== target ||
      pending.current.body.prompt !== prompt
    ) {
      pending.current = {
        target,
        body: { request_id: crypto.randomUUID(), prompt },
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
      generation.current.version++;
      upsertTask(result);
      setDetail(result);
      setDraft("");
      pending.current = null;
      if (!taskID) selectTask(result.task_id);
      void refreshList().catch((err) => {
        if (alive.current && selectedProjectID.current === projectID)
          setLoadError(apiErrorMessage(err, t("projects.tasks.failure"), t));
      });
    } catch (err) {
      setError(apiErrorMessage(err, t("projects.tasks.failure"), t));
    } finally {
      inFlight.current = false;
      setSending(false);
    }
  };

  const stop = async () => {
    if (!taskID || stopping) return;
    setStopping(true);
    try {
      const result = await projectTasksApi.cancel(taskID);
      if (
        !alive.current ||
        selectedTaskID.current !== taskID ||
        selectedProjectID.current !== projectID
      )
        return;
      generation.current.version++;
      upsertTask(result);
      setDetail(result);
    } catch (err) {
      setError(apiErrorMessage(err, t("projects.tasks.failure"), t));
    } finally {
      setStopping(false);
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
                {task.active_turn_id
                  ? t("projects.tasks.running")
                  : formatServerIsoDateTime(task.updated_at, timeZone)}
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
        {(error || loadError) && (
          <Alert
            type="error"
            showIcon
            message={error || loadError}
            action={
              <Button size="small" onClick={() => void refresh()}>
                {t("projects.refresh")}
              </Button>
            }
          />
        )}
        <div
          className={styles.messages}
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
              <Typography.Title level={5}>{detail.title}</Typography.Title>
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
                      <Tag>{t(`projects.tasks.status.${turn.status}`)}</Tag>
                    </Space>
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
                  </div>
                </div>
              ))}
            </>
          )}
          <div ref={end} />
        </div>
        <div className={styles.composer}>
          <Input.TextArea
            aria-label={t("projects.tasks.message")}
            value={draft}
            maxLength={32768}
            autoSize={{ minRows: 3, maxRows: 8 }}
            disabled={sending || loading || !!(taskID && !detail)}
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
                onClick={() => void stop()}
              >
                {t("projects.tasks.stop")}
              </Button>
            ) : (
              <Button
                type="primary"
                icon={<Send size={16} />}
                loading={sending}
                disabled={!draft.trim() || loading || !!(taskID && !detail)}
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
