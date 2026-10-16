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
  const reasoning = resources?.models.find(
    (model) =>
      model.model_ref === (value.model_ref || resources.default_model_ref),
  )?.reasoning_config;
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
                          onChange({
                            ...value,
                            model_ref,
                            reasoning_mode: undefined,
                            reasoning_effort: undefined,
                          })
                        }
                      />
                    </Form.Item>
                    {reasoning &&
                      (reasoning.adapter === "status_only" ? (
                        <Typography.Paragraph type="secondary">
                          {t("chat.reasoningAlways")}
                        </Typography.Paragraph>
                      ) : (
                        <>
                          <Form.Item label={t("chat.reasoningMode")}>
                            <Select
                              aria-label={t("chat.reasoningMode")}
                              disabled={disabled}
                              value={value.reasoning_mode ?? "default"}
                              options={[
                                {
                                  value: "default",
                                  label: t("projects.tasks.reasoningDefault"),
                                },
                                {
                                  value: "auto",
                                  label: t("chat.reasoningAuto"),
                                },
                                {
                                  value: "enabled",
                                  label: t("chat.reasoningEnabled"),
                                },
                                ...(reasoning.toggle
                                  ? [
                                      {
                                        value: "disabled",
                                        label: t("chat.reasoningDisabled"),
                                      },
                                    ]
                                  : []),
                              ]}
                              onChange={(mode) =>
                                onChange({
                                  ...value,
                                  reasoning_mode:
                                    mode === "default"
                                      ? undefined
                                      : (mode as TaskExecutionOptions["reasoning_mode"]),
                                  reasoning_effort:
                                    mode === "disabled" ||
                                    (mode === "default" &&
                                      reasoning.default_mode === "disabled")
                                      ? undefined
                                      : value.reasoning_effort,
                                })
                              }
                            />
                          </Form.Item>
                          {reasoning.efforts.length > 0 && (
                            <Form.Item label={t("chat.reasoningEffort")}>
                              <Select
                                aria-label={t("chat.reasoningEffort")}
                                disabled={
                                  disabled ||
                                  (value.reasoning_mode ??
                                    reasoning.default_mode) === "disabled"
                                }
                                allowClear
                                value={value.reasoning_effort}
                                placeholder={
                                  reasoning.default_effort ||
                                  t("projects.tasks.reasoningDefault")
                                }
                                options={reasoning.efforts.map((effort) => ({
                                  value: effort,
                                  label: effort,
                                }))}
                                onChange={(reasoning_effort) =>
                                  onChange({ ...value, reasoning_effort })
                                }
                              />
                            </Form.Item>
                          )}
                        </>
                      ))}
                    {(["plugins", "subagents"] as const).map((key) => (
                      <Form.Item
                        key={key}
                        label={
                          <Space>
                            <Switch
                              aria-label={t(
                                `projects.tasks.custom${
                                  key === "plugins" ? "Plugins" : "Subagents"
                                }`,
                              )}
                              disabled={disabled}
                              checked={value[key] !== undefined}
                              onChange={(checked) =>
                                onChange({
                                  ...value,
                                  [key]: checked
                                    ? project?.[key] ?? []
                                    : undefined,
                                })
                              }
                            />
                            {t(`projects.${key}`)}
                          </Space>
                        }
                      >
                        {value[key] === undefined ? (
                          <Typography.Text type="secondary">
                            {t("projects.tasks.inheritResources")}
                          </Typography.Text>
                        ) : (
                          <Select
                            mode="multiple"
                            showSearch
                            optionFilterProp="label"
                            disabled={disabled}
                            aria-label={t(`projects.${key}`)}
                            value={value[key]}
                            options={
                              key === "plugins"
                                ? resources.plugins.map((p) => ({
                                    value: p.plugin_id,
                                    label: p.name,
                                    disabled: !p.available,
                                  }))
                                : resources.subagents.map((s) => ({
                                    value: s.slug,
                                    label: s.name,
                                  }))
                            }
                            onChange={(selected) =>
                              onChange({ ...value, [key]: selected })
                            }
                          />
                        )}
                      </Form.Item>
                    ))}
                    {(["memory_enabled", "team_enabled"] as const).map(
                      (key) => (
                        <Form.Item
                          key={key}
                          label={t(
                            key === "memory_enabled"
                              ? "projects.memory"
                              : "projects.team",
                          )}
                        >
                          <Select
                            disabled={disabled}
                            value={
                              value[key] === undefined
                                ? "inherit"
                                : String(value[key])
                            }
                            aria-label={t(
                              key === "memory_enabled"
                                ? "projects.memory"
                                : "projects.team",
                            )}
                            options={[
                              {
                                value: "inherit",
                                label: t("projects.tasks.inheritResources"),
                              },
                              { value: "true", label: t("projects.enabled") },
                              { value: "false", label: t("projects.disabled") },
                            ]}
                            onChange={(selected) =>
                              onChange({
                                ...value,
                                [key]:
                                  selected === "inherit"
                                    ? undefined
                                    : selected === "true",
                              })
                            }
                          />
                        </Form.Item>
                      ),
                    )}
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
