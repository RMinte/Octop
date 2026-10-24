import { useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  Button,
  Empty,
  Input,
  Modal,
  Progress,
  Select,
  Space,
  Spin,
  Tabs,
  Tag,
} from "antd";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { useCurrentUser } from "../../hooks/useCurrentUser";
import { useServerTimezone } from "../../hooks/useServerTimezone";
import { formatServerIsoDateTime } from "../../utils/formatMessageTime";
import { apiErrorMessage } from "../../utils/apiError";
import Markdown from "../../components/Markdown/LazyMarkdown";
import type { ResidentScope } from "../../api/modules/resident";
import {
  residentActivityApi as api,
  type FollowupFields,
  type ResidentContextUsage,
  type ResidentDelegation,
  type ResidentFollowup,
  type ResidentNotification,
} from "../../api/modules/residentActivity";

type Tab = "delegations" | "followups" | "notifications" | "context";
type ActivityRow = ResidentDelegation | ResidentFollowup | ResidentNotification;
type FollowupIntent = FollowupFields & { request_id: string };

export default function ResidentActivityPanel({
  scope,
  active,
}: {
  scope: ResidentScope;
  active: boolean;
}) {
  const user = useCurrentUser();
  return (
    <ActivityScope
      key={`${user?.id}:${scope.agent_id}:${scope.thread_id}`}
      scope={scope}
      active={active}
    />
  );
}

function ActivityScope({
  scope: inputScope,
  active,
}: {
  scope: ResidentScope;
  active: boolean;
}) {
  const { t } = useTranslation();
  const user = useCurrentUser();
  const timezone = useServerTimezone();
  const scope = useMemo(
    () => ({ agent_id: inputScope.agent_id, thread_id: inputScope.thread_id }),
    [inputScope.agent_id, inputScope.thread_id],
  );
  const key = `octop:resident-followup:v1:${user?.id}:${scope.agent_id}:${scope.thread_id}`;
  const [tab, setTab] = useState<Tab>("delegations");
  const [rows, setRows] = useState<ActivityRow[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [context, setContext] = useState<ResidentContextUsage | null>(null);
  const [error, setError] = useState("");
  const [result, setResult] = useState("");
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [revision, setRevision] = useState(0);
  const mutation = useRef(false);
  const generation = useRef(0);
  const [kind, setKind] = useState<"note" | "task_watch">("note");
  const [purpose, setPurpose] = useState("");
  const [text, setText] = useState("");
  const [schedule, setSchedule] = useState("");
  const [taskID, setTaskID] = useState("");
  const [pending, setPending] = useState<FollowupIntent | null>(() => {
    try {
      const value = JSON.parse(
        localStorage.getItem(key) ?? "null",
      ) as FollowupIntent | null;
      return value?.request_id && ["note", "task_watch"].includes(value.kind)
        ? value
        : null;
    } catch {
      return null;
    }
  });

  useEffect(() => {
    const controller = new AbortController();
    const current = ++generation.current;
    setRows([]);
    setCursor(null);
    setContext(null);
    setLoading(true);
    setError("");
    const request =
      tab === "context"
        ? api.context(scope, controller.signal)
        : api[tab](scope, undefined, controller.signal);
    void request
      .then((value) => {
        if (controller.signal.aborted || generation.current !== current) return;
        if ("items" in value) {
          setRows(value.items);
          setCursor(value.next_page_cursor);
        } else setContext(value);
      })
      .catch((err) => {
        if (!controller.signal.aborted)
          setError(apiErrorMessage(err, t("resident.failure"), t));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => {
      controller.abort();
      generation.current = current + 1;
    };
  }, [scope, tab, revision, t]);

  const run = async (action: () => Promise<unknown>) => {
    if (mutation.current) return;
    mutation.current = true;
    setBusy(true);
    setError("");
    const current = generation.current;
    try {
      await action();
      if (generation.current === current) setRevision((v) => v + 1);
    } catch (err) {
      if (generation.current === current)
        setError(apiErrorMessage(err, t("resident.failure"), t));
    } finally {
      mutation.current = false;
      setBusy(false);
    }
  };
  const create = () =>
    run(async () => {
      const intent: FollowupIntent = pending ?? {
        request_id: crypto.randomUUID(),
        purpose: purpose.trim(),
        kind,
        schedule: kind === "note" ? schedule.trim() : null,
        subject_task_id: kind === "task_watch" ? taskID.trim() : null,
        payload: { text: kind === "note" ? text.trim() : "" },
        capabilities: ["conversation_notify"],
        notification_target: "conversation",
      };
      // A failed response must leave the exact original authorization available after reload.
      localStorage.setItem(key, JSON.stringify(intent));
      setPending(intent);
      try {
        await api.createFollowup(scope, intent);
      } catch (err) {
        if (
          err instanceof Error &&
          /^Request failed: (400|404|422)\b/.test(err.message)
        ) {
          localStorage.removeItem(key);
          setPending(null);
        }
        throw err;
      }
      localStorage.removeItem(key);
      setPending(null);
      setPurpose("");
      setText("");
      setSchedule("");
      setTaskID("");
    });
  const loadMore = async () => {
    if (!cursor || loading || tab === "context") return;
    const current = generation.current;
    setLoading(true);
    setError("");
    try {
      const value = await api[tab](scope, cursor);
      if (generation.current !== current) return;
      setRows((old) => [...old, ...value.items]);
      setCursor(value.next_page_cursor);
    } catch (err) {
      if (generation.current === current)
        setError(apiErrorMessage(err, t("resident.failure"), t));
    } finally {
      if (generation.current === current) setLoading(false);
    }
  };
  const status = (value: string) =>
    t(`resident.activity.status.${value}`, { defaultValue: value });
  const taskLink = (id: string) => (
    <Link to={`/conversations/${encodeURIComponent(id)}`}>
      {t("resident.activity.openTask")}
    </Link>
  );
  const box: React.CSSProperties = {
    padding: "16px 0",
    borderBottom: "1px solid var(--border-color, #8883)",
    overflowWrap: "anywhere",
  };
  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <Tabs
        activeKey={tab}
        onChange={(next) => {
          if (!busy) setTab(next as Tab);
        }}
        items={(
          ["delegations", "followups", "notifications", "context"] as Tab[]
        ).map((item) => ({
          key: item,
          label: t(`resident.activity.${item}`),
          disabled: busy,
        }))}
      />
      <Button
        onClick={() => setRevision((v) => v + 1)}
        disabled={busy || loading}
      >
        {t("resident.activity.refresh")}
      </Button>
      {error && <Alert type="error" message={error} showIcon />}
      {result && <Alert type="info" message={result} />}
      {tab === "followups" && (
        <Space direction="vertical" style={{ width: "100%" }}>
          {pending && (
            <Alert
              type="warning"
              message={t("resident.activity.createError")}
              description={pending.purpose}
            />
          )}
          <Select
            aria-label={t("resident.activity.kind")}
            value={kind}
            onChange={setKind}
            disabled={busy || !!pending}
            options={[
              { value: "note", label: t("resident.activity.createReminder") },
              {
                value: "task_watch",
                label: t("resident.activity.createWatch"),
              },
            ]}
          />
          <Input
            aria-label={t("resident.activity.purpose")}
            placeholder={t("resident.activity.purpose")}
            value={purpose}
            maxLength={512}
            onChange={(e) => setPurpose(e.target.value)}
            disabled={busy || !!pending}
          />
          {kind === "note" ? (
            <>
              <Input
                aria-label={t("resident.activity.schedule")}
                placeholder={t("resident.activity.scheduleHint")}
                value={schedule}
                maxLength={256}
                onChange={(e) => setSchedule(e.target.value)}
                disabled={busy || !!pending}
              />
              <small>{t("resident.activity.timezoneHint", { timezone })}</small>
              <Input.TextArea
                aria-label={t("resident.activity.reminderText")}
                placeholder={t("resident.activity.reminderText")}
                value={text}
                maxLength={8000}
                onChange={(e) => setText(e.target.value)}
                disabled={busy || !!pending}
              />
            </>
          ) : (
            <Input
              aria-label={t("resident.activity.taskID")}
              placeholder={t("resident.activity.taskID")}
              value={taskID}
              onChange={(e) => setTaskID(e.target.value)}
              disabled={busy || !!pending}
            />
          )}
          <Button
            type="primary"
            loading={busy}
            disabled={
              !pending &&
              (!purpose.trim() ||
                (kind === "note"
                  ? !schedule.trim() || !text.trim()
                  : !taskID.trim()))
            }
            onClick={create}
          >
            {pending
              ? t("resident.activity.retryAuthorization")
              : t(
                  kind === "note"
                    ? "resident.activity.createReminder"
                    : "resident.activity.createWatch",
                )}
          </Button>
        </Space>
      )}
      {loading && <Spin />}
      {tab !== "context" && !loading && !rows.length && (
        <Empty description={t("resident.activity.empty")} />
      )}
      {rows.map((row) =>
        "delegation_id" in row ? (
          <article key={row.delegation_id} style={box}>
            <Tag>{status(row.status)}</Tag>{" "}
            {row.task_exists
              ? taskLink(row.task_id)
              : t("resident.activity.taskDeleted")}
            {row.reports.map((report) => (
              <div key={report.report_id}>
                <Markdown content={report.text} />
                {report.artifacts.map((artifact) => (
                  <Tag key={artifact.artifact_id}>{artifact.name}</Tag>
                ))}
              </div>
            ))}
          </article>
        ) : "followup_id" in row ? (
          <article key={row.followup_id} style={box}>
            <strong>{row.purpose}</strong> <Tag>{status(row.status)}</Tag>
            <p>{row.schedule ?? row.subject_task_id}</p>
            {row.next_fire_at && (
              <small>
                {formatServerIsoDateTime(row.next_fire_at, row.timezone)}
              </small>
            )}
            {row.last_error_code && (
              <Alert
                type="warning"
                message={t(`apiErrors.${row.last_error_code}`, {
                  defaultValue: row.last_error_code,
                })}
              />
            )}
            {row.last_task_id && <p>{taskLink(row.last_task_id)}</p>}
            {(row.status === "enabled" || row.status === "paused") && (
              <Space wrap>
                <Button
                  size="small"
                  disabled={busy}
                  onClick={() =>
                    void run(() =>
                      api.updateFollowup(
                        scope,
                        row,
                        row.status === "enabled" ? "paused" : "enabled",
                      ),
                    )
                  }
                >
                  {t(
                    row.status === "enabled"
                      ? "resident.activity.pause"
                      : "resident.activity.resume",
                  )}
                </Button>
                <Button
                  size="small"
                  danger
                  disabled={busy}
                  onClick={() =>
                    Modal.confirm({
                      title: t("resident.activity.cancelConfirm"),
                      content: row.purpose,
                      onOk: () => run(() => api.cancelFollowup(scope, row)),
                    })
                  }
                >
                  {t("resident.activity.cancelFollowup")}
                </Button>
              </Space>
            )}
          </article>
        ) : (
          <article key={row.notification_id} style={box}>
            <Tag>{status(row.status)}</Tag>
            <small>
              {formatServerIsoDateTime(row.created_at, row.timezone)}
            </small>
            <Markdown content={row.text} />
            {row.delivery_error && (
              <Alert
                type="warning"
                message={t("resident.activity.deliveryError")}
              />
            )}
          </article>
        ),
      )}
      {cursor && (
        <Button loading={loading} onClick={() => void loadMore()}>
          {t("resident.activity.loadMore")}
        </Button>
      )}
      {tab === "context" && context && (
        <>
          <p>
            {t("resident.activity.contextUsage", {
              used: context.used_tokens,
              max: context.max_tokens,
            })}
          </p>
          <Progress
            percent={
              context.max_tokens
                ? Math.min(
                    100,
                    Math.round(
                      (context.used_tokens / context.max_tokens) * 100,
                    ),
                  )
                : 0
            }
          />
          <p>{t("resident.activity.contextHint")}</p>
          <Button
            disabled={active || busy}
            loading={busy}
            onClick={() =>
              Modal.confirm({
                title: t("resident.activity.compactConfirm"),
                onOk: () =>
                  run(async () => {
                    const value = await api.compact(scope);
                    setResult(
                      t(`resident.activity.compactResult.${value.reason}`),
                    );
                  }),
              })
            }
          >
            {t("resident.activity.compact")}
          </Button>
        </>
      )}
    </Space>
  );
}
