import { useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  Button,
  Checkbox,
  Drawer,
  Empty,
  Input,
  Modal,
  Spin,
  Tag,
  Tooltip,
} from "antd";
import type { TextAreaRef } from "antd/es/input/TextArea";
import {
  ArrowDown,
  ArrowUpRight,
  Bell,
  BookOpen,
  CheckCheck,
  ChevronLeft,
  History,
  LockKeyhole,
  MessageCircle,
  RefreshCw,
  RotateCcw,
  Send,
  Sparkles,
  Square,
} from "lucide-react";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import type {
  ResidentHistoryItem,
  ResidentHistoryLocation,
  ResidentScope,
  ResidentTurn,
} from "../../api/modules/resident";
import Markdown from "../../components/Markdown/LazyMarkdown";
import { useCurrentUser } from "../../hooks/useCurrentUser";
import { useServerTimezone } from "../../hooks/useServerTimezone";
import { apiErrorMessage } from "../../utils/apiError";
import { formatServerIsoDateTime } from "../../utils/formatMessageTime";
import ResidentActivityPanel from "./ResidentActivityPanel";
import ResidentMemoryPanel from "./ResidentMemoryPanel";
import { useResidentConversation } from "./useResidentConversation";
import styles from "./index.module.less";

const prefix = "resident.page";

function mergeItems(items: ResidentHistoryItem[], turns: ResidentTurn[]) {
  const byTurn = new Map(turns.map((turn) => [turn.turn_id, turn]));
  const merged = items.map((item) => {
    if (!item.turn) return item;
    const latest = byTurn.get(item.turn.turn_id);
    byTurn.delete(item.turn.turn_id);
    return latest && latest.revision >= item.turn.revision
      ? { ...item, turn: latest, updated_at: latest.updated_at }
      : item;
  });
  for (const turn of byTurn.values()) {
    merged.push({
      item_id: `live:${turn.turn_id}`,
      kind: "turn",
      day_key: "",
      timezone: "",
      created_at: turn.created_at,
      updated_at: turn.updated_at,
      turn,
      report: null,
      notification: null,
    });
  }
  return merged.sort(
    (a, b) =>
      Date.parse(a.created_at) - Date.parse(b.created_at) ||
      a.item_id.localeCompare(b.item_id),
  );
}

function itemText(item: ResidentHistoryItem) {
  return (
    item.turn?.prompt ||
    item.turn?.reply ||
    item.report?.text ||
    item.notification?.text ||
    ""
  );
}

export default function ResidentPage() {
  const user = useCurrentUser();
  return <ResidentBody key={user?.id ?? "loading"} />;
}

function ResidentBody() {
  const { t } = useTranslation();
  const user = useCurrentUser();
  const timeZone = useServerTimezone();
  const conversation = useResidentConversation(user?.id ?? null);
  const { loadHistory, markRead } = conversation;
  const [historyOpen, setHistoryOpen] = useState(false);
  const [memoryOpen, setMemoryOpen] = useState(false);
  const [activityOpen, setActivityOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [appliedQuery, setAppliedQuery] = useState("");
  const [historyItems, setHistoryItems] = useState<ResidentHistoryItem[]>([]);
  const [historyCursor, setHistoryCursor] = useState<string | null>(null);
  const [historyError, setHistoryError] = useState<unknown>(null);
  const [focused, setFocused] = useState<ResidentHistoryLocation | null>(null);
  const [locating, setLocating] = useState<string | null>(null);
  const [actionError, setActionError] = useState<unknown>(null);
  const [cancelling, setCancelling] = useState(false);
  const [following, setFollowing] = useState(true);
  const searchSequence = useRef(0);
  const locationSequence = useRef(0);
  const input = useRef<TextAreaRef>(null);
  const end = useRef<HTMLDivElement>(null);
  const messages = useRef<HTMLDivElement>(null);
  const seenMessages = useRef(new Set<string>());
  const [pageVisible, setPageVisible] = useState(!document.hidden);
  const anchorNodes = useRef(new Map<string, HTMLElement>());
  const binding = conversation.binding;
  const scope = useMemo<ResidentScope | null>(
    () =>
      binding?.status === "ready" && binding.agent_id && binding.thread_id
        ? { agent_id: binding.agent_id, thread_id: binding.thread_id }
        : null,
    [binding?.status, binding?.agent_id, binding?.thread_id],
  );
  const scopeKey = `${user?.id ?? ""}:${scope?.agent_id ?? ""}:${
    scope?.thread_id ?? ""
  }`;
  const currentScope = useRef(scopeKey);
  currentScope.current = scopeKey;
  const observationAllowed = useRef(false);
  observationAllowed.current =
    pageVisible && !historyOpen && !memoryOpen && !activityOpen;
  const liveItems = useMemo(
    () => mergeItems(conversation.timeline?.items ?? [], conversation.turns),
    [conversation.timeline, conversation.turns],
  );
  const displayedItems = focused?.items ?? liveItems;

  useEffect(() => {
    searchSequence.current += 1;
    const sequence = searchSequence.current;
    locationSequence.current += 1;
    currentScope.current = scopeKey;
    setHistoryItems([]);
    setHistoryCursor(null);
    setHistoryError(null);
    setFocused(null);
    setLocating(null);
    setQuery("");
    setAppliedQuery("");
    setHistoryOpen(false);
    setMemoryOpen(false);
    setActivityOpen(false);
    setActionError(null);
    setCancelling(false);
    setFollowing(true);
    if (!scope) return;
    const key = scopeKey;
    void loadHistory({ limit: 40 })
      .then((page) => {
        if (currentScope.current !== key || sequence !== searchSequence.current)
          return;
        if (!page) return;
        setHistoryItems(page.items);
        setHistoryCursor(page.next_cursor);
      })
      .catch((error: unknown) => {
        if (currentScope.current === key && sequence === searchSequence.current)
          setHistoryError(error);
      });
    return () => {
      if (currentScope.current === key) currentScope.current = "";
    };
  }, [scopeKey, scope, loadHistory]);

  useEffect(() => {
    if (!focused && following) end.current?.scrollIntoView?.({ block: "end" });
  }, [liveItems, focused, following]);

  useEffect(() => {
    const update = () => setPageVisible(!document.hidden);
    document.addEventListener("visibilitychange", update);
    return () => document.removeEventListener("visibilitychange", update);
  }, []);

  useEffect(() => {
    if (
      !messages.current ||
      !pageVisible ||
      historyOpen ||
      memoryOpen ||
      activityOpen
    )
      return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (
          document.hidden ||
          !observationAllowed.current ||
          currentScope.current !== scopeKey
        )
          return;
        for (const entry of entries) {
          const id = (entry.target as HTMLElement).dataset.residentReadId;
          if (
            !id ||
            !entry.isIntersecting ||
            entry.intersectionRatio <= 0 ||
            seenMessages.current.has(id)
          )
            continue;
          seenMessages.current.add(id);
          void markRead(id).catch(() => seenMessages.current.delete(id));
        }
      },
      { root: messages.current, threshold: 0 },
    );
    messages.current
      .querySelectorAll("[data-resident-read-id]")
      .forEach((node) => observer.observe(node));
    return () => observer.disconnect();
  }, [
    displayedItems,
    markRead,
    pageVisible,
    historyOpen,
    memoryOpen,
    activityOpen,
    scopeKey,
  ]);

  useEffect(() => {
    if (focused)
      anchorNodes.current
        .get(focused.anchor_item_id)
        ?.scrollIntoView?.({ block: "center" });
  }, [focused]);

  const search = async (text: string, cursor?: string) => {
    if (!scope) return;
    const key = scopeKey;
    const sequence = ++searchSequence.current;
    const normalized = text.trim();
    setAppliedQuery(normalized);
    setHistoryError(null);
    try {
      const page = normalized
        ? await conversation.searchHistory(normalized, cursor)
        : await conversation.loadHistory({ cursor, limit: 40 });
      if (currentScope.current !== key || sequence !== searchSequence.current)
        return;
      if (!page) return;
      setHistoryItems((previous) => {
        const items = cursor ? previous.concat(page.items) : page.items;
        return Array.from(
          new Map(items.map((item) => [item.item_id, item])).values(),
        ).sort((a, b) => Date.parse(a.created_at) - Date.parse(b.created_at));
      });
      setHistoryCursor(page.next_cursor);
    } catch (error) {
      if (currentScope.current === key && sequence === searchSequence.current)
        setHistoryError(error);
    }
  };
  const locate = async (itemID: string) => {
    const key = scopeKey;
    const sequence = ++locationSequence.current;
    setLocating(itemID);
    setHistoryError(null);
    try {
      const location = await conversation.locateHistory(itemID);
      if (currentScope.current !== key || sequence !== locationSequence.current)
        return;
      if (!location) return;
      setFocused(location);
      setHistoryOpen(false);
    } catch (error) {
      if (currentScope.current === key && sequence === locationSequence.current)
        setHistoryError(error);
    } finally {
      if (currentScope.current === key && sequence === locationSequence.current)
        setLocating(null);
    }
  };
  const action = async (operation: () => Promise<unknown>) => {
    const key = scopeKey;
    setActionError(null);
    try {
      await operation();
    } catch (error) {
      if (currentScope.current === key) setActionError(error);
    }
  };
  const cancel = async () => {
    const turn = conversation.activeTurn;
    if (!turn) return;
    const key = scopeKey;
    setCancelling(true);
    await action(() => conversation.cancel(turn));
    if (currentScope.current === key) setCancelling(false);
  };
  const returnLatest = () => {
    setFocused(null);
    setFollowing(true);
  };
  const reusePrompt = (turn: ResidentTurn) => {
    const apply = () => {
      conversation.setDraft(turn.prompt);
      input.current?.focus();
    };
    if (conversation.draft.trim())
      Modal.confirm({ title: t(`${prefix}.replaceDraft`), onOk: apply });
    else apply();
  };
  const canSend =
    !!scope &&
    !!conversation.draft.trim() &&
    !conversation.sending &&
    !conversation.pendingIntent &&
    !conversation.activeTurn;
  const error = actionError ?? conversation.error;
  const kindLabel = (item: ResidentHistoryItem) =>
    t(
      `${prefix}.${
        item.kind === "task_report"
          ? "taskReport"
          : item.kind === "notification"
          ? "notification"
          : "assistant"
      }`,
    );
  const timestamp = (value: string) => formatServerIsoDateTime(value, timeZone);
  const taskLink = (id: string) => (
    <Link
      to={`/conversations/${encodeURIComponent(id)}`}
      className={styles.taskLink}
    >
      {t(`${prefix}.openTask`)} <ArrowUpRight size={13} />
    </Link>
  );
  const historyPanel = (
    <div className={styles.historyPanel}>
      <div className={styles.historyHeading}>
        <strong>{t(`${prefix}.historyTitle`)}</strong>
        <Button
          type="text"
          size="small"
          icon={<RefreshCw size={14} />}
          aria-label={t(`${prefix}.refresh`)}
          disabled={!scope || conversation.historyLoading}
          onClick={() => void search(appliedQuery)}
        />
      </div>
      <Input.Search
        aria-label={t(`${prefix}.historySearch`)}
        placeholder={t(`${prefix}.historySearch`)}
        value={query}
        onChange={(event) => setQuery(event.target.value)}
        onSearch={(value) => void search(value)}
        allowClear
        disabled={!scope}
      />
      {!!historyError && (
        <Alert
          type="error"
          showIcon
          message={apiErrorMessage(historyError, t("common.unknownError"), t)}
        />
      )}
      <div
        className={styles.historyList}
        role="list"
        aria-label={t(`${prefix}.historyTitle`)}
      >
        {!historyItems.length && !conversation.historyLoading && (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description={t(`${prefix}.historyEmpty`)}
          />
        )}
        {[...historyItems].reverse().map((item) => (
          <button
            type="button"
            role="listitem"
            className={`${styles.historyItem} ${
              focused?.anchor_item_id === item.item_id
                ? styles.selectedHistory
                : ""
            }`}
            key={item.item_id}
            aria-current={
              focused?.anchor_item_id === item.item_id ? "true" : undefined
            }
            disabled={!!locating}
            onClick={() => void locate(item.item_id)}
          >
            <span className={styles.historyItemKind}>
              {kindLabel(item)}
              {locating === item.item_id && <Spin size="small" />}
            </span>
            <span className={styles.historyExcerpt}>
              {itemText(item).replace(/\s+/g, " ").slice(0, 160)}
            </span>
            <time dateTime={item.created_at}>{timestamp(item.created_at)}</time>
          </button>
        ))}
      </div>
      {conversation.historyLoading && <Spin size="small" />}
      {historyCursor && (
        <Button
          size="small"
          block
          loading={conversation.historyLoading}
          onClick={() => void search(appliedQuery, historyCursor)}
        >
          {t(`${prefix}.loadOlder`)}
        </Button>
      )}
    </div>
  );

  const renderItem = (item: ResidentHistoryItem) => {
    const turn = item.turn;
    if (turn) {
      const active = turn.status === "queued" || turn.status === "running";
      return (
        <div className={styles.turn}>
          <article
            className={styles.userMessage}
            aria-label={t(`${prefix}.user`)}
          >
            <div className={styles.messageMeta}>
              <span>{t(`${prefix}.user`)}</span>
              <time dateTime={turn.created_at}>
                {timestamp(turn.created_at)}
              </time>
            </div>
            <div className={styles.userText}>{turn.prompt}</div>
          </article>
          <article
            className={styles.assistantMessage}
            aria-label={t(`${prefix}.assistant`)}
          >
            <div className={styles.messageMeta}>
              <span>
                <Sparkles size={14} />
                {t(`${prefix}.assistant`)}
              </span>
              <Tag bordered={false}>
                {t(`${prefix}.statuses.${turn.status}`)}
              </Tag>
            </div>
            {turn.reply && (
              <Markdown content={turn.reply} isStreaming={active} />
            )}
            {active && !turn.reply && <Spin size="small" />}
            {turn.error_message && (
              <Alert type="error" showIcon message={turn.error_message} />
            )}
            {["failed", "interrupted", "cancelled"].includes(turn.status) && (
              <Button
                type="text"
                size="small"
                icon={<RotateCcw size={13} />}
                onClick={() => reusePrompt(turn)}
              >
                {t(`${prefix}.usePrompt`)}
              </Button>
            )}
            {(!!turn.reply || !active) && (
              <span
                className={styles.readAnchor}
                data-resident-read-id={turn.assistant_message_id}
                aria-hidden="true"
              />
            )}
          </article>
        </div>
      );
    }
    if (item.report)
      return (
        <article
          className={styles.reportMessage}
          aria-label={t(`${prefix}.taskReport`)}
        >
          <div className={styles.messageMeta}>
            <span>
              <CheckCheck size={15} />
              {t(`${prefix}.taskReport`)}
            </span>
            <time dateTime={item.created_at}>{timestamp(item.created_at)}</time>
          </div>
          <Markdown content={item.report.text} />
          {item.report.error_message && (
            <Alert type="warning" message={item.report.error_message} />
          )}
          {taskLink(item.report.task_id)}
          <span
            className={styles.readAnchor}
            data-resident-read-id={item.report.message_id}
            aria-hidden="true"
          />
        </article>
      );
    if (item.notification)
      return (
        <article
          className={styles.notificationMessage}
          aria-label={t(`${prefix}.notification`)}
        >
          <div className={styles.messageMeta}>
            <span>
              <Bell size={15} />
              {t(`${prefix}.notification`)}
            </span>
            <time dateTime={item.created_at}>{timestamp(item.created_at)}</time>
          </div>
          <Markdown content={item.notification.text} />
          {typeof item.notification.payload.task_id === "string" &&
            taskLink(item.notification.payload.task_id)}
          <span
            className={styles.readAnchor}
            data-resident-read-id={item.notification.message_id}
            aria-hidden="true"
          />
        </article>
      );
    return null;
  };

  return (
    <section className={styles.page} aria-label={t(`${prefix}.title`)}>
      <header className={styles.header}>
        <div className={styles.heading}>
          <span className={styles.avatar}>
            <Sparkles size={18} />
          </span>
          <div>
            <h1>{t(`${prefix}.title`)}</h1>
            <span className={styles.identity}>
              <LockKeyhole size={11} />
              {t(`${prefix}.fixedIdentity`)}
            </span>
          </div>
        </div>
        <div className={styles.headerActions}>
          <Tooltip
            title={t(`${prefix}.connection.${conversation.connectionState}`)}
          >
            <span
              className={`${styles.connection} ${
                conversation.connectionState === "connected"
                  ? styles.connected
                  : ""
              }`}
              role="status"
              aria-label={t(
                `${prefix}.connection.${conversation.connectionState}`,
              )}
            />
          </Tooltip>
          <Button
            className={styles.mobileHistory}
            type="text"
            size="small"
            icon={<History size={15} />}
            onClick={() => setHistoryOpen(true)}
          >
            {t(`${prefix}.history`)}
          </Button>
          <Button
            type="text"
            size="small"
            icon={<BookOpen size={15} />}
            disabled={!scope || !conversation.capabilities?.memory_management}
            onClick={() => setMemoryOpen(true)}
          >
            {t(`${prefix}.memory`)}
          </Button>
          <Button
            type="text"
            size="small"
            icon={<Bell size={15} />}
            disabled={!scope}
            onClick={() => setActivityOpen(true)}
          >
            {t(`${prefix}.activity`)}
          </Button>
        </div>
      </header>
      <div className={styles.layout}>
        <aside
          className={styles.historyAside}
          aria-label={t(`${prefix}.historyTitle`)}
        >
          {historyPanel}
        </aside>
        <main className={styles.main}>
          {!!error && (
            <Alert
              className={styles.topAlert}
              type="error"
              showIcon
              message={apiErrorMessage(error, t("common.unknownError"), t)}
              action={
                <Button
                  size="small"
                  onClick={() =>
                    void action(
                      conversation.pendingIntent
                        ? conversation.retryPending
                        : conversation.refresh,
                    )
                  }
                >
                  {t(
                    conversation.pendingIntent
                      ? `${prefix}.retry`
                      : `${prefix}.refresh`,
                  )}
                </Button>
              }
            />
          )}
          {!scope && (
            <div className={styles.preparing}>
              {conversation.loading || binding?.status === "initializing" ? (
                <Spin />
              ) : (
                <LockKeyhole size={24} />
              )}
              <strong>
                {t(
                  conversation.loading || binding?.status === "initializing"
                    ? `${prefix}.preparing`
                    : binding?.status === "failed"
                    ? `${prefix}.failed`
                    : `${prefix}.notReady`,
                )}
              </strong>
              {!conversation.loading && binding?.status !== "initializing" && (
                <Button
                  icon={<RefreshCw size={14} />}
                  loading={conversation.sending}
                  onClick={() => void action(conversation.repair)}
                >
                  {t(`${prefix}.repair`)}
                </Button>
              )}
            </div>
          )}
          {scope && (
            <>
              {focused && (
                <div className={styles.focusBanner}>
                  <span>
                    <History size={14} />
                    {t(`${prefix}.historyFocused`)}
                  </span>
                  <Button
                    type="text"
                    size="small"
                    icon={<ChevronLeft size={13} />}
                    onClick={returnLatest}
                  >
                    {t(`${prefix}.returnLatest`)}
                  </Button>
                </div>
              )}
              <div
                ref={messages}
                className={styles.messages}
                role="log"
                aria-label={t(`${prefix}.message`)}
                aria-live="polite"
                onScroll={(event) => {
                  const element = event.currentTarget;
                  if (!focused)
                    setFollowing(
                      element.scrollHeight -
                        element.scrollTop -
                        element.clientHeight <
                        80,
                    );
                }}
              >
                {!displayedItems.length && (
                  <div className={styles.empty}>
                    <MessageCircle size={30} />
                    <h2>{t(`${prefix}.emptyTitle`)}</h2>
                    <p>{t(`${prefix}.emptyDescription`)}</p>
                  </div>
                )}
                {displayedItems.map((item) => (
                  <div
                    key={item.item_id}
                    ref={(node) => {
                      if (node) anchorNodes.current.set(item.item_id, node);
                      else anchorNodes.current.delete(item.item_id);
                    }}
                    className={
                      focused?.anchor_item_id === item.item_id
                        ? styles.anchor
                        : undefined
                    }
                  >
                    {renderItem(item)}
                  </div>
                ))}
                <div ref={end} />
              </div>
              {!focused && !following && (
                <Button
                  className={styles.latestButton}
                  size="small"
                  icon={<ArrowDown size={13} />}
                  onClick={returnLatest}
                >
                  {t(`${prefix}.returnLatest`)}
                </Button>
              )}
            </>
          )}
          <div className={styles.composer}>
            {conversation.pendingIntent && (
              <Alert
                type="warning"
                showIcon
                message={t(`${prefix}.pendingReceipt`)}
                action={
                  <Button
                    size="small"
                    loading={conversation.sending}
                    onClick={() => void action(conversation.retryPending)}
                  >
                    {t(`${prefix}.retry`)}
                  </Button>
                }
              />
            )}
            <div className={styles.inputBox}>
              <Input.TextArea
                ref={input}
                aria-label={t(`${prefix}.message`)}
                value={conversation.draft}
                onChange={(event) => conversation.setDraft(event.target.value)}
                maxLength={32768}
                autoSize={{ minRows: 2, maxRows: 8 }}
                placeholder={t(`${prefix}.placeholder`)}
                disabled={!user}
                onKeyDown={(event) => {
                  if (
                    event.key === "Enter" &&
                    !event.shiftKey &&
                    !event.nativeEvent.isComposing &&
                    event.keyCode !== 229
                  ) {
                    event.preventDefault();
                    if (canSend) void action(conversation.send);
                  }
                }}
              />
              <div className={styles.inputActions}>
                <span>{t(`${prefix}.textOnly`)}</span>
                {conversation.activeTurn && (
                  <Button
                    size="small"
                    icon={<Square size={13} />}
                    loading={cancelling}
                    onClick={() => void cancel()}
                  >
                    {t(`${prefix}.cancel`)}
                  </Button>
                )}
                <Button
                  size="small"
                  type="primary"
                  icon={<Send size={14} />}
                  loading={conversation.sending}
                  disabled={!canSend}
                  onClick={() => void action(conversation.send)}
                >
                  {t(`${prefix}.send`)}
                </Button>
              </div>
            </div>
            {conversation.capabilities?.turn_read_tools && (
              <Tooltip title={t(`${prefix}.readToolsHint`)}>
                <Checkbox
                  checked={conversation.readTools}
                  disabled={
                    conversation.sending || !!conversation.pendingIntent
                  }
                  onChange={(event) =>
                    conversation.setReadTools(event.target.checked)
                  }
                >
                  {t(`${prefix}.readTools`)}
                </Checkbox>
              </Tooltip>
            )}
          </div>
        </main>
      </div>
      <Drawer
        title={t(`${prefix}.historyTitle`)}
        open={historyOpen}
        onClose={() => setHistoryOpen(false)}
        width="min(350px, 100vw)"
        className={styles.historyDrawer}
      >
        {historyPanel}
      </Drawer>
      {scope && (
        <>
          <ResidentMemoryPanel
            open={memoryOpen}
            onClose={() => setMemoryOpen(false)}
            agentID={scope.agent_id}
            threadID={scope.thread_id}
            conversationActive={!!conversation.activeTurn}
            onLocateHistory={(itemID) => {
              setMemoryOpen(false);
              void locate(itemID);
            }}
          />
          <Drawer
            title={t(`${prefix}.activity`)}
            open={activityOpen}
            onClose={() => setActivityOpen(false)}
            width="min(480px, 100vw)"
            destroyOnHidden
          >
            {activityOpen && (
              <ResidentActivityPanel
                scope={scope}
                active={!!conversation.activeTurn}
              />
            )}
          </Drawer>
        </>
      )}
    </section>
  );
}
