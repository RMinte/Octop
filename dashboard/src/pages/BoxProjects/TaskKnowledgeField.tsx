import { useTaskHost } from "../../context/TaskHostContext";
import { useEffect, useState } from "react";
import { Alert, Form, Select, Space, Spin, Switch, Typography } from "antd";
import { useTranslation } from "react-i18next";
import { type KnowledgeBase } from "../../api/modules/knowledgeBases";
import { useCurrentUser } from "../../hooks/useCurrentUser";
import { apiErrorMessage } from "../../utils/apiError";

export default function TaskKnowledgeField({
  value,
  onChange,
  active,
  disabled,
}: {
  value?: string[] | null;
  onChange: (value: string[] | undefined) => void;
  active: boolean;
  disabled: boolean;
}) {
  const { knowledgeBasesApi, hostID } = useTaskHost();
  const { t } = useTranslation();
  const user = useCurrentUser();
  const [bases, setBases] = useState<KnowledgeBase[]>([]);
  const [loading, setLoading] = useState(active);
  const [usable, setUsable] = useState(false);
  const [error, setError] = useState("");
  const inherited = value == null;
  const defaults = bases
    .filter(
      (base) => !hostID && base.default_open && base.owner_user_id === user?.id,
    )
    .map((base) => base.id);

  useEffect(() => {
    if (!active) return;
    let disposed = false;
    setLoading(true);
    setBases([]);
    setUsable(false);
    setError("");
    void Promise.all([
      knowledgeBasesApi.getCapability(),
      knowledgeBasesApi.list(),
    ])
      .then(([capability, available]) => {
        if (disposed) return;
        setUsable(capability.usable);
        setBases(available);
      })
      .catch((err) => {
        if (!disposed) {
          setUsable(false);
          setError(
            apiErrorMessage(err, t("projects.tasks.knowledge.loadFailed"), t),
          );
        }
      })
      .finally(() => {
        if (!disposed) setLoading(false);
      });
    return () => {
      disposed = true;
    };
  }, [active, t, user?.id, knowledgeBasesApi]);

  return (
    <Form.Item
      label={
        <Space>
          <Switch
            aria-label={t("projects.tasks.knowledge.custom")}
            checked={!inherited}
            disabled={disabled || loading || !!error}
            onChange={(checked) => onChange(checked ? defaults : undefined)}
          />
          {t("projects.tasks.knowledge.label")}
        </Space>
      }
    >
      {loading ? <Spin size="small" /> : null}
      {error ? <Alert type="error" message={error} /> : null}
      {!loading && !error && !usable ? (
        <Alert
          type="warning"
          message={t("projects.tasks.knowledge.unavailable")}
        />
      ) : null}
      {inherited ? (
        <Typography.Paragraph type="secondary">
          {t("projects.tasks.knowledge.inherit")}
          {defaults.length > 0 && (
            <>
              {" "}
              ·{" "}
              {bases
                .filter((base) => defaults.includes(base.id))
                .map((base) => base.name)
                .join("、")}
            </>
          )}
        </Typography.Paragraph>
      ) : (
        <Select
          aria-label={t("projects.tasks.knowledge.label")}
          mode="multiple"
          allowClear
          showSearch
          optionFilterProp="label"
          value={value}
          disabled={disabled || loading || !usable || !!error}
          placeholder={t("projects.tasks.knowledge.disabled")}
          options={bases.map((base) => ({
            value: base.id,
            label: `${base.name}${
              base.shared ? ` · ${t("projects.tasks.knowledge.shared")}` : ""
            }`,
          }))}
          onChange={onChange}
        />
      )}
      <Typography.Text type="secondary">
        {t("projects.tasks.knowledge.hint")}
      </Typography.Text>
    </Form.Item>
  );
}
