import { useEffect, useRef, useState, type ReactNode } from "react";
import {
  Alert,
  Button,
  Collapse,
  Form,
  Modal,
  Popover,
  Select,
  Space,
  Spin,
  Switch,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import { RotateCcw, SlidersHorizontal, X } from "lucide-react";
import { useTranslation } from "react-i18next";
import { useTaskHost } from "../../context/TaskHostContext";
import {
  type Project,
  type ProjectSkill,
  type TaskExecutionOptions,
  type TaskResources,
  type EngineSelection,
  type EngineCatalogEntry,
} from "../../api/modules/projects";
import {
  engineCapabilities,
  engineEntry,
  OCTOP_ENGINE,
  taskOptionsForEngine,
} from "./taskEngine";
import { apiErrorMessage } from "../../utils/apiError";
import ProjectSkillsField, { projectSkillKey } from "./ProjectSkillsField";
import TaskKnowledgeField from "./TaskKnowledgeField";
import styles from "./TaskComposer.module.less";

export interface TaskExecutionSettingsSlots {
  model: ReactNode;
  options: ReactNode;
  resourceChips: ReactNode;
}

export interface TaskExecutionSettingsProps {
  value: TaskExecutionOptions;
  onChange: (value: TaskExecutionOptions) => void;
  project?: Project;
  disabled: boolean;
  selectedExpertID?: string | null;
  engine?: EngineSelection;
  onEngineChange?: (engine: EngineSelection) => void;
  engineCatalog?: EngineCatalogEntry[];
  compact?: boolean;
  scopeKey?: string;
  onResourcesLoaded?: (resources: TaskResources) => void;
  children?: (slots: TaskExecutionSettingsSlots) => ReactNode;
}

export default function TaskExecutionSettings({
  value,
  onChange,
  project,
  disabled,
  selectedExpertID,
  engine = OCTOP_ENGINE,
  onEngineChange,
  engineCatalog,
  compact = false,
  scopeKey,
  onResourcesLoaded,
  children,
}: TaskExecutionSettingsProps) {
  const { projectTasksApi } = useTaskHost();
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [skillPickerOpen, setSkillPickerOpen] = useState(false);
  const [resources, setResources] = useState<TaskResources | null>(null);
  const [error, setError] = useState("");
  const [resourceVersion, setResourceVersion] = useState(0);
  const resourceCallback = useRef(onResourcesLoaded);
  const skillConfirmation = useRef<ReturnType<typeof Modal.confirm>>();
  resourceCallback.current = onResourcesLoaded;
  const external = engine.engine_id !== "octop";
  const capabilities = engineCapabilities(
    engine,
    engineCatalog ?? resources?.execution_engines,
  );
  const currentEngine = engineEntry(
    engine,
    engineCatalog ?? resources?.execution_engines,
  );
  useEffect(() => {
    setOpen(false);
    setSkillPickerOpen(false);
    skillConfirmation.current?.destroy();
    return () => skillConfirmation.current?.destroy();
  }, [scopeKey]);
  const selectedExpert = resources?.experts?.find(
    (expert) => expert.expert_id === selectedExpertID,
  );
  const inheritedSkills = Array.from(
    new Map(
      [
        ...(selectedExpert?.default_skills ?? []),
        ...(project?.skills ?? []),
      ].map((skill) => [projectSkillKey(skill), skill]),
    ).values(),
  );
  const skillDefaultsUnavailable =
    value.skills === undefined &&
    !!selectedExpertID &&
    selectedExpert?.default_skills === undefined;
  const selectedRef = value.model_ref || resources?.default_model_ref;
  const selectedModel = resources?.models.find(
    (model) => model.model_ref === selectedRef,
  );
  const defaultModel = resources?.models.find(
    (model) => model.model_ref === resources.default_model_ref,
  );
  const modelLabel = (model: TaskResources["models"][number]) =>
    `${model.name} · ${model.model_ref}${
      model.supports_images ? ` · ${t("projects.tasks.supportsImages")}` : ""
    }`;
  const defaultLabel = defaultModel
    ? modelLabel(defaultModel)
    : resources?.default_model_ref ||
      t(resources ? "models.noModels" : "common.loading");
  const modelGroups = [
    { label: t("models.boxProvider"), box: true },
    { label: t("projects.tasks.remoteModels"), box: false },
  ]
    .map((group) => ({
      label: group.label,
      options: (resources?.models ?? [])
        .filter((model) => (model.provider_kind === "box") === group.box)
        .map((model) => ({ value: model.model_ref, label: modelLabel(model) })),
    }))
    .filter((group) => group.options.length > 0);
  const reasoning = external ? undefined : selectedModel?.reasoning_config;
  const overrideCount = Object.entries(
    taskOptionsForEngine(value, engine),
  ).filter(([key, option]) => key !== "model_ref" && option != null).length;
  const modelHint =
    selectedExpertID && !value.model_ref
      ? t("projects.tasks.expertModelHint")
      : t("projects.tasks.usingModel", {
          model: selectedModel
            ? modelLabel(selectedModel)
            : selectedRef ||
              (resources ? t("models.noModels") : t("common.loading")),
        });

  useEffect(() => {
    let disposed = false;
    void projectTasksApi
      .resources()
      .then((result) => {
        if (!disposed) {
          setResources(result);
          resourceCallback.current?.(result);
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
  }, [projectTasksApi, resourceVersion, t]);

  const changeSkills = (skills: ProjectSkill[] | undefined) => {
    if (skills !== undefined && skillDefaultsUnavailable) return;
    const apply = () => onChange({ ...value, skills });
    if (
      skills !== undefined &&
      value.skills === undefined &&
      selectedExpert?.has_workspace_skills
    ) {
      skillConfirmation.current = Modal.confirm({
        title: t("projects.composerResources.replaceWorkspaceSkillsTitle"),
        content: t("projects.composerResources.replaceWorkspaceSkills"),
        okText: t("projects.composerResources.replaceWorkspaceSkillsConfirm"),
        onOk: apply,
      });
    } else apply();
  };

  const listField = (key: "plugins" | "subagents") => (
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
            disabled={disabled || !capabilities[key]}
            checked={value[key] !== undefined}
            onChange={(checked) =>
              onChange({
                ...value,
                [key]: checked ? project?.[key] ?? [] : undefined,
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
          disabled={disabled || !capabilities[key]}
          aria-label={t(`projects.${key}`)}
          value={value[key]}
          options={
            key === "plugins"
              ? resources?.plugins.map((p) => ({
                  value: p.plugin_id,
                  label: p.name,
                  disabled: !p.available,
                }))
              : resources?.subagents.map((s) => ({
                  value: s.slug,
                  label: s.name,
                }))
          }
          onChange={(selected) => onChange({ ...value, [key]: selected })}
        />
      )}
    </Form.Item>
  );
  const booleanField = (key: "memory_enabled" | "team_enabled") => (
    <Form.Item
      key={key}
      label={t(key === "memory_enabled" ? "projects.memory" : "projects.team")}
    >
      <Select
        disabled={
          disabled ||
          (key === "memory_enabled" ? !capabilities.memory : external)
        }
        value={value[key] === undefined ? "inherit" : String(value[key])}
        aria-label={t(
          key === "memory_enabled" ? "projects.memory" : "projects.team",
        )}
        options={[
          { value: "inherit", label: t("projects.tasks.inheritResources") },
          { value: "true", label: t("projects.enabled") },
          { value: "false", label: t("projects.disabled") },
        ]}
        onChange={(selected) =>
          onChange({
            ...value,
            [key]: selected === "inherit" ? undefined : selected === "true",
          })
        }
      />
    </Form.Item>
  );
  const content = (
    <div
      className={styles.optionsPanel}
      role="dialog"
      aria-label={t("projects.tasks.executionOptions")}
    >
      <div className={styles.optionsHeader}>
        <Typography.Text strong>
          {t("projects.composer.turnSettings")}
        </Typography.Text>
        <Button
          size="small"
          type="text"
          icon={<X size={15} />}
          aria-label={t("common.close")}
          onClick={() => setOpen(false)}
        />
      </div>
      <Typography.Paragraph type="secondary" className={styles.optionsHint}>
        {t("projects.composer.overrideHint")}
      </Typography.Paragraph>
      {error && <Alert type="error" message={error} />}
      {external ? (
        <Alert
          type="info"
          showIcon
          message={t("projects.tasks.engineOptionsHint")}
          description={t("projects.tasks.engineUnsupported", {
            features: (
              [
                "skills",
                "mcp",
                "plugins",
                "subagents",
                "knowledge",
                "memory",
              ] as const
            )
              .filter((key) => !capabilities[key])
              .map((key) => t(`projects.tasks.engineCapabilities.${key}`))
              .join("、"),
          })}
        />
      ) : null}
      {!resources && !error ? (
        <Spin />
      ) : (
        resources && (
          <Form layout="vertical" component="div" size="small">
            <Collapse
              size="small"
              ghost
              defaultActiveKey={["resources"]}
              items={[
                {
                  key: "reasoning",
                  collapsible: external ? "disabled" : undefined,
                  label: t("projects.composer.groups.reasoning"),
                  children: (
                    <>
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

                      {!reasoning && (
                        <Typography.Text type="secondary">
                          {t("projects.tasks.reasoningDefault")}
                        </Typography.Text>
                      )}
                    </>
                  ),
                },
                {
                  key: "resources",
                  label: t("projects.composer.groups.resources"),
                  children: (
                    <>
                      <Form.Item
                        label={
                          <Space>
                            <Switch
                              aria-label={t("projects.tasks.customSkills")}
                              disabled={
                                disabled ||
                                skillDefaultsUnavailable ||
                                !capabilities.skills
                              }
                              checked={value.skills !== undefined}
                              onChange={(checked) =>
                                changeSkills(
                                  checked ? inheritedSkills : undefined,
                                )
                              }
                            />
                            {t("projects.tasks.customSkills")}
                          </Space>
                        }
                      >
                        {value.skills === undefined ? (
                          <Typography.Text type="secondary">
                            {t(
                              skillDefaultsUnavailable
                                ? "projects.composerResources.skillDefaultsUnavailable"
                                : "projects.tasks.inheritResources",
                            )}
                          </Typography.Text>
                        ) : (
                          <ProjectSkillsField
                            key={scopeKey}
                            value={value.skills}
                            onChange={(skills) => changeSkills(skills)}
                            installed={resources.skills}
                            disabled={disabled || !capabilities.skills}
                            active={open}
                            onPickerOpenChange={setSkillPickerOpen}
                          />
                        )}
                      </Form.Item>

                      <Form.Item
                        label={
                          <Space>
                            <Switch
                              aria-label={t("projects.tasks.customMcp")}
                              disabled={disabled || !capabilities.mcp}
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
                            disabled={disabled || !capabilities.mcp}
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

                      <TaskKnowledgeField
                        value={value.knowledge_base_ids}
                        onChange={(knowledge_base_ids) =>
                          onChange({ ...value, knowledge_base_ids })
                        }
                        active={open}
                        disabled={disabled || !capabilities.knowledge}
                      />
                      {listField("plugins")}
                    </>
                  ),
                },
                {
                  key: "collaboration",
                  label: t("projects.composer.groups.collaboration"),
                  children: (
                    <>
                      {listField("subagents")}
                      {booleanField("team_enabled")}
                    </>
                  ),
                },
                {
                  key: "memory",
                  label: t("projects.composer.groups.memory"),
                  children: (
                    <>
                      {booleanField("memory_enabled")}
                      {!!selectedExpertID && (
                        <Form.Item label={t("projects.tasks.expertMemory")}>
                          <Select
                            aria-label={t("projects.tasks.expertMemory")}
                            disabled={disabled || !capabilities.memory}
                            value={
                              value.expert_memory_enabled === false
                                ? "off"
                                : "inherit"
                            }
                            options={[
                              {
                                value: "inherit",
                                label: t("projects.tasks.expertMemoryDefault"),
                              },
                              { value: "off", label: t("projects.disabled") },
                            ]}
                            onChange={(selected) =>
                              onChange({
                                ...value,
                                expert_memory_enabled:
                                  selected === "off" ? false : undefined,
                              })
                            }
                          />
                        </Form.Item>
                      )}

                      {!!selectedExpertID && (
                        <Typography.Text type="secondary">
                          {t("projects.tasks.expertMemoryDefault")}
                        </Typography.Text>
                      )}
                    </>
                  ),
                },
              ]}
            />
            <Button
              type="text"
              size="small"
              icon={<RotateCcw size={14} />}
              disabled={disabled || !overrideCount}
              onClick={() => onChange({ model_ref: value.model_ref })}
            >
              {t("projects.composer.resetOverrides")}
            </Button>
          </Form>
        )
      )}
    </div>
  );
  const model = external ? (
    <div className={compact ? styles.modelCompact : styles.modelFull}>
      <Tooltip title={t("projects.tasks.engineModelHint")}>
        {capabilities.model_selection && currentEngine?.models.length ? (
          <Select
            aria-label={t("projects.tasks.engineModel")}
            className={styles.modelSelect}
            size={compact ? "small" : "middle"}
            variant={compact ? "borderless" : "outlined"}
            disabled={disabled || !onEngineChange}
            value={engine.model_id ?? ""}
            options={[
              { value: "", label: t("projects.tasks.engineDefaultModel") },
              ...currentEngine.models.map((model) => ({
                value: model.model_id,
                label: model.label,
              })),
            ]}
            onChange={(model_id) =>
              onEngineChange?.({ ...engine, model_id: model_id || null })
            }
          />
        ) : (
          <Typography.Text type="secondary">
            {engine.model_id || t("projects.tasks.engineDefaultModel")}
          </Typography.Text>
        )}
      </Tooltip>
    </div>
  ) : (
    <div className={compact ? styles.modelCompact : styles.modelFull}>
      {!compact && (
        <Typography.Text>{t("projects.tasks.model")}</Typography.Text>
      )}
      <Tooltip
        title={
          <>
            {modelHint}
            <br />
            {t("projects.tasks.modelSelectionHint")}
          </>
        }
      >
        <Select
          aria-label={t("projects.tasks.model")}
          className={styles.modelSelect}
          size={compact ? "small" : "middle"}
          variant={compact ? "borderless" : "outlined"}
          showSearch
          optionFilterProp="label"
          disabled={disabled}
          loading={!resources && !error}
          value={value.model_ref ?? ""}
          options={[
            {
              value: "",
              label: t(
                selectedExpertID
                  ? "projects.tasks.followExpertModel"
                  : "projects.tasks.followDefaultModel",
                { model: defaultLabel },
              ),
            },
            ...modelGroups,
          ]}
          popupMatchSelectWidth={false}
          popupClassName={styles.modelDropdown}
          onOpenChange={(visible) => {
            if (visible) setResourceVersion((version) => version + 1);
          }}
          onChange={(model_ref) =>
            onChange({
              ...value,
              model_ref: model_ref || undefined,
              reasoning_mode: undefined,
              reasoning_effort: undefined,
            })
          }
        />
      </Tooltip>
      {!compact && (
        <>
          <Space wrap size={6}>
            <Typography.Text type="secondary">{modelHint}</Typography.Text>
            {selectedModel && (
              <Tag>
                {t(
                  selectedModel.provider_kind === "box"
                    ? "models.boxProvider"
                    : "projects.tasks.remoteModels",
                )}
              </Tag>
            )}
          </Space>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {t("projects.tasks.modelSelectionHint")}
          </Typography.Text>
          {error && <Alert type="error" message={error} />}
        </>
      )}
    </div>
  );
  const options = (
    <Popover
      content={content}
      open={open}
      onOpenChange={(next) => {
        if (!skillPickerOpen) setOpen(next);
      }}
      trigger="click"
      placement="topRight"
      overlayClassName={styles.optionsPopover}
    >
      <Button
        size="small"
        type={overrideCount ? "default" : "text"}
        icon={<SlidersHorizontal size={14} />}
        aria-label={t("projects.tasks.executionOptions")}
      >
        {t(
          compact
            ? "projects.composer.turnSettings"
            : "projects.tasks.executionOptions",
        )}
        {overrideCount ? ` · ${overrideCount}` : ""}
      </Button>
    </Popover>
  );
  const chips: ReactNode[] = [];
  const chip = (
    key: string,
    name: string,
    remove: () => void,
    inherited = false,
    unavailable = false,
  ) => {
    chips.push(
      <span className={styles.resourceChip} key={key}>
        <span>
          {name}
          {inherited && (
            <span className={styles.inheritedMark}>
              {" "}
              · {t("projects.composer.inherited")}
            </span>
          )}
        </span>
        <Button
          size="small"
          type="text"
          icon={<X size={12} />}
          disabled={disabled || unavailable}
          aria-label={t("projects.composer.removeResource", { name })}
          onClick={remove}
        />
      </span>,
    );
  };
  const skillList = value.skills ?? inheritedSkills;
  skillList.forEach((skill) => {
    const name =
      "source" in skill
        ? skill.name
        : resources?.skills.find(
            (entry) => projectSkillKey(entry) === projectSkillKey(skill),
          )?.name ?? skill.slug;
    chip(
      `skill:${projectSkillKey(skill)}`,
      name,
      () =>
        changeSkills(
          skillList.filter(
            (item) => projectSkillKey(item) !== projectSkillKey(skill),
          ),
        ),
      value.skills === undefined,
      skillDefaultsUnavailable,
    );
  });
  if (value.skills?.length === 0)
    chip(
      "skills:empty",
      `${t("projects.tasks.customSkills")} · ${t(
        "projects.composer.explicitlyEmpty",
      )}`,
      () => onChange({ ...value, skills: undefined }),
    );
  if (value.skills === undefined && selectedExpert?.has_workspace_skills)
    chips.push(
      <span className={styles.inheritedResource} key="workspace-skills">
        {t("projects.composer.workspaceSkills")}
      </span>,
    );
  for (const key of ["mcp_servers", "plugins", "subagents"] as const) {
    const chosen = value[key];
    if (chosen === undefined) continue;
    const label =
      key === "mcp_servers" ? t("projects.mcp") : t(`projects.${key}`);
    if (!chosen.length)
      chip(
        `${key}:empty`,
        `${label} · ${t("projects.composer.explicitlyEmpty")}`,
        () => onChange({ ...value, [key]: undefined }),
      );
    chosen.forEach((id) => {
      const name =
        key === "mcp_servers"
          ? resources?.mcp_servers.find((item) => item.server_name === id)?.name
          : key === "plugins"
          ? resources?.plugins.find((item) => item.plugin_id === id)?.name
          : resources?.subagents.find((item) => item.slug === id)?.name;
      chip(`${key}:${id}`, name || id, () =>
        onChange({ ...value, [key]: chosen.filter((item) => item !== id) }),
      );
    });
  }
  if (value.knowledge_base_ids != null) {
    const ids = value.knowledge_base_ids;
    chip(
      "knowledge",
      `${t("projects.tasks.knowledge.label")} · ${
        ids.length || t("projects.composer.explicitlyEmpty")
      }`,
      () =>
        onChange({ ...value, knowledge_base_ids: ids.length ? [] : undefined }),
    );
  }
  for (const key of [
    "memory_enabled",
    "expert_memory_enabled",
    "team_enabled",
  ] as const) {
    if (value[key] === undefined) continue;
    const name = t(
      key === "memory_enabled"
        ? "projects.memory"
        : key === "expert_memory_enabled"
        ? "projects.tasks.expertMemory"
        : "projects.team",
    );
    chip(
      key,
      `${name} · ${t(value[key] ? "projects.enabled" : "projects.disabled")}`,
      () => onChange({ ...value, [key]: undefined }),
    );
  }
  const resourceChips = chips.length ? (
    <div
      className={styles.resourceChips}
      aria-label={t("projects.composer.selectedResources")}
    >
      {chips}
    </div>
  ) : null;
  return children ? (
    children({ model, options, resourceChips })
  ) : (
    <>
      {model}
      {resourceChips}
      {options}
    </>
  );
}
