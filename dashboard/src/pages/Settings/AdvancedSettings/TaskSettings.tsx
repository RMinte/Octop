import { useEffect, useState } from "react";
import { Alert, Button, Space, Spin, Switch } from "antd";
import { ListChecks } from "lucide-react";
import { useTranslation } from "react-i18next";
import {
  projectTasksApi,
  type TaskPresentationSettings,
} from "../../../api/modules/projects";
import { invalidateTaskPresentationSettings } from "../../../hooks/useTaskPresentationSettings";
import { apiErrorMessage } from "../../../utils/apiError";
import { TabPanelHeader } from "./TabPanelHeader";
import styles from "./TaskSettings.module.less";

export default function TaskSettingsPanel() {
  const { t } = useTranslation();
  const [settings, setSettings] = useState<TaskPresentationSettings | null>(
    null,
  );
  const [draft, setDraft] = useState<TaskPresentationSettings | null>(null);
  const [reload, setReload] = useState(0);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    setError("");
    void projectTasksApi.settings().then(
      (value) => {
        if (!active) return;
        setSettings(value);
        setDraft(value);
      },
      (err) => {
        if (active)
          setError(
            apiErrorMessage(err, t("advancedSettings.tasks.loadFailed"), t),
          );
      },
    );
    return () => {
      active = false;
    };
  }, [reload, t]);

  const dirty = Boolean(
    draft &&
      settings &&
      (draft.action_timeline_enabled !== settings.action_timeline_enabled ||
        draft.step_panel_enabled !== settings.step_panel_enabled),
  );

  const change = (key: keyof TaskPresentationSettings, value: boolean) => {
    setDraft((current) => current && { ...current, [key]: value });
    setSaved(false);
    setError("");
  };

  const save = async () => {
    if (!draft || !dirty || saving) return;
    setSaving(true);
    setSaved(false);
    setError("");
    try {
      const value = await projectTasksApi.updateSettings(draft);
      setSettings(value);
      setDraft(value);
      setSaved(true);
      invalidateTaskPresentationSettings();
    } catch (err) {
      setError(apiErrorMessage(err, t("advancedSettings.tasks.saveFailed"), t));
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className={styles.panel}>
      <TabPanelHeader
        icon={<ListChecks size={22} />}
        title={t("advancedSettings.tasks.title")}
        description={t("advancedSettings.tasks.description")}
      />
      {!draft && !error && <Spin aria-label={t("common.loading")} />}
      {draft && (
        <>
          <div className={styles.switches}>
            <div className={styles.row}>
              <div className={styles.copy}>
                <label id="task-actions-label" className={styles.label}>
                  {t("advancedSettings.tasks.actions")}
                </label>
                <p id="task-actions-hint" className={styles.hint}>
                  {t("advancedSettings.tasks.actionsHint")}
                </p>
              </div>
              <Switch
                aria-labelledby="task-actions-label"
                aria-describedby="task-actions-hint"
                checked={draft.action_timeline_enabled}
                disabled={saving}
                onChange={(value) => change("action_timeline_enabled", value)}
              />
            </div>
            <div className={styles.row}>
              <div className={styles.copy}>
                <label id="task-steps-label" className={styles.label}>
                  {t("advancedSettings.tasks.steps")}
                </label>
                <p id="task-steps-hint" className={styles.hint}>
                  {t("advancedSettings.tasks.stepsHint")}
                </p>
              </div>
              <Switch
                aria-labelledby="task-steps-label"
                aria-describedby="task-steps-hint"
                checked={draft.step_panel_enabled}
                disabled={saving}
                onChange={(value) => change("step_panel_enabled", value)}
              />
            </div>
          </div>
          <p className={styles.hint}>
            {t("advancedSettings.tasks.effectHint")}
          </p>
          <Space className={styles.actions}>
            <Button
              type="primary"
              loading={saving}
              disabled={!dirty}
              onClick={() => void save()}
            >
              {t("common.save")}
            </Button>
            <Button
              disabled={!dirty || saving}
              onClick={() => {
                setDraft(settings);
                setError("");
                setSaved(false);
              }}
            >
              {t("common.reset")}
            </Button>
          </Space>
        </>
      )}
      {error && (
        <Alert
          type="error"
          showIcon
          message={error}
          action={
            !draft ? (
              <Button
                size="small"
                onClick={() => setReload((value) => value + 1)}
              >
                {t("common.retry")}
              </Button>
            ) : undefined
          }
        />
      )}
      {saved && (
        <Alert
          type="success"
          showIcon
          message={t("advancedSettings.tasks.saved")}
        />
      )}
    </section>
  );
}
