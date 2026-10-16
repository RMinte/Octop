import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { getApiUrl } from "../../api/config";
import {
  projectsApi,
  projectTasksApi,
  type Project,
  type TaskSummary,
  type TaskSyncPage,
} from "../../api/modules/projects";
import { useCurrentUser } from "../../hooks/useCurrentUser";
import { parseApiError } from "../../utils/apiError";

type Cache<T> = {
  items: T[];
  cursor: string | null;
  page: string | null;
  generation: number;
  writes: number;
  pending?: Promise<void>;
  paging?: Promise<void>;
};

function load<T>(key: string | null): Cache<T> {
  const empty = {
    items: [] as T[],
    cursor: null,
    page: null,
    generation: 0,
    writes: 0,
  };
  if (!key) return empty;
  try {
    const saved = JSON.parse(sessionStorage.getItem(key) ?? "null");
    if (
      saved &&
      Array.isArray(saved.items) &&
      typeof saved.cursor === "string" &&
      (saved.page === null || typeof saved.page === "string")
    ) {
      return {
        ...empty,
        items: saved.items,
        cursor: saved.cursor,
        page: saved.page,
      };
    }
  } catch {
    /* An unavailable browser cache falls back to the first page. */
  }
  return empty;
}

function save<T>(key: string | null, cache: Cache<T>) {
  if (!key || !cache.cursor) return;
  try {
    sessionStorage.setItem(
      key,
      JSON.stringify({
        items: cache.items,
        cursor: cache.cursor,
        page: cache.page,
      }),
    );
  } catch {
    /* Keep the current tab usable when browser storage is full. */
  }
}

function reset<T>(cache: Cache<T>) {
  cache.items = [];
  cache.cursor = null;
  cache.page = null;
  cache.generation++;
}

function useCache<T>(scope: string) {
  const user = useCurrentUser();
  const key = user
    ? `octop:project-lists:v2:${getApiUrl("/")}:${user.id}:${
        user.username
      }:${scope}`
    : null;
  const identity = key ?? `memory:${scope}`;
  const cache = useMemo(
    () => load<T>(identity.startsWith("memory:") ? null : identity),
    [identity],
  );
  const current = useRef<Cache<T> | null>(cache);
  current.current = cache;
  const [, render] = useState(0);
  useEffect(() => {
    current.current = cache;
    return () => {
      current.current = null;
    };
  }, [cache]);
  const publish = useCallback(() => {
    if (current.current !== cache) return;
    save(key, cache);
    render((value) => value + 1);
  }, [cache, key]);
  return { cache, publish, current };
}

function mergeTasks(
  existing: TaskSummary[],
  incoming: TaskSummary[],
): TaskSummary[] {
  const rows = new Map(existing.map((item) => [item.task_id, item]));
  for (const item of incoming) {
    if ((rows.get(item.task_id)?.list_revision ?? 0) <= item.list_revision)
      rows.set(item.task_id, item);
  }
  return [...rows.values()].sort(
    (a, b) =>
      Date.parse(b.updated_at) - Date.parse(a.updated_at) ||
      b.task_id.localeCompare(a.task_id),
  );
}

export function useProjectList() {
  const { cache, publish, current } = useCache<Project>("projects");
  const refresh = useCallback((): Promise<void> => {
    if (cache.pending) return cache.pending;
    const work = async () => {
      for (;;) {
        const writes = cache.writes;
        let page: string | null = null;
        let next: string | null = null;
        let snapshot = false;
        const incoming: Project[] = [];
        try {
          do {
            const result = await projectsApi.sync(cache.cursor, page);
            snapshot = result.mode === "snapshot";
            incoming.push(...result.changes.map((change) => change.data));
            next = result.next_sync_cursor;
            page = result.next_page_cursor;
          } while (page);
        } catch (error) {
          if (current.current !== cache) return;
          if (parseApiError(error)?.code !== "PROJECT_SYNC_RESET") throw error;
          reset(cache);
          continue;
        }
        if (current.current !== cache) return;
        if (writes !== cache.writes) continue;
        const rows = new Map(
          (snapshot ? [] : cache.items).map((item) => [item.project_id, item]),
        );
        for (const item of incoming) {
          if ((rows.get(item.project_id)?.revision ?? 0) <= item.revision)
            rows.set(item.project_id, item);
        }
        cache.items = [...rows.values()].sort(
          (a, b) =>
            Date.parse(b.updated_at) - Date.parse(a.updated_at) ||
            b.project_id.localeCompare(a.project_id),
        );
        cache.cursor = next;
        publish();
        return;
      }
    };
    cache.pending = work().finally(() => {
      cache.pending = undefined;
    });
    return cache.pending;
  }, [cache, current, publish]);
  const upsert = useCallback(
    (item: Project) => {
      cache.writes++;
      const rows = new Map(cache.items.map((row) => [row.project_id, row]));
      if ((rows.get(item.project_id)?.revision ?? 0) <= item.revision)
        rows.set(item.project_id, item);
      cache.items = [...rows.values()].sort(
        (a, b) =>
          Date.parse(b.updated_at) - Date.parse(a.updated_at) ||
          b.project_id.localeCompare(a.project_id),
      );
      publish();
    },
    [cache, publish],
  );
  return { items: cache.items, refresh, upsert };
}

export function useTaskList(projectID: string | null, archived = false) {
  const { cache, publish, current } = useCache<TaskSummary>(
    `tasks:status-v1:${projectID ?? ""}:${archived}`,
  );
  const listPage = useCallback(
    (page?: string | null) =>
      archived
        ? projectTasksApi.list(projectID, page, true)
        : page
        ? projectTasksApi.list(projectID, page)
        : projectTasksApi.list(projectID),
    [projectID, archived],
  );
  const refresh = useCallback((): Promise<void> => {
    if (cache.pending) return cache.pending;
    const work = async () => {
      for (;;) {
        const generation = cache.generation;
        try {
          if (!cache.cursor) {
            const result = await listPage();
            if (current.current !== cache || generation !== cache.generation)
              return;
            cache.items = mergeTasks(cache.items, result.items);
            cache.page = result.next_page_cursor;
            cache.cursor = result.sync_cursor;
          } else {
            let page: string | null = null;
            let next: string | null = null;
            const incoming: TaskSummary[] = [];
            do {
              const result: TaskSyncPage = await (archived
                ? projectTasksApi.sync(projectID, cache.cursor, page, true)
                : projectTasksApi.sync(projectID, cache.cursor, page));
              incoming.push(...result.changes.map((change) => change.data));
              next = result.next_sync_cursor;
              page = result.next_page_cursor;
            } while (page);
            if (current.current !== cache || generation !== cache.generation)
              return;
            cache.items = mergeTasks(cache.items, incoming);
            cache.cursor = next;
          }
          publish();
          return;
        } catch (error) {
          if (current.current !== cache) return;
          if (parseApiError(error)?.code !== "PROJECT_SYNC_RESET") throw error;
          reset(cache);
        }
      }
    };
    cache.pending = work().finally(() => {
      cache.pending = undefined;
    });
    return cache.pending;
  }, [cache, projectID, current, publish, archived, listPage]);
  const loadMore = useCallback((): Promise<void> => {
    if (cache.paging) return cache.paging;
    if (!cache.page) return Promise.resolve();
    const page = cache.page;
    const generation = cache.generation;
    cache.paging = (async () => {
      try {
        const result = await listPage(page);
        if (current.current !== cache || generation !== cache.generation)
          return;
        cache.items = mergeTasks(cache.items, result.items);
        cache.page = result.next_page_cursor;
        publish();
      } catch (error) {
        if (parseApiError(error)?.code !== "PROJECT_SYNC_RESET") throw error;
        reset(cache);
        publish();
        await refresh();
        if (!cache.cursor) await refresh();
      }
    })().finally(() => {
      cache.paging = undefined;
      publish();
    });
    publish();
    return cache.paging;
  }, [cache, current, publish, refresh, listPage]);
  const upsert = useCallback(
    (item: TaskSummary) => {
      if (item.project_id !== projectID) return;
      const {
        task_id,
        project_id,
        title,
        archived,
        status,
        active_turn_id,
        list_revision,
        created_at,
        updated_at,
      } = item;
      cache.items = mergeTasks(cache.items, [
        {
          task_id,
          project_id,
          title,
          archived,
          status,
          active_turn_id,
          list_revision,
          created_at,
          updated_at,
        },
      ]);
      publish();
    },
    [cache, projectID, publish],
  );
  return {
    items: cache.items.filter((item) => item.archived === archived),
    hasMore: !!cache.page,
    loadingMore: !!cache.paging,
    refresh,
    loadMore,
    upsert,
  };
}
