import { useTaskHost } from "../../context/TaskHostContext";
import { useEffect, useRef, useState } from "react";
import { Alert, Button, Space, Switch, Typography } from "antd";
import { useTranslation } from "react-i18next";
import { type TaskDetail, type TaskPatch } from "../../api/modules/projects";
import { apiErrorMessage } from "../../utils/apiError";

export default function TaskWebSearch({
  task,
  draft,
  onDraftChange,
  onChange,
  onBusyChange,
  disabled,
}: {
  task: TaskDetail | null;
  draft: boolean;
  onDraftChange: (enabled: boolean) => void;
  onChange: (task: TaskDetail) => void;
  onBusyChange: (busy: boolean) => void;
  disabled: boolean;
}) {
  const { projectTasksApi } = useTaskHost();
  const { t } = useTranslation();
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState<TaskPatch | null>(null);
  const alive = useRef(true);
  const inFlight = useRef(false);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const write = async (body: TaskPatch | null) => {
    if (!task || inFlight.current) return;
    inFlight.current = true;
    setSaving(true);
    onBusyChange(true);
    setError("");
    try {
      const result = body
        ? await projectTasksApi.update(task.task_id, body)
        : await projectTasksApi.get(task.task_id);
      if (alive.current) {
        onChange(result);
        setRetry(null);
      }
    } catch (err) {
      if (alive.current) {
        if (body) setRetry(body);
        setError(apiErrorMessage(err, t("projects.tasks.failure"), t));
      }
    } finally {
      inFlight.current = false;
      if (alive.current) setSaving(false);
      onBusyChange(false);
    }
  };

  return (
    <Space
      direction="vertical"
      style={{ width: "100%", marginBottom: 10 }}
      size={4}
    >
      <Space>
        <Typography.Text>{t("projects.tasks.webSearch.label")}</Typography.Text>
        <Switch
          aria-label={t("projects.tasks.webSearch.label")}
          checked={task?.web_search_enabled ?? draft}
          loading={saving}
          disabled={disabled || saving || !!retry}
          onChange={(enabled) => {
            if (!task) onDraftChange(enabled);
            else
              void write({
                expected_revision: task.list_revision,
                web_search_enabled: enabled,
              });
          }}
        />
      </Space>
      <Typography.Text type="secondary">
        {t("projects.tasks.webSearch.scope")}
      </Typography.Text>
      {error && (
        <Alert
          type="error"
          message={error}
          action={
            <Space>
              {retry && (
                <Button
                  size="small"
                  loading={saving}
                  disabled={disabled}
                  onClick={() => void write(retry)}
                >
                  {t("projects.tasks.approvalMode.retry")}
                </Button>
              )}
              <Button
                size="small"
                disabled={saving || disabled}
                onClick={() => void write(null)}
              >
                {t("projects.tasks.approvalMode.refresh")}
              </Button>
            </Space>
          }
        />
      )}
    </Space>
  );
}
