import { randomUUID } from "../../utils/randomUUID";
import { useTaskHost } from "../../context/TaskHostContext";
import { useCallback, useEffect, useRef, useState } from "react";
import { Alert, Button, Modal, Popover, Select, Space, Typography } from "antd";
import { ShieldCheck } from "lucide-react";
import { useTranslation } from "react-i18next";
import {
  type TaskApprovalPolicy,
  type TaskApprovalMode,
  type TaskApprovalUpdate,
} from "../../api/modules/projects";
import { apiErrorMessage, parseApiError } from "../../utils/apiError";

const modes: TaskApprovalMode[] = [
  "ask_for_approval",
  "approve_for_me",
  "full_access",
];

export default function TaskApprovalModePicker({
  onBusyChange,
  disabled,
  compact = false,
}: {
  onBusyChange: (busy: boolean) => void;
  disabled: boolean;
  compact?: boolean;
}) {
  const { projectTasksApi } = useTaskHost();
  const { t } = useTranslation();
  const [policy, setPolicy] = useState<TaskApprovalPolicy | null>(null);
  const confirmed = useRef<TaskApprovalPolicy | null>(null);
  const pending = useRef<TaskApprovalUpdate | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState<TaskApprovalUpdate | null>(null);
  const [confirmFull, setConfirmFull] = useState(false);
  const [reload, setReload] = useState(0);
  const fullConfirmed = useRef(false);
  const alive = useRef(true);
  const inFlight = useRef(false);
  const accept = useCallback((next: TaskApprovalPolicy) => {
    if (confirmed.current && confirmed.current.revision > next.revision) return;
    confirmed.current = next;
    setPolicy(next);
    if (pending.current && next.revision > pending.current.expected_revision) {
      pending.current = null;
      setRetry(null);
    }
  }, []);
  useEffect(() => {
    alive.current = true;
    setLoading(true);
    let disposed = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let controller: AbortController | null = null;
    let connecting = false;
    const read = async () => {
      try {
        const next = await projectTasksApi.approvalPolicy();
        if (!disposed) {
          accept(next);
          if (!pending.current) setError("");
        }
      } catch (err) {
        if (!disposed)
          setError(apiErrorMessage(err, t("projects.tasks.failure"), t));
      } finally {
        if (!disposed) setLoading(false);
      }
    };
    const connect = async () => {
      if (disposed || connecting || document.hidden || !navigator.onLine)
        return;
      connecting = true;
      controller = new AbortController();
      try {
        await projectTasksApi.approvalEvents(
          (next) => {
            if (!disposed) accept(next);
          },
          controller.signal,
          () => {
            void read();
          },
        );
      } catch (err) {
        if (!disposed && !controller.signal.aborted)
          setError(apiErrorMessage(err, t("projects.tasks.failure"), t));
      } finally {
        connecting = false;
        if (!disposed && !document.hidden && navigator.onLine)
          timer = setTimeout(() => void connect(), 1000);
      }
    };
    const resume = () => {
      if (!document.hidden && navigator.onLine) {
        void read();
        void connect();
      }
    };
    void read();
    void connect();
    window.addEventListener("focus", resume);
    window.addEventListener("online", resume);
    document.addEventListener("visibilitychange", resume);
    return () => {
      disposed = true;
      alive.current = false;
      clearTimeout(timer);
      controller?.abort();
      window.removeEventListener("focus", resume);
      window.removeEventListener("online", resume);
      document.removeEventListener("visibilitychange", resume);
    };
  }, [t, reload, accept, projectTasksApi]);
  const save = async (body: TaskApprovalUpdate) => {
    if (inFlight.current) return;
    inFlight.current = true;
    setSaving(true);
    onBusyChange(true);
    setError("");
    pending.current = body;
    try {
      // Retry the original CAS body. A newer revision settles it without
      // applying an old intent against a newly read version.
      if (retry) {
        const current = await projectTasksApi.approvalPolicy();
        if (!alive.current) return;
        accept(current);
        if (current.revision > body.expected_revision) return;
      }
      const result = await projectTasksApi.updateApprovalPolicy(body);
      if (alive.current) {
        accept(result);
        pending.current = null;
        setRetry(null);
      }
    } catch (err) {
      if (alive.current) {
        const status =
          err instanceof Error
            ? Number(err.message.match(/^(\d{3})\b/)?.[1])
            : 0;
        const definitive = status >= 400 && status < 500;
        if (definitive) {
          pending.current = null;
          setRetry(null);
        } else setRetry(body);
        setError(apiErrorMessage(err, t("projects.tasks.failure"), t));
        if (parseApiError(err)?.code === "TASK_APPROVAL_POLICY_CONFLICT") {
          try {
            const next = await projectTasksApi.approvalPolicy();
            if (alive.current) accept(next);
          } catch {
            /* Explicit refresh retries the read. */
          }
        }
      }
    } finally {
      inFlight.current = false;
      if (alive.current) setSaving(false);
      if (alive.current) onBusyChange(false);
    }
  };
  const choose = (mode: TaskApprovalMode) => {
    if (!confirmed.current || inFlight.current || pending.current) return;
    if (mode === confirmed.current.mode) return;
    if (mode === "full_access" && !fullConfirmed.current) {
      setConfirmFull(true);
      return;
    }
    void save({
      request_id: randomUUID(),
      expected_revision: confirmed.current.revision,
      mode,
    });
  };
  const refresh = async () => {
    if (inFlight.current) return;
    setLoading(true);
    try {
      const current = await projectTasksApi.approvalPolicy();
      if (alive.current) {
        accept(current);
        if (!pending.current) setError("");
      }
    } catch (err) {
      if (alive.current)
        setError(apiErrorMessage(err, t("projects.tasks.failure"), t));
    } finally {
      if (alive.current) setLoading(false);
    }
  };
  const mode = policy?.mode ?? "ask_for_approval";
  const body = (
    <Space
      direction="vertical"
      style={{ width: "100%", marginBottom: 10 }}
      size={4}
    >
      <Space wrap>
        <Typography.Text>
          {t("projects.tasks.approvalMode.label")}
        </Typography.Text>
        <Select
          aria-label={t("projects.tasks.approvalMode.label")}
          value={mode}
          loading={loading || saving}
          disabled={disabled || loading || saving || !!retry || !policy}
          style={{ minWidth: 160 }}
          options={modes.map((value) => ({
            value,
            label: t(`projects.tasks.approvalMode.${value}`),
          }))}
          onChange={choose}
        />
      </Space>
      <Typography.Text type="secondary">
        {t("projects.tasks.approvalMode.scope")}
      </Typography.Text>
      <details>
        <summary>{t("projects.tasks.approvalMode.details")}</summary>
        <Typography.Paragraph type="secondary">
          {t("projects.tasks.approvalMode.hostScope")}
        </Typography.Paragraph>
        <Typography.Paragraph type="secondary">
          {t("projects.tasks.approvalMode.clientScope")}
        </Typography.Paragraph>
        <Typography.Paragraph type="secondary">
          {t("projects.tasks.approvalMode.reviewerHint")}
        </Typography.Paragraph>
        <Typography.Paragraph type="secondary">
          {t("projects.tasks.approvalMode.futureOnly")}
        </Typography.Paragraph>
      </details>
      {error && (
        <Alert
          type="error"
          message={error}
          action={
            retry ? (
              <Space>
                <Button
                  size="small"
                  loading={saving}
                  disabled={disabled}
                  onClick={() => void save(retry)}
                >
                  {t("projects.tasks.approvalMode.retry")}
                </Button>
                <Button
                  size="small"
                  disabled={saving || disabled}
                  onClick={() => void refresh()}
                >
                  {t("projects.tasks.approvalMode.refresh")}
                </Button>
              </Space>
            ) : (
              <Button
                size="small"
                disabled={loading}
                onClick={() => setReload((value) => value + 1)}
              >
                {t("projects.refresh")}
              </Button>
            )
          }
        />
      )}
      <Modal
        title={t("projects.tasks.approvalMode.confirmTitle")}
        open={confirmFull}
        okText={t("projects.tasks.approvalMode.confirm")}
        cancelText={t("common.cancel")}
        onCancel={() => setConfirmFull(false)}
        onOk={() => {
          fullConfirmed.current = true;
          setConfirmFull(false);
          choose("full_access");
        }}
      >
        <Typography.Paragraph>
          {t("projects.tasks.approvalMode.hostScope")}
        </Typography.Paragraph>
        <Typography.Paragraph>
          {t("projects.tasks.approvalMode.fullWarning")}
        </Typography.Paragraph>
      </Modal>
    </Space>
  );
  return compact ? (
    <Popover
      trigger="click"
      placement="topRight"
      title={t("projects.composer.accountApproval")}
      content={
        <div
          style={{
            width: "min(340px, calc(100vw - 48px))",
            maxHeight: "60dvh",
            overflow: "auto",
          }}
        >
          {body}
        </div>
      }
    >
      <Button
        type="text"
        size="small"
        icon={<ShieldCheck size={14} />}
        aria-label={t("projects.composer.accountApproval")}
        danger={!!error}
      >
        {t("projects.composer.accountApproval")}
      </Button>
    </Popover>
  ) : (
    body
  );
}
