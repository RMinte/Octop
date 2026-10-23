import { useTaskHost } from "../../context/TaskHostContext";
import { useEffect, useRef, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Drawer,
  Empty,
  Input,
  Segmented,
  Space,
  Spin,
  Switch,
  Tag,
  Typography,
} from "antd";
import { Brain } from "lucide-react";
import { useTranslation } from "react-i18next";
import {
  type ExpertMemoryList,
  type ExpertMemoryPolicy,
  type ExpertMemorySource,
} from "../../api/modules/taskExpertMemory";
import { apiErrorMessage } from "../../utils/apiError";
import { useServerTimezone } from "../../hooks/useServerTimezone";
import { formatServerIsoDateTime } from "../../utils/formatMessageTime";

export default function TaskExpertMemory({
  taskID,
  expertID,
  projectID,
  disabled,
}: {
  taskID: string;
  expertID: string;
  projectID: string | null;
  disabled?: boolean;
}) {
  const { taskExpertMemoryApi } = useTaskHost();
  const { t } = useTranslation();
  const timeZone = useServerTimezone();
  const [open, setOpen] = useState(false);
  const [source, setSource] = useState<ExpertMemorySource>("scope");
  const [search, setSearch] = useState("");
  const [query, setQuery] = useState("");
  const [version, setVersion] = useState(0);
  const [policy, setPolicy] = useState<ExpertMemoryPolicy | null>(null);
  const [list, setList] = useState<ExpertMemoryList | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [policySaving, setPolicySaving] = useState(false);
  const [error, setError] = useState("");
  const [content, setContent] = useState("");
  const [uncertainKey, setUncertainKey] = useState<string | null>(null);
  const pending = useRef<{ key: string; requestID: string } | null>(null);
  const scopeLabel = t(
    projectID
      ? "projects.tasks.memory.projectScope"
      : "projects.tasks.memory.taskScope",
  );
  useEffect(() => {
    const timer = window.setTimeout(() => setQuery(search.trim()), 250);
    return () => window.clearTimeout(timer);
  }, [search]);
  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    setLoading(true);
    setError("");
    setList(null);
    void taskExpertMemoryApi
      .policy(taskID, controller.signal)
      .then(async (nextPolicy) => {
        if (controller.signal.aborted) return;
        setPolicy(nextPolicy);
        if (source === "legacy" && !nextPolicy.legacy_recall_enabled) return;
        const nextList = await taskExpertMemoryApi.list(
          taskID,
          source,
          query,
          controller.signal,
        );
        if (!controller.signal.aborted) setList(nextList);
      })
      .catch((err) => {
        if (!controller.signal.aborted)
          setError(apiErrorMessage(err, t("projects.tasks.failure"), t));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [taskID, source, query, version, open, t, taskExpertMemoryApi]);
  useEffect(() => {
    if (
      !open ||
      !list?.extraction_jobs?.some(
        (job) => job.status === "pending" || job.status === "running",
      )
    )
      return;
    const timer = window.setTimeout(() => setVersion((v) => v + 1), 3000);
    return () => window.clearTimeout(timer);
  }, [open, list]);
  const save = async () => {
    if (source === "legacy" || !content.trim() || saving) return;
    const key = JSON.stringify([taskID, source, content.trim()]);
    if (key === uncertainKey) return;
    if (pending.current?.key !== key)
      pending.current = { key, requestID: crypto.randomUUID() };
    setSaving(true);
    setError("");
    try {
      const result = await taskExpertMemoryApi.save(taskID, {
        expected_expert_id: expertID,
        source,
        content: content.trim(),
        request_id: pending.current.requestID,
      });
      if (result.status === "uncertain") setUncertainKey(key);
      else {
        setContent("");
        pending.current = null;
        setUncertainKey(null);
      }
      setVersion((v) => v + 1);
    } catch (err) {
      setError(apiErrorMessage(err, t("projects.tasks.failure"), t));
    } finally {
      setSaving(false);
    }
  };
  const updatePolicy = async (
    changes: Partial<
      Pick<
        ExpertMemoryPolicy,
        | "capture_enabled"
        | "legacy_recall_enabled"
        | "auto_extract_enabled"
        | "general_promotion_enabled"
      >
    >,
  ) => {
    if (!policy || policySaving || policy.policy_editable === false) return;
    setPolicySaving(true);
    setError("");
    try {
      setPolicy(
        await taskExpertMemoryApi.updatePolicy(taskID, {
          expected_expert_id: expertID,
          expected_revision: policy.config_revision,
          legacy_recall_enabled:
            changes.legacy_recall_enabled ?? policy.legacy_recall_enabled,
          ...(changes.auto_extract_enabled !== undefined
            ? { auto_extract_enabled: changes.auto_extract_enabled }
            : {}),
          ...(changes.general_promotion_enabled !== undefined
            ? { general_promotion_enabled: changes.general_promotion_enabled }
            : {}),
          ...(changes.capture_enabled !== undefined
            ? { capture_enabled: changes.capture_enabled }
            : {}),
        }),
      );
      setVersion((value) => value + 1);
    } catch (err) {
      setError(apiErrorMessage(err, t("projects.tasks.failure"), t));
    } finally {
      setPolicySaving(false);
    }
  };
  return (
    <>
      <Button
        size="small"
        icon={<Brain size={15} />}
        disabled={disabled}
        onClick={() => setOpen(true)}
      >
        {t("projects.tasks.memory.open")}
      </Button>
      <Drawer
        title={t("projects.tasks.memory.title", {
          expert: policy?.expert_name ?? "",
        })}
        open={open}
        onClose={() => setOpen(false)}
        width={560}
        destroyOnClose
      >
        <Space direction="vertical" size="middle" style={{ width: "100%" }}>
          <Typography.Paragraph>
            {t("projects.tasks.memory.scopeHint")}
          </Typography.Paragraph>
          <Segmented
            block
            aria-label={t("projects.tasks.memory.source")}
            value={source}
            options={[
              { value: "scope", label: scopeLabel },
              { value: "general", label: t("projects.tasks.memory.general") },
              { value: "legacy", label: t("projects.tasks.memory.legacy") },
            ]}
            onChange={(value) => setSource(value as ExpertMemorySource)}
          />
          {source === "legacy" && (
            <Alert
              type="info"
              showIcon
              message={t("projects.tasks.memory.legacyHint")}
            />
          )}
          {policy && (
            <Card size="small" title={t("projects.tasks.memory.policy")}>
              <Space direction="vertical">
                <Typography.Text type="secondary">
                  {t("projects.tasks.memory.policyHint")}
                </Typography.Text>
                <Space>
                  <Switch
                    aria-label={t("projects.tasks.memory.legacyRecall")}
                    checked={policy.legacy_recall_enabled}
                    loading={policySaving}
                    disabled={
                      !policy.enabled ||
                      loading ||
                      saving ||
                      policy.policy_editable === false
                    }
                    onChange={(checked) =>
                      void updatePolicy({ legacy_recall_enabled: checked })
                    }
                  />
                  <Typography.Text>
                    {t("projects.tasks.memory.legacyRecall")}
                  </Typography.Text>
                </Space>
                <Space>
                  <Switch
                    aria-label={t("projects.tasks.memory.capture")}
                    checked={policy.capture_enabled}
                    loading={policySaving}
                    disabled={
                      !policy.enabled ||
                      loading ||
                      saving ||
                      policy.policy_editable === false
                    }
                    onChange={(checked) =>
                      void updatePolicy({ capture_enabled: checked })
                    }
                  />
                  <Typography.Text>
                    {t("projects.tasks.memory.capture")}
                  </Typography.Text>
                </Space>
                {(
                  ["auto_extract_enabled", "general_promotion_enabled"] as const
                ).map((field) => (
                  <Space key={field} align="start">
                    <Switch
                      aria-label={t(`projects.tasks.memory.${field}`)}
                      checked={policy[field] ?? true}
                      loading={policySaving}
                      disabled={
                        !policy.enabled ||
                        loading ||
                        saving ||
                        policy.policy_editable === false
                      }
                      onChange={(checked) =>
                        void updatePolicy({ [field]: checked })
                      }
                    />
                    <Space direction="vertical" size={0}>
                      <Typography.Text>
                        {t(`projects.tasks.memory.${field}`)}
                      </Typography.Text>
                      <Typography.Text type="secondary">
                        {t(`projects.tasks.memory.${field}Hint`)}
                      </Typography.Text>
                    </Space>
                  </Space>
                ))}
                {policy.policy_editable === false && (
                  <Typography.Text type="secondary">
                    {t("projects.tasks.memory.sharedPolicyReadOnly")}
                  </Typography.Text>
                )}
                {!policy.enabled && (
                  <Typography.Text type="secondary">
                    {t("projects.tasks.memory.disabled")}
                  </Typography.Text>
                )}
              </Space>
            </Card>
          )}
          {!!list?.extraction_jobs?.length && (
            <Card
              size="small"
              title={t("projects.tasks.memory.extractionTitle")}
              extra={
                <Button size="small" onClick={() => setVersion((v) => v + 1)}>
                  {t("projects.refresh")}
                </Button>
              }
            >
              <Space direction="vertical" style={{ width: "100%" }}>
                {list.extraction_jobs.slice(0, 10).map((job) => (
                  <Space key={job.job_id} wrap>
                    <Tag
                      color={
                        job.status === "uncertain"
                          ? "warning"
                          : job.status === "failed"
                          ? "error"
                          : undefined
                      }
                    >
                      {t(
                        `projects.tasks.memory.extractionStatus.${job.status}`,
                      )}
                    </Tag>
                    {(job.status === "pending" || job.status === "running") && (
                      <Typography.Text type="secondary">
                        {t(
                          `projects.tasks.memory.extractionPhase.${job.phase}`,
                        )}
                      </Typography.Text>
                    )}
                    {job.status === "uncertain" && (
                      <Typography.Text type="warning">
                        {t("projects.tasks.memory.extractionUncertain")}
                      </Typography.Text>
                    )}
                  </Space>
                ))}
              </Space>
            </Card>
          )}
          <Input.Search
            aria-label={t("projects.tasks.memory.search")}
            placeholder={t("projects.tasks.memory.search")}
            value={search}
            allowClear
            onChange={(event) => setSearch(event.target.value)}
          />
          {error && (
            <Alert
              type="error"
              showIcon
              message={error}
              action={
                <Button size="small" onClick={() => setVersion((v) => v + 1)}>
                  {t("projects.refresh")}
                </Button>
              }
            />
          )}
          {loading ? (
            <Spin />
          ) : list?.entries.length ? (
            list.entries.map((entry, index) => (
              <Card key={`${entry.path}:${index}`} size="small">
                <Typography.Paragraph
                  style={{ whiteSpace: "pre-wrap" }}
                  copyable
                >
                  {entry.content}
                </Typography.Paragraph>
                {entry.occurred_at && (
                  <Typography.Text type="secondary">
                    {formatServerIsoDateTime(entry.occurred_at, timeZone)}
                  </Typography.Text>
                )}
              </Card>
            ))
          ) : source === "legacy" && !policy?.legacy_recall_enabled ? null : (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description={t("projects.tasks.memory.empty")}
            />
          )}
          {uncertainKey && (
            <Alert
              type="warning"
              showIcon
              message={t("projects.tasks.memory.uncertain")}
            />
          )}
          {source !== "legacy" && (
            <>
              <Typography.Text>
                {t("projects.tasks.memory.saveHint", {
                  scope:
                    source === "scope"
                      ? scopeLabel
                      : t("projects.tasks.memory.general"),
                })}
              </Typography.Text>
              <Input.TextArea
                aria-label={t("projects.tasks.memory.content")}
                value={content}
                disabled={saving || policySaving || !policy?.enabled}
                maxLength={16000}
                autoSize={{ minRows: 3, maxRows: 10 }}
                onChange={(event) => setContent(event.target.value)}
              />
              <Button
                type="primary"
                loading={saving}
                disabled={
                  !content.trim() ||
                  loading ||
                  policySaving ||
                  !policy?.enabled ||
                  uncertainKey ===
                    JSON.stringify([taskID, source, content.trim()])
                }
                onClick={() => void save()}
              >
                {t("projects.tasks.memory.save")}
              </Button>
            </>
          )}
        </Space>
      </Drawer>
    </>
  );
}
