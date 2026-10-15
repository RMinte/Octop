import { useEffect, useState } from "react";
import {
  Alert,
  Collapse,
  Form,
  Select,
  Space,
  Spin,
  Switch,
  Typography,
} from "antd";
import { useTranslation } from "react-i18next";
import {
  projectTasksApi,
  type Project,
  type TaskExecutionOptions,
  type TaskResources,
} from "../../api/modules/projects";
import { apiErrorMessage } from "../../utils/apiError";
import ProjectSkillsField from "./ProjectSkillsField";

export default function TaskExecutionSettings({
  value,
  onChange,
  project,
  disabled,
}: {
  value: TaskExecutionOptions;
  onChange: (value: TaskExecutionOptions) => void;
  project?: Project;
  disabled: boolean;
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [resources, setResources] = useState<TaskResources | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    if (!open) return;
    let disposed = false;
    void projectTasksApi
      .resources()
      .then((result) => {
        if (!disposed) {
          setResources(result);
          setError("");
        }
      })
      .catch((err) => {
        if (!disposed)
          setError(apiErrorMessage(err, t("projects.tasks.failure"), t));
      });
    return () => {
      disposed = true;
    };
  }, [open, t]);
  return (
    <Collapse
      size="small"
      style={{ marginBottom: 10 }}
      onChange={(keys) => setOpen(keys.length > 0)}
      items={[
        {
          key: "options",
          label: t("projects.tasks.executionOptions"),
          children: (
            <>
              <Typography.Paragraph type="secondary">
                {t("projects.tasks.optionsHint")}
              </Typography.Paragraph>
              {error && <Alert type="error" message={error} />}
              {!resources && !error ? (
                <Spin />
              ) : (
                resources && (
                  <Form layout="vertical" component="div">
                    <Form.Item label={t("projects.tasks.model")}>
                      <Select
                        aria-label={t("projects.tasks.model")}
                        allowClear
                        showSearch
                        optionFilterProp="label"
                        disabled={disabled}
                        value={value.model_ref}
                        placeholder={
                          resources.default_model_ref ||
                          t("projects.tasks.defaultModel")
                        }
                        options={resources.models.map((model) => ({
                          value: model.model_ref,
                          label: `${model.name} · ${model.model_ref}`,
                        }))}
                        onChange={(model_ref) =>
                          onChange({ ...value, model_ref })
                        }
                      />
                    </Form.Item>
                    <Form.Item
                      label={
                        <Space>
                          <Switch
                            aria-label={t("projects.tasks.customSkills")}
                            disabled={disabled}
                            checked={value.skills !== undefined}
                            onChange={(checked) =>
                              onChange({
                                ...value,
                                skills: checked
                                  ? project?.skills ?? []
                                  : undefined,
                              })
                            }
                          />
                          {t("projects.tasks.customSkills")}
                        </Space>
                      }
                    >
                      {value.skills === undefined ? (
                        <Typography.Text type="secondary">
                          {t("projects.tasks.inheritResources")}
                        </Typography.Text>
                      ) : (
                        <ProjectSkillsField
                          value={value.skills}
                          onChange={(skills) => onChange({ ...value, skills })}
                          installed={resources.skills}
                          disabled={disabled}
                          active={open}
                        />
                      )}
                    </Form.Item>
                    <Form.Item
                      label={
                        <Space>
                          <Switch
                            aria-label={t("projects.tasks.customMcp")}
                            disabled={disabled}
                            checked={value.mcp_servers !== undefined}
                            onChange={(checked) =>
                              onChange({
                                ...value,
                                mcp_servers: checked
                                  ? project?.mcp_servers ?? []
                                  : undefined,
                              })
                            }
                          />
                          {t("projects.tasks.customMcp")}
                        </Space>
                      }
                    >
                      {value.mcp_servers === undefined ? (
                        <Typography.Text type="secondary">
                          {t("projects.tasks.inheritResources")}
                        </Typography.Text>
                      ) : (
                        <Select
                          aria-label={t("projects.tasks.customMcp")}
                          mode="multiple"
                          value={value.mcp_servers}
                          disabled={disabled}
                          options={resources.mcp_servers.map((mcp) => ({
                            value: mcp.server_name,
                            label: mcp.name,
                            disabled: mcp.state !== "configured",
                          }))}
                          onChange={(mcp_servers) =>
                            onChange({ ...value, mcp_servers })
                          }
                        />
                      )}
                    </Form.Item>
                  </Form>
                )
              )}
            </>
          ),
        },
      ]}
    />
  );
}
