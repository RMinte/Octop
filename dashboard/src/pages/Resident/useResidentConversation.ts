import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import i18n from "../../i18n";
import { getWsUrl } from "../../api/config";
import {
  getAuthToken,
  isSessionOnlyAuth,
  setAuthToken,
} from "../../api/request";
import {
  residentApi,
  type ResidentBinding,
  type ResidentCapabilities,
  type ResidentHistoryPage,
  type ResidentHistoryQuery,
  type ResidentInbox,
  type ResidentMessage,
  type ResidentScope,
  type ResidentSnapshot,
  type ResidentTurn,
} from "../../api/modules/resident";

export type ResidentConnectionState =
  | "connecting"
  | "connected"
  | "polling"
  | "disconnected";
interface StoredConversation {
  draft: string;
  readTools: boolean;
  pending: ResidentMessage | null;
  cancellations: Record<string, string>;
}
const emptyStored = (): StoredConversation => ({
  draft: "",
  readTools: false,
  pending: null,
  cancellations: {},
});
export const residentStorageKey = (
  account: string | number,
  scope: ResidentScope,
) =>
  `octop.resident.v1:${JSON.stringify([
    String(account),
    scope.agent_id,
    scope.thread_id,
  ])}`;
const storage = () => (isSessionOnlyAuth() ? sessionStorage : localStorage);
function restore(key: string): StoredConversation {
  const value = storage().getItem(key);
  if (!value) return emptyStored();
  const parsed = JSON.parse(value) as StoredConversation;
  if (
    typeof parsed.draft !== "string" ||
    typeof parsed.readTools !== "boolean" ||
    (parsed.pending !== null &&
      (!parsed.pending ||
        typeof parsed.pending.request_id !== "string" ||
        typeof parsed.pending.prompt !== "string"))
  )
    throw new Error(i18n.t("resident.page.savedIntentInvalid"));
  return { ...parsed, cancellations: parsed.cancellations ?? {} };
}
const isNotFound = (error: unknown) =>
  error instanceof Error && /(?:failed:|^)\s*404\b/.test(error.message);
const definiteRejection = (error: unknown) =>
  error instanceof Error &&
  /failed:\s*(400|403|404|413|422)\b/.test(error.message);
const active = (turn: ResidentTurn) =>
  turn.status === "queued" || turn.status === "running";

/** Fixed private identity. No ordinary expert or Task selection is consulted. */
export function useResidentConversation(accountId: string | number | null) {
  const account = accountId === null ? "" : String(accountId);
  const [identity, setIdentity] = useState<{
    account: string;
    binding: ResidentBinding | null;
    capabilities: ResidentCapabilities | null;
  }>({ account: "", binding: null, capabilities: null });
  const binding = identity.account === account ? identity.binding : null;
  const capabilities =
    identity.account === account ? identity.capabilities : null;
  const scope = useMemo<ResidentScope | null>(
    () =>
      binding?.status === "ready" && binding.agent_id && binding.thread_id
        ? { agent_id: binding.agent_id, thread_id: binding.thread_id }
        : null,
    [binding],
  );
  const key = scope && account ? residentStorageKey(account, scope) : "";
  const currentKey = useRef(key);
  currentKey.current = key;
  const currentAccount = useRef(account);
  currentAccount.current = account;
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const connectionError = useRef<unknown>(null);
  const [connectionState, setConnectionState] =
    useState<ResidentConnectionState>("disconnected");
  const [view, setView] = useState<{
    key: string;
    snapshot: ResidentSnapshot | null;
    timeline: ResidentHistoryPage | null;
    inbox: ResidentInbox | null;
  }>({ key: "", snapshot: null, timeline: null, inbox: null });
  const [stored, setStored] = useState<{
    key: string;
    value: StoredConversation;
  }>({ key: "", value: emptyStored() });
  const storedRef = useRef(stored);
  storedRef.current = stored;
  const [sendingKey, setSendingKey] = useState("");
  const sendingRef = useRef("");
  const [historyState, setHistoryState] = useState<{
    key: string;
    page: ResidentHistoryPage | null;
  }>({ key: "", page: null });
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyError, setHistoryError] = useState<unknown>(null);
  const historySequence = useRef(0);
  const snapshotRef = useRef<ResidentSnapshot | null>(null);
  const repairSequence = useRef(0);

  const persist = useCallback(
    (
      expectedKey: string,
      transform: (value: StoredConversation) => StoredConversation,
    ) => {
      if (!expectedKey || currentKey.current !== expectedKey) return;
      const previous =
        storedRef.current.key === expectedKey
          ? storedRef.current.value
          : restore(expectedKey);
      const next = { key: expectedKey, value: transform(previous) };
      // Persist before accepting a local send; a storage failure must not lose its identity.
      storage().setItem(expectedKey, JSON.stringify(next.value));
      storedRef.current = next;
      setStored(next);
    },
    [],
  );

  const settle = useCallback(
    (expectedKey: string, turn: ResidentTurn) => {
      if (currentKey.current !== expectedKey) return;
      const snapshot = snapshotRef.current;
      if (snapshot) {
        const old = snapshot.turns.find(
          (item) => item.turn_id === turn.turn_id,
        );
        if (!old || old.revision <= turn.revision) {
          const turns = [
            ...snapshot.turns.filter((item) => item.turn_id !== turn.turn_id),
            turn,
          ].sort((a, b) => a.created_at.localeCompare(b.created_at));
          const next = { ...snapshot, turns };
          // Fence later REST/WS reads synchronously, before React schedules its render.
          snapshotRef.current = next;
          setView((previous) =>
            previous.key === expectedKey
              ? { ...previous, snapshot: next }
              : previous,
          );
        }
      }
      persist(expectedKey, (previous) =>
        previous.pending?.request_id === turn.request_id
          ? {
              ...previous,
              pending: null,
              draft:
                previous.draft === previous.pending.prompt
                  ? ""
                  : previous.draft,
            }
          : previous,
      );
    },
    [persist],
  );

  const applySnapshot = useCallback(
    (expectedKey: string, incoming: ResidentSnapshot) => {
      if (currentKey.current !== expectedKey) return;
      const previous = snapshotRef.current;
      if (previous && incoming.revision < previous.revision) return;
      const turns = new Map(
        (previous?.turns ?? []).map((turn) => [turn.turn_id, turn]),
      );
      for (const turn of incoming.turns)
        if ((turns.get(turn.turn_id)?.revision ?? 0) <= turn.revision)
          turns.set(turn.turn_id, turn);
      const snapshot = {
        ...incoming,
        turns: [...turns.values()].sort((a, b) =>
          a.created_at.localeCompare(b.created_at),
        ),
      };
      snapshotRef.current = snapshot;
      setView((value) =>
        value.key === expectedKey ? { ...value, snapshot } : value,
      );
      const pending =
        storedRef.current.key === expectedKey
          ? storedRef.current.value.pending
          : null;
      const accepted =
        pending &&
        snapshot.turns.find((turn) => turn.request_id === pending.request_id);
      if (accepted) settle(expectedKey, accepted);
    },
    [settle],
  );

  const observe = useCallback(
    async (expectedKey: string, target: ResidentScope) => {
      const [snapshot, timeline, inbox] = await Promise.all([
        residentApi.snapshot(target),
        residentApi.history(target, { limit: 50 }),
        residentApi.inbox(target),
      ]);
      if (currentKey.current !== expectedKey) return;
      applySnapshot(expectedKey, snapshot);
      setView((previous) =>
        previous.key === expectedKey
          ? {
              ...previous,
              timeline:
                !previous.timeline ||
                timeline.history_revision >= previous.timeline.history_revision
                  ? timeline
                  : previous.timeline,
              inbox:
                !previous.inbox || inbox.revision >= previous.inbox.revision
                  ? inbox
                  : previous.inbox,
            }
          : previous,
      );
    },
    [applySnapshot],
  );

  const recover = useCallback(
    async (expectedKey: string, target: ResidentScope, allowSend: boolean) => {
      const pending =
        storedRef.current.key === expectedKey
          ? storedRef.current.value.pending
          : null;
      if (
        !pending ||
        currentKey.current !== expectedKey ||
        sendingRef.current === expectedKey
      )
        return;
      sendingRef.current = expectedKey;
      setSendingKey(expectedKey);
      let rejected = false;
      try {
        let turn: ResidentTurn;
        try {
          turn = await residentApi.request(target, pending.request_id);
        } catch (failure) {
          if (!isNotFound(failure) || !allowSend) throw failure;
          if (currentKey.current !== expectedKey) return;
          try {
            turn = await residentApi.send(target, pending);
          } catch (failure) {
            rejected = definiteRejection(failure);
            throw failure;
          }
        }
        if (currentKey.current !== expectedKey) return;
        settle(expectedKey, turn);
        setError(null);
        await observe(expectedKey, target);
      } catch (failure) {
        // A lost POST receipt is ambiguous. One read may reconcile it, never a new send ID.
        if (currentKey.current !== expectedKey) return;
        try {
          settle(
            expectedKey,
            await residentApi.request(target, pending.request_id),
          );
        } catch (lookupFailure) {
          if (currentKey.current === expectedKey) {
            // Only an explicit rejection plus absent receipt permits a new user intent.
            if (rejected && isNotFound(lookupFailure))
              persist(expectedKey, (value) =>
                value.pending?.request_id === pending.request_id
                  ? { ...value, pending: null }
                  : value,
              );
            setError(failure);
          }
        }
      } finally {
        if (sendingRef.current === expectedKey) sendingRef.current = "";
        setSendingKey((previous) => (previous === expectedKey ? "" : previous));
      }
    },
    [observe, settle, persist],
  );

  useEffect(() => {
    let cancelled = false;
    setLoading(Boolean(account));
    setError(null);
    setIdentity({ account, binding: null, capabilities: null });
    if (!account) return;
    const load = async () => {
      try {
        const [nextBinding, nextCapabilities] = await Promise.all([
          residentApi.binding(),
          residentApi.capabilities().catch(() => null),
        ]);
        if (!cancelled)
          setIdentity({
            account,
            binding: nextBinding,
            capabilities: nextCapabilities,
          });
      } catch (failure) {
        if (!cancelled) setError(failure);
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
  }, [account]);

  const agentId = scope?.agent_id;
  const threadId = scope?.thread_id;
  const streamSupported =
    capabilities?.api_version === 1 &&
    capabilities.conversation_stream?.transport === "websocket" &&
    capabilities.conversation_stream.path === "/api/project-tasks/ws" &&
    capabilities.conversation_stream.protocol_version === 3;
  useEffect(() => {
    snapshotRef.current = null;
    historySequence.current += 1;
    setView({ key, snapshot: null, timeline: null, inbox: null });
    setHistoryState({ key, page: null });
    setHistoryError(null);
    setHistoryLoading(false);
    setConnectionState(key ? "connecting" : "disconnected");
    if (!key || !agentId || !threadId) return;
    const target = { agent_id: agentId, thread_id: threadId };
    let disposed = false;
    let socket: WebSocket | null = null;
    let reconnect: ReturnType<typeof setTimeout> | undefined;
    let watchdog: ReturnType<typeof setTimeout> | undefined;
    let pollBusy = false;
    let token = getAuthToken();
    try {
      const next = { key, value: restore(key) };
      storedRef.current = next;
      setStored(next);
    } catch (failure) {
      setError(failure);
    }
    setLoading(true);
    const poll = async () => {
      if (disposed || pollBusy) return;
      pollBusy = true;
      try {
        await observe(key, target);
        if (!disposed) {
          const recovered = connectionError.current;
          connectionError.current = null;
          setError((previous: unknown) =>
            previous === recovered ? null : previous,
          );
        }
      } catch (failure) {
        if (!disposed) {
          connectionError.current = failure;
          setError(failure);
        }
      } finally {
        pollBusy = false;
        if (!disposed) setLoading(false);
      }
    };
    const connect = () => {
      if (disposed) return;
      token = getAuthToken();
      if (!streamSupported || !token || typeof WebSocket === "undefined") {
        setConnectionState("polling");
        return;
      }
      let ws: WebSocket;
      const fail = () => {
        if (disposed || socket !== ws) return;
        socket = null;
        clearTimeout(watchdog);
        ws.close();
        setConnectionState("polling");
        reconnect = setTimeout(connect, 3000);
      };
      try {
        ws = new WebSocket(getWsUrl("/project-tasks/ws"));
      } catch {
        setConnectionState("polling");
        reconnect = setTimeout(connect, 3000);
        return;
      }
      socket = ws;
      watchdog = setTimeout(fail, 15000);
      ws.onopen = () => {
        if (!disposed && socket === ws)
          ws.send(
            JSON.stringify({
              type: "authenticate",
              token,
              locale: i18n.language?.startsWith("zh") ? "zh" : "en",
            }),
          );
      };
      ws.onmessage = (event) => {
        if (disposed || socket !== ws) return;
        if (getAuthToken() !== token) {
          fail();
          return;
        }
        clearTimeout(watchdog);
        watchdog = setTimeout(fail, 30000);
        try {
          const frame = JSON.parse(String(event.data));
          if (frame.type === "ready") {
            if (frame.protocol_version !== 3) {
              fail();
              return;
            }
            ws.send(
              JSON.stringify({
                type: "resident.subscribe",
                id: crypto.randomUUID(),
                ...target,
              }),
            );
            void recover(key, target, true);
          } else if (
            frame.type === "session" &&
            typeof frame.access_token === "string"
          ) {
            setAuthToken(frame.access_token, !isSessionOnlyAuth());
            token = frame.access_token;
          } else if (
            frame.type === "resident.snapshot" &&
            frame.data?.agent_id === target.agent_id &&
            frame.data?.thread_id === target.thread_id
          ) {
            applySnapshot(key, frame.data);
            setConnectionState("connected");
            void poll();
          } else if (frame.type === "error") {
            const failure = new Error(
              `${frame.status ?? 500} - ${JSON.stringify({
                error: frame.error,
              })}`,
            );
            connectionError.current = failure;
            setError(failure);
            fail();
          }
        } catch {
          fail();
        }
      };
      ws.onerror = fail;
      ws.onclose = fail;
    };
    void poll().then(() => {
      if (!disposed) void recover(key, target, true);
    });
    connect();
    // REST also covers task reports/notifications that do not advance conversation revision.
    const interval = setInterval(() => {
      void poll();
    }, 5000);
    return () => {
      disposed = true;
      clearInterval(interval);
      clearTimeout(reconnect);
      clearTimeout(watchdog);
      if (socket) {
        socket.onclose = null;
        socket.onerror = null;
        socket.close();
      }
    };
  }, [
    key,
    agentId,
    threadId,
    streamSupported,
    observe,
    recover,
    applySnapshot,
  ]);

  // Initializing is server-managed; reads can observe completion without repeatedly repairing.
  useEffect(() => {
    if (!account || binding?.status !== "initializing") return;
    const timer = setInterval(() => {
      void residentApi
        .binding()
        .then((value) => {
          if (currentAccount.current === account)
            setIdentity((previous) => ({ ...previous, binding: value }));
        })
        .catch((failure) => {
          if (currentAccount.current === account) setError(failure);
        });
    }, 3000);
    return () => clearInterval(timer);
  }, [account, binding?.status]);

  const state = stored.key === key ? stored.value : emptyStored();
  const data =
    view.key === key ? view : { snapshot: null, timeline: null, inbox: null };
  const setDraft = useCallback(
    (draft: string) => {
      try {
        persist(key, (value) => ({ ...value, draft }));
      } catch (failure) {
        setError(failure);
      }
    },
    [key, persist],
  );
  const setReadTools = useCallback(
    (readTools: boolean) => {
      try {
        persist(key, (value) => ({ ...value, readTools }));
      } catch (failure) {
        setError(failure);
      }
    },
    [key, persist],
  );
  const refresh = useCallback(async () => {
    if (!scope || !key || currentKey.current !== key) return;
    try {
      await observe(key, scope);
      if (currentKey.current === key) setError(null);
    } catch (failure) {
      if (currentKey.current === key) setError(failure);
    }
  }, [key, scope, observe]);
  const retryPending = useCallback(async () => {
    if (scope && key) await recover(key, scope, true);
  }, [key, scope, recover]);
  const send = useCallback(async () => {
    if (
      !scope ||
      !key ||
      currentKey.current !== key ||
      sendingRef.current === key
    )
      return;
    try {
      const value =
        storedRef.current.key === key ? storedRef.current.value : restore(key);
      if (!value.pending) {
        if (
          !value.draft.trim() ||
          value.draft.length > 32768 ||
          snapshotRef.current?.turns.some(active)
        )
          return;
        persist(key, (previous) => ({
          ...previous,
          pending: {
            request_id: crypto.randomUUID(),
            prompt: previous.draft,
            read_tools: previous.readTools,
          },
        }));
      }
      await recover(key, scope, true);
    } catch (failure) {
      if (currentKey.current === key) setError(failure);
    }
  }, [key, scope, persist, recover]);
  const cancel = useCallback(
    async (turn: ResidentTurn) => {
      if (!scope || !key || currentKey.current !== key || !active(turn)) return;
      try {
        let requestId = "";
        persist(key, (value) => {
          requestId = value.cancellations[turn.turn_id] ?? crypto.randomUUID();
          return {
            ...value,
            cancellations: {
              ...value.cancellations,
              [turn.turn_id]: requestId,
            },
          };
        });
        settle(
          key,
          await residentApi.cancel(scope, {
            request_id: requestId,
            turn_id: turn.turn_id,
          }),
        );
        if (currentKey.current === key) await observe(key, scope);
      } catch (failure) {
        if (currentKey.current === key) setError(failure);
      }
    },
    [key, scope, persist, settle, observe],
  );
  const repair = useCallback(async () => {
    if (!account || currentAccount.current !== account) return;
    const sequence = ++repairSequence.current;
    setLoading(true);
    try {
      const next = await residentApi.repair();
      if (
        currentAccount.current === account &&
        sequence === repairSequence.current
      ) {
        setIdentity((previous) => ({ ...previous, account, binding: next }));
        setError(null);
      }
    } catch (failure) {
      if (currentAccount.current === account) setError(failure);
    } finally {
      if (currentAccount.current === account) setLoading(false);
    }
  }, [account]);
  const queryHistory = useCallback(
    async (operation: () => Promise<ResidentHistoryPage>) => {
      if (!key || currentKey.current !== key) return null;
      const sequence = ++historySequence.current;
      setHistoryLoading(true);
      setHistoryError(null);
      try {
        const page = await operation();
        if (currentKey.current === key && sequence === historySequence.current)
          setHistoryState({ key, page });
        return page;
      } catch (failure) {
        if (currentKey.current === key && sequence === historySequence.current)
          setHistoryError(failure);
        throw failure;
      } finally {
        if (currentKey.current === key && sequence === historySequence.current)
          setHistoryLoading(false);
      }
    },
    [key],
  );
  const loadHistory = useCallback(
    (values: ResidentHistoryQuery = {}) =>
      scope
        ? queryHistory(() => residentApi.history(scope, values))
        : Promise.resolve(null),
    [scope, queryHistory],
  );
  const searchHistory = useCallback(
    (q: string, cursor?: string) =>
      scope
        ? queryHistory(() => residentApi.searchHistory(scope, q, cursor))
        : Promise.resolve(null),
    [scope, queryHistory],
  );
  const historyDays = useCallback(
    (cursor?: string) =>
      scope && key && currentKey.current === key
        ? residentApi.historyDays(scope, { cursor })
        : Promise.resolve(null),
    [scope, key],
  );
  const locateHistory = useCallback(
    (id: string) =>
      scope && key && currentKey.current === key
        ? residentApi.locateHistory(scope, id)
        : Promise.resolve(null),
    [scope, key],
  );
  const markRead = useCallback(
    async (messageId: string) => {
      if (!scope || !key || currentKey.current !== key) return;
      const next = await residentApi.markRead(scope, messageId);
      if (currentKey.current === key)
        setView((previous) =>
          previous.key === key &&
          (!previous.inbox || next.revision >= previous.inbox.revision)
            ? { ...previous, inbox: next }
            : previous,
        );
    },
    [key, scope],
  );

  return {
    binding,
    capabilities,
    scope,
    turns: data.snapshot?.turns ?? [],
    snapshot: data.snapshot,
    timeline: data.timeline,
    inbox: data.inbox,
    loading,
    error,
    connectionState,
    draft: state.draft,
    setDraft,
    readTools: state.readTools,
    setReadTools,
    send,
    pendingIntent: state.pending,
    retryPending,
    cancel,
    refresh,
    repair,
    sending: !!key && sendingKey === key,
    activeTurn: data.snapshot?.turns.find(active) ?? null,
    history: historyState.key === key ? historyState.page : null,
    historyLoading,
    historyError,
    loadHistory,
    searchHistory,
    historyDays,
    locateHistory,
    markRead,
  };
}
