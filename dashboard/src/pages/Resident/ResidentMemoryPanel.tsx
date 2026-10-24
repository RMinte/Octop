import { useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  Button,
  Drawer,
  Empty,
  Input,
  Modal,
  Select,
  Space,
  Spin,
  Switch,
  Tabs,
  Tag,
  Typography,
} from "antd";
import { useTranslation } from "react-i18next";
import {
  residentApi,
  type ResidentMemoryCandidate,
  type ResidentMemoryCandidatePage,
  type ResidentMemoryCandidateStatus,
  type ResidentMemoryExtraction,
  type ResidentMemoryItem,
  type ResidentMemoryKind,
  type ResidentMemoryPage,
  type ResidentMemoryPolicy,
  type ResidentMemorySources,
} from "../../api/modules/resident";
import { useServerTimezone } from "../../hooks/useServerTimezone";
import { apiErrorMessage } from "../../utils/apiError";
import { formatServerIsoDateTime } from "../../utils/formatMessageTime";
import styles from "./ResidentMemoryPanel.module.less";

const STATUSES: ResidentMemoryCandidateStatus[] = [
  "pending",
  "needs_review",
  "conflict",
  "promoted",
  "rejected",
  "extractor_failed",
];
const KINDS: ResidentMemoryKind[] = ["Fact", "Decision", "Task", "Preference"];
type Editor = { scope: string; atom?: ResidentMemoryItem };
type Reason = {
  scope: string;
  kind: "deprecate" | "reject";
  id: string;
  assertion: string;
};
type Source = { scope: string; id: string; candidate: boolean };
interface Snapshot {
  scope: string;
  policy: ResidentMemoryPolicy | null;
  memories: ResidentMemoryPage;
  candidates: ResidentMemoryCandidatePage;
}
const EMPTY: Snapshot = {
  scope: "",
  policy: null,
  memories: { items: [], has_more: false },
  candidates: { items: [], has_more: false },
};

export default function ResidentMemoryPanel({
  open,
  onClose,
  agentID,
  threadID,
  archived = false,
  conversationActive = false,
  onLocateHistory,
}: {
  open: boolean;
  onClose: () => void;
  agentID: string;
  threadID: string;
  archived?: boolean;
  conversationActive?: boolean;
  onLocateHistory?: (historyItemID: string) => void;
}) {
  const { t } = useTranslation();
  const timeZone = useServerTimezone();
  const scope = useMemo(
    () => ({ agent_id: agentID, thread_id: threadID }),
    [agentID, threadID],
  );
  const scopeKey = JSON.stringify([agentID, threadID]);
  const currentScope = useRef(scopeKey);
  currentScope.current = scopeKey;
  const currentOpen = useRef(open);
  currentOpen.current = open;
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  const [snapshot, setSnapshot] = useState<Snapshot>(EMPTY);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [actionError, setActionError] = useState("");
  const [version, setVersion] = useState(0);
  const [includeDeprecated, setIncludeDeprecated] = useState(false);
  const [memoryLimit, setMemoryLimit] = useState(50);
  const [candidateLimit, setCandidateLimit] = useState(50);
  const [status, setStatus] = useState<ResidentMemoryCandidateStatus>();
  const [editor, setEditor] = useState<Editor | null>(null);
  const [assertion, setAssertion] = useState("");
  const [kind, setKind] = useState<ResidentMemoryKind>("Fact");
  const [reason, setReason] = useState<Reason | null>(null);
  const [reasonText, setReasonText] = useState("");
  const [source, setSource] = useState<Source | null>(null);
  const [sourceData, setSourceData] = useState<ResidentMemorySources | null>(
    null,
  );
  const [sourceError, setSourceError] = useState("");
  const [sourceLoading, setSourceLoading] = useState(false);
  const [extraction, setExtraction] = useState<{
    scope: string;
    value: ResidentMemoryExtraction;
  } | null>(null);
  const pending = useRef(new Set<string>());
  const [pendingScopes, setPendingScopes] = useState<string[]>([]);
  const busy = pendingScopes.includes(scopeKey);
  const data = snapshot.scope === scopeKey ? snapshot : EMPTY;
  const refresh = () => setVersion((value) => value + 1);
  useEffect(() => {
    setEditor(null);
    setReason(null);
    setSource(null);
    setAssertion("");
    setReasonText("");
    setActionError("");
    setLoadError("");
    setExtraction(null);
    setMemoryLimit(50);
    setCandidateLimit(50);
    setIncludeDeprecated(false);
    setStatus(undefined);
  }, [scopeKey]);
  useEffect(() => {
    if (!open) return;
    let disposed = false;
    setLoading(true);
    setLoadError("");
    void Promise.allSettled([
      residentApi.memoryPolicy(scope),
      residentApi.memory(scope, {
        limit: memoryLimit,
        include_deprecated: includeDeprecated,
      }),
      residentApi.candidates(scope, { limit: candidateLimit, status }),
    ]).then(([policy, memories, candidates]) => {
      if (disposed) return;
      const nextPolicy = policy.status === "fulfilled" ? policy.value : null;
      setSnapshot((previous) => ({
        scope: scopeKey,
        policy:
          previous.scope === scopeKey &&
          previous.policy &&
          (!nextPolicy || previous.policy.revision > nextPolicy.revision)
            ? previous.policy
            : nextPolicy,
        memories:
          memories.status === "fulfilled" ? memories.value : EMPTY.memories,
        candidates:
          candidates.status === "fulfilled"
            ? candidates.value
            : EMPTY.candidates,
      }));
      const failed = [policy, memories, candidates].find(
        (result) => result.status === "rejected",
      );
      if (failed?.status === "rejected")
        setLoadError(apiErrorMessage(failed.reason, t("resident.failure"), t));
      setLoading(false);
    });
    return () => {
      disposed = true;
    };
  }, [
    open,
    scope,
    scopeKey,
    memoryLimit,
    includeDeprecated,
    candidateLimit,
    status,
    version,
    t,
  ]);
  useEffect(() => {
    if (!source || source.scope !== scopeKey) return;
    let disposed = false;
    setSourceData(null);
    setSourceError("");
    setSourceLoading(true);
    const read = source.candidate
      ? residentApi.candidateSources(scope, source.id)
      : residentApi.memorySources(scope, source.id);
    void read
      .then((value) => {
        if (!disposed) setSourceData(value);
      })
      .catch((error) => {
        if (!disposed)
          setSourceError(apiErrorMessage(error, t("resident.failure"), t));
      })
      .finally(() => {
        if (!disposed) setSourceLoading(false);
      });
    return () => {
      disposed = true;
    };
  }, [source, scope, scopeKey, t]);

  async function mutate<T>(
    operation: () => Promise<T>,
    accept?: (value: T) => void,
  ) {
    const key = scopeKey;
    if (!alive.current || !currentOpen.current || currentScope.current !== key)
      return;
    if (pending.current.has(key)) return;
    pending.current.add(key);
    setPendingScopes([...pending.current]);
    setActionError("");
    try {
      const value = await operation();
      if (alive.current && currentScope.current === key) {
        accept?.(value);
        refresh();
      }
    } catch (error) {
      if (alive.current && currentScope.current === key) {
        setActionError(apiErrorMessage(error, t("resident.failure"), t));
        // Read the current revision after a conflict; never replay the user's write automatically.
        refresh();
      }
    } finally {
      pending.current.delete(key);
      if (alive.current) setPendingScopes([...pending.current]);
    }
  }
  const updatePolicy = (enabled: boolean) => {
    const policy = data.policy;
    if (!policy || busy || (archived && enabled)) return;
    const write = () =>
      mutate(
        () =>
          residentApi.updateMemoryPolicy(scope, {
            capture_enabled: enabled,
            expected_revision: policy.revision,
          }),
        (value) =>
          setSnapshot((old) =>
            old.scope === scopeKey ? { ...old, policy: value } : old,
          ),
      );
    if (enabled)
      Modal.confirm({
        title: t("resident.memory.enableCapture"),
        content: t("resident.memory.captureConfirm"),
        okText: t("resident.memory.enableCapture"),
        cancelText: t("common.cancel"),
        onOk: write,
      });
    else void write();
  };
  const edit = (atom?: ResidentMemoryItem) => {
    setAssertion(atom?.assertion ?? "");
    setKind("Fact");
    setEditor({ scope: scopeKey, atom });
  };
  const save = () => {
    if (!editor || editor.scope !== scopeKey || !assertion.trim() || busy)
      return;
    const atom = editor.atom;
    if (!atom && archived) return;
    void mutate(
      () =>
        atom
          ? residentApi.replaceMemory(scope, atom.atom_id, assertion.trim())
          : residentApi.saveMemory(scope, {
              assertion: assertion.trim(),
              kind,
            }),
      () => {
        setEditor(null);
        setAssertion("");
      },
    );
  };
  const decide = (candidate: ResidentMemoryCandidate) =>
    Modal.confirm({
      title: t("resident.memory.approveConfirm"),
      content: candidate.assertion,
      okText: t("resident.memory.approve"),
      cancelText: t("common.cancel"),
      onOk: () =>
        mutate(() =>
          residentApi.approveCandidate(scope, candidate.candidate_id),
        ),
    });
  const extract = () =>
    Modal.confirm({
      title: t("resident.memory.extractConfirm"),
      content: t("resident.memory.extractHint"),
      okText: t("resident.memory.extract"),
      cancelText: t("common.cancel"),
      onOk: () =>
        mutate(
          () => residentApi.extractCandidates(scope),
          (value) => setExtraction({ scope: scopeKey, value }),
        ),
    });
  const submitReason = () => {
    if (!reason || reason.scope !== scopeKey || busy) return;
    void mutate<ResidentMemoryItem | ResidentMemoryCandidate>(
      () =>
        reason.kind === "deprecate"
          ? residentApi.deprecateMemory(scope, reason.id, reasonText.trim())
          : residentApi.rejectCandidate(scope, reason.id, reasonText.trim()),
      () => {
        setReason(null);
        setReasonText("");
      },
    );
  };
  const askReason = (next: Reason) => {
    setReason(next);
    setReasonText("");
  };
  const evidence = (id: string, candidate = false) =>
    setSource({ scope: scopeKey, id, candidate });
  const result = extraction?.scope === scopeKey ? extraction.value : null;
  const showMore = (more: boolean, limit: number, expand: () => void) =>
    more &&
    (limit < 100 ? (
      <Button size="small" onClick={expand}>
        {t("resident.memory.loadMore")}
      </Button>
    ) : (
      <Typography.Text type="secondary">
        {t("resident.memory.recentLimit")}
      </Typography.Text>
    ));

  return (
    <Drawer
      title={t("resident.memory.title")}
      open={open}
      onClose={onClose}
      width={720}
      className={styles.drawer}
    >
      <div className={styles.panel}>
        <Typography.Paragraph type="secondary">
          {t("resident.memory.scopeHint")}
        </Typography.Paragraph>
        {archived && (
          <Alert type="info" message={t("resident.memory.archivedHint")} />
        )}
        {(loadError || actionError) && (
          <Alert
            type="error"
            showIcon
            message={actionError || loadError}
            action={
              <Button onClick={refresh}>{t("resident.memory.refresh")}</Button>
            }
          />
        )}
        <div className={styles.policy}>
          <div>
            <strong>{t("resident.memory.capture")}</strong>
            <p>{t("resident.memory.captureHint")}</p>
          </div>
          <Switch
            aria-label={t("resident.memory.capture")}
            checked={data.policy?.capture_enabled ?? false}
            disabled={
              !data.policy ||
              loading ||
              busy ||
              (archived && !data.policy.capture_enabled)
            }
            loading={busy}
            onChange={updatePolicy}
          />
        </div>
        <Tabs
          items={[
            {
              key: "facts",
              label: t("resident.memory.facts"),
              children: (
                <div className={styles.section}>
                  <div className={styles.toolbar}>
                    <Button
                      type="primary"
                      disabled={busy || archived}
                      onClick={() => edit()}
                    >
                      {t("resident.memory.add")}
                    </Button>
                    <Space>
                      <span>{t("resident.memory.includeDeprecated")}</span>
                      <Switch
                        aria-label={t("resident.memory.includeDeprecated")}
                        size="small"
                        checked={includeDeprecated}
                        onChange={setIncludeDeprecated}
                      />
                    </Space>
                    <Button onClick={refresh}>
                      {t("resident.memory.refresh")}
                    </Button>
                  </div>
                  {loading ? (
                    <Spin />
                  ) : !data.memories.items.length ? (
                    <Empty
                      description={t("resident.memory.empty")}
                      image={Empty.PRESENTED_IMAGE_SIMPLE}
                    />
                  ) : (
                    data.memories.items.map((atom) => {
                      const active = !atom.deprecated_at && !atom.superseded_by;
                      return (
                        <article key={atom.atom_id} className={styles.item}>
                          <Space wrap>
                            <Tag>
                              {t("resident.memory.kinds." + atom.kind, {
                                defaultValue: atom.kind,
                              })}
                            </Tag>
                            {!active && (
                              <Tag>
                                {t(
                                  atom.superseded_by
                                    ? "resident.memory.superseded"
                                    : "resident.memory.deprecated",
                                )}
                              </Tag>
                            )}
                            <Typography.Text type="secondary">
                              {formatServerIsoDateTime(
                                atom.created_at,
                                timeZone,
                              )}
                            </Typography.Text>
                          </Space>
                          <p className={styles.assertion}>{atom.assertion}</p>
                          <Space wrap>
                            <Button
                              size="small"
                              onClick={() => evidence(atom.atom_id)}
                            >
                              {t("resident.memory.sources")}
                            </Button>
                            <Button
                              size="small"
                              disabled={!active || busy}
                              onClick={() => edit(atom)}
                            >
                              {t("resident.memory.replace")}
                            </Button>
                            <Button
                              size="small"
                              danger
                              disabled={!active || busy}
                              onClick={() =>
                                askReason({
                                  scope: scopeKey,
                                  kind: "deprecate",
                                  id: atom.atom_id,
                                  assertion: atom.assertion,
                                })
                              }
                            >
                              {t("resident.memory.deprecate")}
                            </Button>
                          </Space>
                        </article>
                      );
                    })
                  )}
                  {showMore(data.memories.has_more, memoryLimit, () =>
                    setMemoryLimit(100),
                  )}
                </div>
              ),
            },
            {
              key: "candidates",
              label: t("resident.memory.candidates"),
              children: (
                <div className={styles.section}>
                  <Typography.Paragraph type="secondary">
                    {t("resident.memory.candidateHint")}
                  </Typography.Paragraph>
                  <div className={styles.toolbar}>
                    <Button
                      disabled={busy || archived || conversationActive}
                      onClick={extract}
                      title={
                        conversationActive
                          ? t("resident.memory.extractBusy")
                          : undefined
                      }
                    >
                      {t("resident.memory.extract")}
                    </Button>
                    <Select
                      aria-label={t("resident.memory.filterStatus")}
                      allowClear
                      placeholder={t("resident.memory.allStatuses")}
                      value={status}
                      onChange={setStatus}
                      style={{ minWidth: 150 }}
                      options={STATUSES.map((value) => ({
                        value,
                        label: t("resident.memory.status." + value),
                      }))}
                    />
                  </div>
                  {result && (
                    <Alert
                      type={result.status === "failed" ? "error" : "info"}
                      message={t(
                        "resident.memory.extraction." + result.status,
                        {
                          count: result.candidates,
                          events: result.events_extracted,
                        },
                      )}
                      description={t("resident.memory.extractionWindow", {
                        pending: result.pending_events,
                        truncated: result.truncated_events,
                        earlier: result.has_earlier
                          ? t("resident.memory.hasEarlier")
                          : t("resident.memory.noEarlier"),
                      })}
                    />
                  )}
                  {loading ? (
                    <Spin />
                  ) : !data.candidates.items.length ? (
                    <Empty
                      description={t("resident.memory.noCandidates")}
                      image={Empty.PRESENTED_IMAGE_SIMPLE}
                    />
                  ) : (
                    data.candidates.items.map((candidate) => {
                      const reviewable = [
                        "pending",
                        "needs_review",
                        "conflict",
                      ].includes(candidate.status);
                      return (
                        <article
                          className={styles.item}
                          key={candidate.candidate_id}
                        >
                          <Space wrap>
                            <Tag>
                              {t("resident.memory.status." + candidate.status)}
                            </Tag>
                            <Typography.Text type="secondary">
                              {formatServerIsoDateTime(
                                candidate.created_at,
                                timeZone,
                              )}
                            </Typography.Text>
                          </Space>
                          <p className={styles.assertion}>
                            {candidate.assertion}
                          </p>
                          {candidate.quote && (
                            <blockquote className={styles.quote}>
                              {candidate.quote}
                            </blockquote>
                          )}
                          <Space wrap>
                            <Button
                              size="small"
                              onClick={() =>
                                evidence(candidate.candidate_id, true)
                              }
                            >
                              {t("resident.memory.sources")}
                            </Button>
                            {reviewable && (
                              <>
                                <Button
                                  size="small"
                                  disabled={busy || archived}
                                  onClick={() => decide(candidate)}
                                >
                                  {t("resident.memory.approve")}
                                </Button>
                                <Button
                                  size="small"
                                  disabled={busy}
                                  onClick={() =>
                                    askReason({
                                      scope: scopeKey,
                                      kind: "reject",
                                      id: candidate.candidate_id,
                                      assertion: candidate.assertion,
                                    })
                                  }
                                >
                                  {t("resident.memory.reject")}
                                </Button>
                              </>
                            )}
                          </Space>
                        </article>
                      );
                    })
                  )}
                  {showMore(data.candidates.has_more, candidateLimit, () =>
                    setCandidateLimit(100),
                  )}
                </div>
              ),
            },
          ]}
        />
      </div>
      <Modal
        open={open && editor?.scope === scopeKey}
        title={t(
          editor?.atom ? "resident.memory.replace" : "resident.memory.add",
        )}
        onCancel={() => !busy && setEditor(null)}
        okText={t("resident.memory.save")}
        cancelText={t("common.cancel")}
        confirmLoading={busy}
        okButtonProps={{ disabled: !assertion.trim() || busy }}
        onOk={save}
      >
        <Space direction="vertical" style={{ width: "100%" }}>
          {!editor?.atom && (
            <Select
              aria-label={t("resident.memory.kind")}
              value={kind}
              onChange={setKind}
              options={KINDS.map((value) => ({
                value,
                label: t("resident.memory.kinds." + value),
              }))}
            />
          )}
          <Input.TextArea
            aria-label={t("resident.memory.assertion")}
            value={assertion}
            onChange={(event) => setAssertion(event.target.value)}
            rows={4}
            maxLength={2048}
            disabled={busy}
          />
          <Typography.Text type="secondary">
            {t(
              editor?.atom
                ? "resident.memory.replaceHint"
                : "resident.memory.manualHint",
            )}
          </Typography.Text>
        </Space>
      </Modal>
      <Modal
        open={open && reason?.scope === scopeKey}
        title={t(
          reason?.kind === "deprecate"
            ? "resident.memory.deprecate"
            : "resident.memory.reject",
        )}
        onCancel={() => !busy && setReason(null)}
        okText={t("resident.memory.confirm")}
        cancelText={t("common.cancel")}
        confirmLoading={busy}
        onOk={submitReason}
      >
        <p className={styles.assertion}>{reason?.assertion}</p>
        <Input.TextArea
          aria-label={t("resident.memory.reason")}
          placeholder={t("resident.memory.reason")}
          value={reasonText}
          onChange={(event) => setReasonText(event.target.value)}
          maxLength={1024}
          disabled={busy}
        />
        <Typography.Paragraph type="secondary">
          {t("resident.memory.auditHint")}
        </Typography.Paragraph>
      </Modal>
      <Modal
        open={open && source?.scope === scopeKey}
        title={t("resident.memory.sources")}
        footer={
          <Button onClick={() => setSource(null)}>{t("common.close")}</Button>
        }
        onCancel={() => setSource(null)}
        width={640}
      >
        <Typography.Paragraph type="secondary">
          {t("resident.memory.sourcesHint")}
        </Typography.Paragraph>
        {sourceLoading ? (
          <Spin />
        ) : sourceError ? (
          <Alert type="error" message={sourceError} />
        ) : !sourceData?.items.length ? (
          <Empty
            description={t("resident.memory.noSources")}
            image={Empty.PRESENTED_IMAGE_SIMPLE}
          />
        ) : (
          sourceData.items.map((item) => (
            <article key={item.event_id} className={styles.item}>
              <Typography.Text type="secondary">
                {formatServerIsoDateTime(item.timestamp, timeZone)} ·{" "}
                {item.event_type}
              </Typography.Text>
              <p className={styles.assertion}>{item.content}</p>
              {item.source_turn_id && (
                <Typography.Paragraph copyable>
                  {t("resident.memory.sourceTurn")}: {item.source_turn_id}
                </Typography.Paragraph>
              )}
              {item.history_item_id && (
                <>
                  <Typography.Paragraph copyable>
                    {t("resident.memory.historyAnchor")}: {item.history_item_id}
                  </Typography.Paragraph>
                  {onLocateHistory && (
                    <Button
                      size="small"
                      onClick={() => {
                        setSource(null);
                        onClose();
                        onLocateHistory(item.history_item_id!);
                      }}
                    >
                      {t("resident.memory.openHistory")}
                    </Button>
                  )}
                </>
              )}
            </article>
          ))
        )}
      </Modal>
    </Drawer>
  );
}
