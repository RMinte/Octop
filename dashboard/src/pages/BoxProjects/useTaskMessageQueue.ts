import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  projectTasksApi,
  type TaskDetail,
  type TaskInputAttachment,
  type TaskMessage,
} from "../../api/modules/projects";
import { parseApiError } from "../../utils/apiError";

export const TASK_QUEUE_MAX_ITEMS = 10;

export interface QueuedTaskMessage {
  id: string;
  body: TaskMessage;
  attachments: TaskInputAttachment[];
}

interface TaskQueue {
  items: QueuedTaskMessage[];
  projectID: string | null;
  error: Error | null;
  sendingID: string | null;
}

interface QueueMonitor {
  dispose: () => void;
  resume: () => void;
  pause: () => void;
  pump: () => void;
  retry: () => void;
}

const canSend = () => !document.hidden && navigator.onLine;
const asError = (error: unknown) =>
  error instanceof Error ? error : new Error(String(error));

/** In-memory FIFO queues live for the projects page, including background tasks. */
export function useTaskMessageQueue() {
  const { t } = useTranslation();
  const [queues, setQueues] = useState<Record<string, TaskQueue>>({});
  const [delivered, setDelivered] = useState<Record<string, TaskDetail>>({});
  const current = useRef(queues);
  const monitors = useRef(new Map<string, QueueMonitor>());
  const heldTasks = useRef(new Map<string, number>());
  const mounted = useRef(true);
  const translate = useRef(t);
  translate.current = t;

  const writeQueues = useCallback((next: Record<string, TaskQueue>) => {
    current.current = next;
    if (mounted.current) setQueues(next);
  }, []);

  const dropQueue = useCallback(
    (taskID: string) => {
      monitors.current.get(taskID)?.dispose();
      monitors.current.delete(taskID);
      const { [taskID]: _removed, ...rest } = current.current;
      writeQueues(rest);
    },
    [writeQueues],
  );

  const clear = useCallback(
    (taskID: string) => {
      dropQueue(taskID);
      if (mounted.current)
        setDelivered((old) => {
          const { [taskID]: _removed, ...rest } = old;
          return rest;
        });
    },
    [dropQueue],
  );

  const enqueue = useCallback(
    (
      taskID: string,
      projectID: string | null,
      body: TaskMessage,
      attachments: TaskInputAttachment[],
    ): "ok" | "full" => {
      const queue = current.current[taskID];
      if ((queue?.items.length ?? 0) >= TASK_QUEUE_MAX_ITEMS) return "full";
      const item = {
        id: body.request_id,
        body: structuredClone(body),
        attachments: structuredClone(attachments),
      };
      writeQueues({
        ...current.current,
        [taskID]: {
          items: [...(queue?.items ?? []), item],
          projectID: queue ? queue.projectID : projectID,
          error: queue?.error ?? null,
          sendingID: queue?.sendingID ?? null,
        },
      });
      return "ok";
    },
    [writeQueues],
  );

  const reclaim = useCallback(
    (taskID: string, id: string): QueuedTaskMessage | null => {
      const queue = current.current[taskID];
      if (!queue || queue.sendingID === id) return null;
      const item = queue.items.find((entry) => entry.id === id);
      if (!item) return null;
      const items = queue.items.filter((entry) => entry.id !== id);
      if (!items.length) clear(taskID);
      else
        writeQueues({
          ...current.current,
          [taskID]: {
            ...queue,
            items,
            error: queue.items[0]?.id === id ? null : queue.error,
          },
        });
      return item;
    },
    [clear, writeQueues],
  );

  const remove = useCallback(
    (taskID: string, id: string) => {
      reclaim(taskID, id);
    },
    [reclaim],
  );

  const retry = useCallback((taskID: string) => {
    monitors.current.get(taskID)?.retry();
  }, []);

  const hold = useCallback(
    (taskID: string, held: boolean) => {
      const count = (heldTasks.current.get(taskID) ?? 0) + (held ? 1 : -1);
      if (count > 0) heldTasks.current.set(taskID, count);
      else heldTasks.current.delete(taskID);
      writeQueues({ ...current.current });
      if (!heldTasks.current.has(taskID)) monitors.current.get(taskID)?.pump();
    },
    [writeQueues],
  );
  const isHeld = useCallback(
    (taskID: string) => heldTasks.current.has(taskID),
    [],
  );

  useEffect(() => {
    mounted.current = true;
    const resume = () => {
      for (const monitor of monitors.current.values()) {
        if (canSend()) monitor.resume();
        else monitor.pause();
      }
    };
    document.addEventListener("visibilitychange", resume);
    window.addEventListener("online", resume);
    window.addEventListener("offline", resume);
    const activeMonitors = monitors.current;
    return () => {
      mounted.current = false;
      for (const monitor of activeMonitors.values()) monitor.dispose();
      activeMonitors.clear();
      document.removeEventListener("visibilitychange", resume);
      window.removeEventListener("online", resume);
      window.removeEventListener("offline", resume);
    };
  }, []);

  useEffect(() => {
    for (const [taskID, queue] of Object.entries(queues)) {
      const existing = monitors.current.get(taskID);
      if (existing) {
        existing.pump();
        continue;
      }
      const projectID = queue.projectID;
      let disposed = false;
      let stopped = false;
      let connecting = false;
      let checking = 0;
      let snapshot: TaskDetail | null = null;
      let blockedRevision: number | null = null;
      let verifiedHead: string | null = null;
      let verifiedRevision: number | null = null;
      let recheckedRevision: number | null = null;
      let controller: AbortController | null = null;
      let timer: number | undefined;
      let delay = 1000;
      const live = () =>
        !disposed &&
        mounted.current &&
        monitors.current.get(taskID) === monitor &&
        !!current.current[taskID]?.items.length;
      const patchQueue = (patch: Partial<TaskQueue>) => {
        if (!live()) return;
        writeQueues({
          ...current.current,
          [taskID]: { ...current.current[taskID], ...patch },
        });
      };
      const fail = (error: unknown, sent = false) => {
        patchQueue({
          error: asError(error),
          ...(sent ? { sendingID: null } : {}),
        });
      };
      const receipt = (id: string) => {
        if (!live() || !snapshot) return;
        const latest = current.current[taskID];
        if (latest.items[0]?.id !== id) return;
        const accepted = snapshot;
        setDelivered((old) => ({ ...old, [taskID]: accepted }));
        if (latest.items.length === 1) dropQueue(taskID);
        else
          patchQueue({
            items: latest.items.slice(1),
            sendingID: null,
            error: null,
          });
      };
      const apply = (incoming: TaskDetail) => {
        if (!live()) return false;
        if (incoming.task_id !== taskID || incoming.project_id !== projectID) {
          stopped = true;
          fail(
            new Error(translate.current("projects.tasks.wrongProject")),
            true,
          );
          controller?.abort();
          return false;
        }
        // Keep the newest admission state even if an older GET arrives late.
        const summary =
          snapshot && snapshot.list_revision > incoming.list_revision
            ? snapshot
            : incoming;
        const turns = new Map(
          snapshot?.turns.map((turn) => [turn.request_id, turn]),
        );
        for (const turn of incoming.turns) turns.set(turn.request_id, turn);
        snapshot = { ...summary, turns: [...turns.values()] };
        if (snapshot.archived) {
          stopped = true;
          fail(new Error(translate.current("apiErrors.TASK_ARCHIVED")), true);
          controller?.abort();
          return false;
        }
        const latest = current.current[taskID];
        // A failed write remains explicit until retry reads its original ID.
        if (
          !latest.error &&
          snapshot.turns.some((turn) => turn.request_id === latest.items[0]?.id)
        )
          receipt(latest.items[0].id);
        return true;
      };
      const refresh = async (signal?: AbortSignal) => {
        checking += 1;
        let verified = false;
        let stale = false;
        try {
          const incoming = await projectTasksApi.get(taskID);
          if (!live() || signal?.aborted) return;
          if (apply(incoming) && live()) {
            stale = incoming.list_revision < (snapshot?.list_revision ?? 0);
            if (!stale) {
              verifiedHead = current.current[taskID].items[0]?.id ?? null;
              verifiedRevision = incoming.list_revision;
              verified = true;
            }
          }
        } catch (error) {
          if (!live() || signal?.aborted) return;
          const code = parseApiError(error)?.code;
          if (code === "NOT_FOUND" || code === "FORBIDDEN") stopped = true;
          fail(error);
        } finally {
          checking -= 1;
          if (live() && verified) pump();
          else if (
            live() &&
            stale &&
            !checking &&
            !stopped &&
            !signal?.aborted &&
            canSend() &&
            !current.current[taskID].error &&
            !current.current[taskID].sendingID &&
            snapshot?.active_turn_id === null &&
            recheckedRevision !== snapshot.list_revision
          ) {
            // An idle event may arrive while GET is pending. Recheck it once;
            // repeated old snapshots must not become an automatic write loop.
            recheckedRevision = snapshot.list_revision;
            void refresh();
          }
        }
      };
      const pump = () => {
        const latest = current.current[taskID];
        if (
          !live() ||
          stopped ||
          heldTasks.current.has(taskID) ||
          checking ||
          !canSend() ||
          !snapshot ||
          snapshot.active_turn_id !== null ||
          latest.error ||
          latest.sendingID ||
          (blockedRevision !== null &&
            snapshot.list_revision <= blockedRevision)
        )
          return;
        const head = latest.items[0];
        // Recheck the target and admission state before each queued write.
        if (
          verifiedHead !== head.id ||
          verifiedRevision !== snapshot.list_revision
        ) {
          void refresh();
          return;
        }
        verifiedHead = null;
        verifiedRevision = null;
        const sentRevision = snapshot.list_revision;
        patchQueue({ sendingID: head.id });
        void projectTasksApi.send(taskID, head.body).then(
          (incoming) => {
            if (!live() || current.current[taskID].items[0]?.id !== head.id)
              return;
            if (apply(incoming)) {
              receipt(head.id);
              pump();
            }
          },
          (error: unknown) => {
            if (!live() || current.current[taskID].items[0]?.id !== head.id)
              return;
            if (parseApiError(error)?.code === "TASK_BUSY") {
              blockedRevision = sentRevision;
              patchQueue({ sendingID: null });
              void refresh();
            } else fail(error, true);
          },
        );
      };
      const connect = async () => {
        if (!live() || stopped || connecting || !canSend()) return;
        window.clearTimeout(timer);
        connecting = true;
        const connection = new AbortController();
        controller = connection;
        try {
          await refresh(connection.signal);
          if (
            !live() ||
            stopped ||
            connection.signal.aborted ||
            !snapshot?.event_cursor
          )
            return;
          await projectTasksApi.events(
            taskID,
            snapshot.event_cursor,
            (event) => {
              if (!live() || connection.signal.aborted || !snapshot) return;
              if (event.type === "snapshot") apply(event.data);
              else
                apply({
                  ...snapshot,
                  ...event.data.task,
                  turns: [event.data.turn],
                  event_cursor: event.data.cursor,
                });
              delay = 1000;
              pump();
            },
            connection.signal,
          );
        } catch (error) {
          if (!live() || connection.signal.aborted) return;
          if (parseApiError(error)?.code === "PROJECT_SYNC_RESET")
            snapshot = null;
          delay = Math.min(delay * 2, 15000);
        } finally {
          connecting = false;
          if (live() && !stopped && canSend())
            timer = window.setTimeout(() => void connect(), delay);
        }
      };
      const monitor: QueueMonitor = {
        dispose: () => {
          disposed = true;
          controller?.abort();
          window.clearTimeout(timer);
        },
        pause: () => {
          controller?.abort();
          window.clearTimeout(timer);
        },
        resume: () => void connect(),
        pump,
        retry: () => {
          if (!live() || current.current[taskID].sendingID) return;
          stopped = false;
          blockedRevision = null;
          recheckedRevision = null;
          // Begin the read before updating state, so an old idle snapshot cannot send.
          const read = refresh();
          patchQueue({ error: null });
          void read.then(() => {
            if (!connecting) void connect();
          });
        },
      };
      monitors.current.set(taskID, monitor);
      void connect();
    }
  }, [queues, dropQueue, writeQueues]);

  return {
    queues,
    delivered,
    enqueue,
    remove,
    reclaim,
    clear,
    retry,
    hold,
    isHeld,
  };
}

export type TaskMessageQueue = ReturnType<typeof useTaskMessageQueue>;
