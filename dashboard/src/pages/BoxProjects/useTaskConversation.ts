import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  projectTasksApi,
  type TaskDetail,
  type TaskTurn,
} from "../../api/modules/projects";
import { apiErrorMessage, parseApiError } from "../../utils/apiError";

const terminal = (turn: TaskTurn) =>
  turn.status === "completed" ||
  turn.status === "failed" ||
  turn.status === "cancelled";

const approvalPolicy = (
  old: TaskDetail,
  incoming: Pick<TaskDetail, "approval_policy">,
) =>
  old.approval_policy.revision > incoming.approval_policy.revision
    ? old.approval_policy
    : incoming.approval_policy;

function mergeTurns(current: TaskTurn[], incoming: TaskTurn[]): TaskTurn[] {
  const rows = new Map(current.map((turn) => [turn.turn_id, turn]));
  for (const turn of incoming) {
    const old = rows.get(turn.turn_id);
    const base =
      old &&
      (terminal(old) ||
        (!terminal(turn) &&
          Date.parse(old.updated_at) > Date.parse(turn.updated_at)))
        ? old
        : turn;
    const interaction =
      old && (old.interaction_revision ?? 0) > (turn.interaction_revision ?? 0)
        ? old
        : turn;
    rows.set(turn.turn_id, {
      ...base,
      interactions: interaction.interactions,
      interaction_revision: interaction.interaction_revision,
      pause_requested: interaction.pause_requested,
      reply:
        old && old.reply.length > turn.reply.length ? old.reply : turn.reply,
      reasoning:
        old && old.reasoning.length > turn.reasoning.length
          ? old.reasoning
          : turn.reasoning,
      content_blocks:
        old && old.content_revision > turn.content_revision
          ? old.content_blocks
          : turn.content_blocks,
      content_revision: Math.max(
        old?.content_revision ?? 0,
        turn.content_revision,
      ),
      cancel_requested: old?.cancel_requested || turn.cancel_requested,
    });
  }
  return [...rows.values()].sort((a, b) => a.sequence - b.sequence);
}

export function useTaskConversation(
  taskID: string | null,
  projectID: string | null,
) {
  const { t } = useTranslation();
  const [detail, setDetail] = useState<TaskDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [reconnecting, setReconnecting] = useState(false);
  const [loadingHistory, setLoadingHistory] = useState(false);
  const [restart, setRestart] = useState(0);
  const selected = useRef({ taskID, projectID });
  const current = useRef(detail);
  current.current = detail;
  selected.current = { taskID, projectID };
  const historyRequest = useRef<object | null>(null);

  const applyWrite = useCallback((incoming: TaskDetail) => {
    setDetail((old) => {
      if (!old || old.task_id !== incoming.task_id) return incoming;
      const summary =
        old.list_revision > incoming.list_revision ? old : incoming;
      return {
        ...summary,
        approval_policy: approvalPolicy(old, incoming),
        turns: mergeTurns(old.turns, incoming.turns),
        next_page_cursor: old.next_page_cursor,
        event_cursor: old.event_cursor,
      };
    });
  }, []);

  const refresh = useCallback(async () => {
    if (!taskID) return;
    try {
      const incoming = await projectTasksApi.get(taskID);
      if (
        selected.current.taskID !== taskID ||
        selected.current.projectID !== projectID
      )
        return;
      if (incoming.project_id !== projectID)
        throw new Error(t("projects.tasks.wrongProject"));
      applyWrite(incoming);
      setError("");
    } catch (err) {
      if (
        selected.current.taskID === taskID &&
        selected.current.projectID === projectID
      )
        setError(apiErrorMessage(err, t("projects.tasks.failure"), t));
    }
  }, [taskID, projectID, applyWrite, t]);

  useEffect(() => {
    let disposed = false;
    let running = false;
    let stopped = false;
    let cursor: string | null = null;
    let opened = false;
    let controller: AbortController | null = null;
    let timer: number | undefined;
    let delay = 1000;
    setDetail(null);
    setLoading(!!taskID);
    setError("");
    setReconnecting(false);
    setLoadingHistory(false);
    historyRequest.current = null;
    const connect = async () => {
      if (
        disposed ||
        stopped ||
        running ||
        !taskID ||
        document.hidden ||
        !navigator.onLine
      )
        return;
      running = true;
      controller = new AbortController();
      try {
        if (!cursor) {
          let snapshot: TaskDetail;
          if (opened) snapshot = await projectTasksApi.get(taskID);
          else {
            // A lost receipt may follow a successful admission. Recover by reading.
            opened = true;
            snapshot = await projectTasksApi.open(taskID);
          }
          if (disposed) return;
          if (snapshot.project_id !== projectID)
            throw new Error(t("projects.tasks.wrongProject"));
          // A reset replaces the loaded range rather than keeping an unknown gap.
          historyRequest.current = null;
          setLoadingHistory(false);
          setDetail((old) =>
            old?.task_id === snapshot.task_id &&
            old.list_revision > snapshot.list_revision
              ? old
              : old?.task_id === snapshot.task_id
              ? { ...snapshot, approval_policy: approvalPolicy(old, snapshot) }
              : snapshot,
          );
          cursor = snapshot.event_cursor;
          setLoading(false);
          setError("");
        }
        if (disposed || controller.signal.aborted || !cursor) return;
        await projectTasksApi.events(
          taskID,
          cursor,
          (event) => {
            if (disposed || controller?.signal.aborted) return;
            if (event.type === "snapshot") {
              setDetail((old) =>
                old?.task_id === event.data.task_id &&
                old.list_revision > event.data.list_revision
                  ? old
                  : old?.task_id === event.data.task_id
                  ? {
                      ...event.data,
                      approval_policy: approvalPolicy(old, event.data),
                    }
                  : event.data,
              );
              cursor = event.data.event_cursor;
            } else {
              const { task, turn } = event.data;
              setDetail((old) =>
                old && old.task_id === task.task_id
                  ? {
                      ...old,
                      ...(task.list_revision >= old.list_revision ? task : {}),
                      approval_policy: approvalPolicy(old, task),
                      turns: mergeTurns(old.turns, [turn]),
                    }
                  : old,
              );
              cursor = event.data.cursor;
            }
            delay = 1000;
            setReconnecting(false);
            setError("");
          },
          controller.signal,
          () => {
            if (!disposed) {
              setReconnecting(false);
              setError("");
            }
          },
        );
        if (controller.signal.aborted) return;
        setReconnecting(false);
        delay = 1000;
      } catch (err) {
        if (disposed || controller.signal.aborted) return;
        const code = parseApiError(err)?.code;
        if (code === "PROJECT_SYNC_RESET") cursor = null;
        stopped =
          code === "NOT_FOUND" ||
          code === "FORBIDDEN" ||
          code === "USER_DISABLED" ||
          (err instanceof Error &&
            (/\b(401|403|404)\b/.test(err.message) ||
              err.message === "Unauthorized" ||
              err.message === t("projects.tasks.wrongProject")));
        setReconnecting(!stopped);
        setError(apiErrorMessage(err, t("projects.tasks.failure"), t));
        setLoading(false);
        delay = Math.min(delay * 2, 15000);
      } finally {
        running = false;
        if (!disposed && !stopped && !document.hidden && navigator.onLine)
          timer = window.setTimeout(() => void connect(), delay);
      }
    };
    const resume = () => {
      window.clearTimeout(timer);
      if (document.hidden || !navigator.onLine) {
        setReconnecting(true);
        controller?.abort();
      } else void connect();
    };
    void connect();
    document.addEventListener("visibilitychange", resume);
    window.addEventListener("online", resume);
    window.addEventListener("offline", resume);
    return () => {
      disposed = true;
      controller?.abort();
      window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", resume);
      window.removeEventListener("online", resume);
      window.removeEventListener("offline", resume);
    };
  }, [taskID, projectID, t, restart]);

  const loadHistory = useCallback(async () => {
    const pageCursor = current.current?.next_page_cursor;
    if (!taskID || !pageCursor || historyRequest.current) return;
    const request = {};
    historyRequest.current = request;
    setLoadingHistory(true);
    try {
      const page = await projectTasksApi.get(taskID, pageCursor);
      if (
        historyRequest.current !== request ||
        selected.current.taskID !== taskID ||
        selected.current.projectID !== projectID
      )
        return;
      setDetail((old) =>
        old && old.task_id === taskID
          ? {
              ...old,
              turns: mergeTurns(page.turns, old.turns),
              next_page_cursor: page.next_page_cursor,
            }
          : old,
      );
      setError("");
    } catch (err) {
      if (historyRequest.current === request) {
        if (parseApiError(err)?.code === "PROJECT_SYNC_RESET")
          setRestart((value) => value + 1);
        else setError(apiErrorMessage(err, t("projects.tasks.failure"), t));
      }
    } finally {
      if (historyRequest.current === request) {
        historyRequest.current = null;
        setLoadingHistory(false);
      }
    }
  }, [taskID, projectID, t]);

  return {
    detail,
    loading,
    error,
    reconnecting,
    loadingHistory,
    refresh,
    applyWrite,
    loadHistory,
  };
}
