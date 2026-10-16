import { useEffect, useRef, useState } from "react";
import { Alert, Button, Checkbox, Input, Space, Spin, Typography } from "antd";
import { useTranslation } from "react-i18next";
import {
  securityApi,
  type TaskApprovalSettings,
} from "../../../api/modules/security";
import type { TaskApprovalMode } from "../../../api/modules/projects";
import { apiErrorMessage } from "../../../utils/apiError";
import styles from "./index.module.less";

export default function TaskApprovalsPanel() {
  const { t } = useTranslation();
  const [settings, setSettings] = useState<TaskApprovalSettings | null>(null);
  const [tools, setTools] = useState("");
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState("");
  const alive = useRef(true);
  const apply = (value: TaskApprovalSettings) => {
    setSettings(value);
    setTools(value.require_human_tools.join("\n"));
  };
  useEffect(() => {
    alive.current = true;
    void securityApi
      .getTaskApprovals()
      .then((value) => {
        if (alive.current) apply(value);
      })
      .catch((err) => {
        if (alive.current)
          setError(apiErrorMessage(err, t("security.loadError"), t));
      });
    return () => {
      alive.current = false;
    };
  }, [t]);
  const save = async () => {
    if (!settings || saving) return;
    setSaving(true);
    setSaved(false);
    setError("");
    try {
      const result = await securityApi.saveTaskApprovals({
        allowed_modes: settings.allowed_modes,
        require_human_tools: [
          ...new Set(
            tools
              .split("\n")
              .map((tool) => tool.trim())
              .filter(Boolean),
          ),
        ],
      });
      if (alive.current) {
        apply(result);
        setSaved(true);
      }
    } catch (err) {
      if (alive.current)
        setError(apiErrorMessage(err, t("security.saveFailed"), t));
    } finally {
      if (alive.current) setSaving(false);
    }
  };
  return (
    <section
      className={styles.rulesSection}
      aria-label={t("security.taskApprovals.title")}
    >
      <h4 className={styles.rulesSectionTitle}>
        {t("security.taskApprovals.title")}
      </h4>
      <Typography.Paragraph type="secondary">
        {t("security.taskApprovals.hint")}
      </Typography.Paragraph>
      {!settings && !error && <Spin />}
      {settings && (
        <Space direction="vertical" style={{ width: "100%" }}>
          <Checkbox.Group
            aria-label={t("security.taskApprovals.allowedModes")}
            value={settings.allowed_modes}
            disabled={saving}
            options={(
              [
                "ask_for_approval",
                "approve_for_me",
                "full_access",
              ] as TaskApprovalMode[]
            ).map((mode) => ({
              value: mode,
              label: t(`projects.tasks.approvalMode.${mode}`),
              disabled: mode === "ask_for_approval",
            }))}
            onChange={(values) => {
              setSettings({
                ...settings,
                allowed_modes: [
                  "ask_for_approval",
                  ...(values.filter(
                    (value) => value !== "ask_for_approval",
                  ) as TaskApprovalMode[]),
                ],
              });
              setSaved(false);
            }}
          />
          <Typography.Text>
            {t("security.taskApprovals.requireHuman")}
          </Typography.Text>
          <Input.TextArea
            aria-label={t("security.taskApprovals.requireHuman")}
            value={tools}
            onChange={(event) => {
              setTools(event.target.value);
              setSaved(false);
            }}
            disabled={saving}
            rows={4}
          />
          <Typography.Text type="secondary">
            {t("security.taskApprovals.toolsHint")}
          </Typography.Text>
          <Typography.Text type="secondary">
            {t("projects.tasks.approvalMode.hostScope")}
          </Typography.Text>
          <Typography.Text type="secondary">
            {t("projects.tasks.approvalMode.reviewerHint")}
          </Typography.Text>
          <Button type="primary" loading={saving} onClick={() => void save()}>
            {t("security.taskApprovals.save")}
          </Button>
        </Space>
      )}
      {error && <Alert type="error" message={error} />}
      {saved && <Alert type="success" message={t("security.saved")} />}
    </section>
  );
}
