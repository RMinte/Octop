import { useEffect, useRef, useState } from "react";
import { Alert, Button, Modal, Select, Space, Typography } from "antd";
import { useTranslation } from "react-i18next";
import {
  projectTasksApi,
  type TaskApprovalCapabilities,
  type TaskApprovalMode,
  type TaskApprovalUpdate,
  type TaskDetail,
} from "../../api/modules/projects";
import { apiErrorMessage } from "../../utils/apiError";

const modes: TaskApprovalMode[] = [
  "ask_for_approval",
  "approve_for_me",
  "full_access",
];

export default function TaskApprovalModePicker({
  task,
  draft,
  onDraftChange,
  onChange,
  onBusyChange,
  disabled,
}: {
  task: TaskDetail | null;
  draft: TaskApprovalMode;
  onDraftChange: (mode: TaskApprovalMode) => void;
  onChange: (task: TaskDetail) => void;
  onBusyChange: (busy: boolean) => void;
  disabled: boolean;
}) {
  const { t } = useTranslation();
  const [capabilities, setCapabilities] =
    useState<TaskApprovalCapabilities | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState<TaskApprovalUpdate | null>(null);
  const [confirmFull, setConfirmFull] = useState(false);
  const [reload, setReload] = useState(0);
  const fullConfirmed = useRef(false);
  const alive = useRef(true);
  const inFlight = useRef(false);
  useEffect(() => {
    alive.current = true;
    let disposed = false;
    setLoading(true);
    void projectTasksApi
      .resources()
      .then((result) => {
        if (!disposed) {
          setCapabilities(result.approval);
          setError("");
        }
      })
      .catch((err) => {
        if (!disposed)
          setError(apiErrorMessage(err, t("projects.tasks.failure"), t));
      })
      .finally(() => {
        if (!disposed) setLoading(false);
      });
    return () => {
      disposed = true;
      alive.current = false;
    };
  }, [t, reload]);
  const save = async (body: TaskApprovalUpdate) => {
    if (!task || inFlight.current) return;
    inFlight.current = true;
    setSaving(true);
    onBusyChange(true);
    setError("");
    try {
      const result = await projectTasksApi.setApproval(task.task_id, body);
      if (alive.current) {
        onChange(result);
        setRetry(null);
      }
    } catch (err) {
      if (alive.current) {
        setRetry(body);
        setError(apiErrorMessage(err, t("projects.tasks.failure"), t));
      }
    } finally {
      inFlight.current = false;
      if (alive.current) setSaving(false);
      if (alive.current) onBusyChange(false);
    }
  };
  const choose = (mode: TaskApprovalMode) => {
    if (mode === "full_access" && !fullConfirmed.current) {
      setConfirmFull(true);
      return;
    }
    if (!task) onDraftChange(mode);
    else if (task.approval_policy.mode !== mode)
      void save({
        request_id: crypto.randomUUID(),
        expected_revision: task.approval_policy.revision,
        mode,
      });
  };
  const refresh = async () => {
    if (!task || inFlight.current) return;
    inFlight.current = true;
    setSaving(true);
    onBusyChange(true);
    try {
      const result = await projectTasksApi.get(task.task_id);
      if (alive.current) {
        onChange(result);
        setRetry(null);
        setError("");
      }
    } catch (err) {
      if (alive.current)
        setError(apiErrorMessage(err, t("projects.tasks.failure"), t));
    } finally {
      inFlight.current = false;
      if (alive.current) setSaving(false);
      if (alive.current) onBusyChange(false);
    }
  };
  const mode = task?.approval_policy.mode ?? draft;
  return (
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
          disabled={disabled || loading || saving || !!retry || !capabilities}
          style={{ minWidth: 160 }}
          options={modes.map((value) => {
            const allowed =
              capabilities?.allowed_modes.includes(value) ?? false;
            const reviewerUnavailable =
              value === "approve_for_me" && !capabilities?.reviewer_ready;
            const reason = !allowed
              ? t("projects.tasks.approvalMode.adminDisabled")
              : reviewerUnavailable
              ? t("projects.tasks.approvalMode.reviewerUnavailable")
              : "";
            return {
              value,
              disabled: !allowed || reviewerUnavailable,
              label: `${t(`projects.tasks.approvalMode.${value}`)}${
                reason ? ` · ${reason}` : ""
              }`,
              title: reason,
            };
          })}
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
}
