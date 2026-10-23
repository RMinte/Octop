import TaskHostBoundary from "./TaskHostBoundary";
import { useTaskHost } from "../../context/TaskHostContext";
import TaskHostDirectory from "./TaskHostDirectory";
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import {
  Alert,
  Button,
  Card,
  Empty,
  Form,
  Input,
  Modal,
  Segmented,
  Select,
  Space,
  Spin,
  Switch,
  Tabs,
  Tag,
  Typography,
} from "antd";
import {
  Archive,
  ArchiveRestore,
  Folder,
  FolderOpen,
  Pencil,
  Plus,
  RefreshCw,
  Trash2,
} from "lucide-react";
import { useTranslation } from "react-i18next";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import {
  type Project,
  type ProjectFields,
  type ProjectResources,
  type ProjectSkill,
} from "../../api/modules/projects";
import { useServerTimezone } from "../../hooks/useServerTimezone";
import PageShell from "../../layouts/PageShell";
import { message } from "../../utils/antdMessage";
import { apiErrorMessage } from "../../utils/apiError";
import { formatServerIsoDateTime } from "../../utils/formatMessageTime";
import RootDirSelect from "../Experts/components/RootDirSelect";
import styles from "./index.module.less";
import TaskConversations from "./TaskConversations";
import { useProjectList } from "./useProjectLists";
import { useTaskMessageQueue } from "./useTaskMessageQueue";
import type { TaskMessageQueue } from "./useTaskMessageQueue";
import ProjectSkillsField, { projectSkillKey } from "./ProjectSkillsField";
import workspaceStyles from "./Workspace.module.less";

const EMPTY: ProjectFields = {
  name: "",
  description: "",
  directory: null,
  client_directory: null,
  instructions: "",
  skills: [],
  mcp_servers: [],
  plugins: [],
  subagents: [],
  memory_enabled: true,
  team_enabled: false,
};
type FormValues = ProjectFields;

export default function BoxProjectsPage() {
  const [params] = useSearchParams();
  const hostID = params.get("host_id");
  return (
    <TaskHostBoundary key={hostID ?? "local"} hostID={hostID}>
      <ProjectsManager />
    </TaskHostBoundary>
  );
}
interface ProjectManagerProps {
  embedded?: boolean;
  initialProjectID?: string | null;
  editorRequest?: { project: Project | "new"; revision: number };
  queue?: TaskMessageQueue;
  onChanged?: () => void;
  onRemoved?: (projectID: string) => void;
  onOpenConversation?: (
    taskID: string | null,
    projectID: string | null,
  ) => void;
}

function EmbeddedProjectShell({
  title,
  actions,
  children,
}: {
  title: string;
  actions?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className={workspaceStyles.manager}>
      <div className={workspaceStyles.managerHeading}>
        <strong>{title}</strong>
        {actions}
      </div>
      {children}
    </div>
  );
}

/** Reuse the complete project editor in the conversation workspace. */
export function ProjectsManager({
  embedded = false,
  initialProjectID = null,
  editorRequest,
  queue: sharedQueue,
  onChanged,
  onRemoved,
  onOpenConversation,
}: ProjectManagerProps = {}) {
  const { projectsApi, hostID } = useTaskHost();
  const { t } = useTranslation();
  const localQueue = useTaskMessageQueue();
  const taskQueue = sharedQueue ?? localQueue;
  const { projectID: routeProjectID } = useParams<{ projectID: string }>();
  const [managedProjectID, setManagedProjectID] = useState(initialProjectID);
  const [managedArchived, setManagedArchived] = useState(false);
  const projectID = embedded ? managedProjectID : routeProjectID;
  useEffect(() => {
    if (embedded) setManagedProjectID(initialProjectID);
  }, [embedded, initialProjectID]);
  const rawNavigate = useNavigate();
  const navigate = useCallback(
    (url: string) => {
      if (embedded) {
        const target = new URL(url, window.location.origin);
        if (target.pathname.startsWith("/conversations")) {
          const id = target.pathname.split("/")[2];
          onOpenConversation?.(
            id ? decodeURIComponent(id) : null,
            target.searchParams.get("project_id"),
          );
          return;
        }
        if (target.pathname.startsWith("/box-projects")) {
          const id = target.pathname.split("/")[2];
          setManagedProjectID(id ? decodeURIComponent(id) : null);
          setManagedArchived(
            target.searchParams.get("project_archived") === "true",
          );
          return;
        }
      }
      if (!hostID) {
        rawNavigate(url);
        return;
      }
      const target = new URL(url, window.location.origin);
      target.searchParams.set("host_id", hostID);
      rawNavigate(`${target.pathname}${target.search}`);
    },
    [rawNavigate, hostID, embedded, onOpenConversation],
  );
  const timeZone = useServerTimezone();
  const [searchParams, setSearchParams] = useSearchParams();
  const archivedView = embedded
    ? managedArchived
    : searchParams.get("project_archived") === "true";
  const standalone =
    !embedded && !projectID && searchParams.get("view") === "tasks";
  const {
    items: projects,
    refresh: refreshProjects,
    upsert: upsertProject,
    remove: removeProject,
  } = useProjectList();
  const selected =
    projects.find((item) => item.project_id === projectID) ?? null;
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [editing, setEditing] = useState<Project | "new" | null>(null);
  const [saving, setSaving] = useState(false);
  const [archiving, setArchiving] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<Project | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState("");
  const [saveError, setSaveError] = useState("");
  const [resources, setResources] = useState<ProjectResources | null>(null);
  const [resourceError, setResourceError] = useState("");
  const [form] = Form.useForm<FormValues>();
  const generation = useRef({ version: 0 });
  const creation = useRef<{ body: string; key: string } | null>(null);
  const [detailTab, setDetailTab] = useState("general");
  const visibleProjects = projects.filter(
    (project) => !!project.archived === archivedView,
  );

  useEffect(() => {
    if (!selected || !!selected.archived === archivedView) return;
    if (embedded) {
      setManagedArchived(!!selected.archived);
      return;
    }
    const next = new URLSearchParams(searchParams);
    if (selected.archived) next.set("project_archived", "true");
    else next.delete("project_archived");
    setSearchParams(next, { replace: true });
  }, [selected, archivedView, searchParams, setSearchParams, embedded]);

  const refresh = useCallback(async () => {
    const current = ++generation.current.version;
    setError("");
    try {
      await refreshProjects();
    } catch (cause) {
      if (current === generation.current.version)
        setError(apiErrorMessage(cause, t("projects.loadFailed"), t));
    } finally {
      if (current === generation.current.version) setLoading(false);
    }
  }, [refreshProjects, t]);

  useEffect(() => {
    const pendingRequests = generation.current;
    void refresh();
    const onVisible = () => {
      if (document.visibilityState === "visible") void refresh();
    };
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener("online", onVisible);
    return () => {
      pendingRequests.version++;
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener("online", onVisible);
    };
  }, [refresh, projectID]);

  const loadResources = useCallback(async () => {
    setResourceError("");
    try {
      setResources(await projectsApi.resources());
    } catch (cause) {
      setResourceError(
        apiErrorMessage(cause, t("projects.resourcesFailed"), t),
      );
    }
  }, [projectsApi, t]);

  useEffect(() => {
    void loadResources();
  }, [loadResources]);

  function openEditor(project: Project | "new") {
    creation.current = null;
    const values = project === "new" ? EMPTY : project;
    setEditing(project);
    setSaveError("");
    setDetailTab("general");
    form.setFieldsValue(values);
    void loadResources();
  }

  const lastEditorRequest = useRef<number>();
  useEffect(() => {
    if (!editorRequest || lastEditorRequest.current === editorRequest.revision)
      return;
    lastEditorRequest.current = editorRequest.revision;
    creation.current = null;
    setEditing(editorRequest.project);
    setSaveError("");
    setDetailTab("general");
    form.setFieldsValue(
      editorRequest.project === "new" ? EMPTY : editorRequest.project,
    );
  }, [editorRequest, form]);

  async function save() {
    if (saving || editing === null) return;
    let values: FormValues;
    try {
      values = await form.validateFields();
    } catch {
      setDetailTab(
        form.getFieldError("subagents").length ? "capabilities" : "general",
      );
      return;
    }
    setSaving(true);
    setSaveError("");
    const fields: ProjectFields = {
      name: values.name.trim(),
      description: values.description ?? "",
      directory: values.directory || null,
      client_directory: values.client_directory || null,
      instructions: values.instructions ?? "",
      skills: values.skills ?? [],
      mcp_servers: values.mcp_servers ?? [],
      plugins: values.plugins ?? [],
      subagents: values.subagents ?? [],
      memory_enabled: values.memory_enabled ?? true,
      team_enabled: values.team_enabled ?? false,
    };
    const body = JSON.stringify(fields);
    if (editing === "new" && creation.current?.body !== body)
      creation.current = { body, key: crypto.randomUUID() };
    try {
      const result =
        editing === "new"
          ? await projectsApi.create(fields, creation.current!.key)
          : await projectsApi.update(editing.project_id, {
              ...fields,
              expected_revision: editing.revision,
            });
      upsertProject(result);
      onChanged?.();
      setEditing(null);
      void message.success(t("projects.saved"));
      if (projectID === result.project_id) await refresh();
      else navigate(`/box-projects/${encodeURIComponent(result.project_id)}`);
    } catch (cause) {
      setSaveError(apiErrorMessage(cause, t("projects.saveFailed"), t));
    } finally {
      setSaving(false);
    }
  }

  async function archiveProject() {
    if (!selected || archiving) return;
    setArchiving(true);
    setError("");
    try {
      const result = await projectsApi.archive(
        selected.project_id,
        !selected.archived,
        selected.revision,
      );
      upsertProject(result);
      onChanged?.();
    } catch (cause) {
      setError(apiErrorMessage(cause, t("projects.archiveFailed"), t));
    } finally {
      setArchiving(false);
    }
  }

  async function deleteProject() {
    if (!deleteTarget || deleting) return;
    setDeleting(true);
    setDeleteError("");
    try {
      await projectsApi.delete(deleteTarget.project_id);
      for (const [taskID, queued] of Object.entries(taskQueue.queues)) {
        if (queued.projectID === deleteTarget.project_id)
          taskQueue.clear(taskID);
      }
      removeProject(deleteTarget.project_id);
      onRemoved?.(deleteTarget.project_id);
      onChanged?.();
      setDeleteTarget(null);
      void message.success(t("projects.deleted"));
      navigate("/box-projects");
    } catch (cause) {
      setDeleteError(apiErrorMessage(cause, t("projects.deleteFailed"), t));
    } finally {
      setDeleting(false);
    }
  }

  const mcpOptions =
    resources?.mcp_servers.map((server) => ({
      value: server.server_name,
      label: `${server.name}${
        server.state === "configured"
          ? ""
          : ` · ${t(`projects.mcpStates.${server.state}`)}`
      }`,
    })) ?? [];
  const skillName = (skill: ProjectSkill) =>
    "source" in skill
      ? skill.name
      : resources?.skills.find(
          (entry) => projectSkillKey(entry) === projectSkillKey(skill),
        )?.name ?? skill.slug;

  const Shell = embedded ? EmbeddedProjectShell : PageShell;
  return (
    <Shell
      title={t("projects.title")}
      subtitle={t("projects.subtitle")}
      actions={
        <Space>
          <Button
            icon={<RefreshCw size={16} />}
            onClick={() => void refresh()}
            loading={loading}
          >
            {t("projects.refresh")}
          </Button>
          <Button
            type="primary"
            icon={<Plus size={16} />}
            onClick={() => openEditor("new")}
          >
            {t("projects.create")}
          </Button>
        </Space>
      }
    >
      {!embedded && <TaskHostDirectory hostID={hostID} />}
      {error && (
        <Alert type="error" showIcon message={error} className={styles.alert} />
      )}
      <div
        className={`${styles.layout} ${
          embedded ? workspaceStyles.managerLayout : ""
        }`}
      >
        <aside className={styles.list} aria-label={t("projects.list")}>
          <Button
            type={standalone ? "primary" : "default"}
            onClick={() => navigate("/conversations")}
          >
            {t("projects.tasks.all")}
          </Button>
          <Segmented
            block
            aria-label={t("projects.listScope")}
            value={archivedView ? "archived" : "active"}
            options={[
              { label: t("projects.activeList"), value: "active" },
              { label: t("projects.archivedList"), value: "archived" },
            ]}
            onChange={(value) =>
              navigate(
                value === "archived"
                  ? "/box-projects?project_archived=true"
                  : "/box-projects",
              )
            }
          />
          <Typography.Text type="secondary">
            {t("projects.list")} · {visibleProjects.length}
          </Typography.Text>
          {loading && projects.length === 0 ? (
            <Spin />
          ) : visibleProjects.length === 0 ? (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description={t("projects.empty")}
            />
          ) : (
            visibleProjects.map((project) => (
              <button
                key={project.project_id}
                type="button"
                className={`${styles.projectItem} ${
                  project.project_id === projectID ? styles.active : ""
                }`}
                onClick={() =>
                  navigate(
                    `/box-projects/${encodeURIComponent(project.project_id)}${
                      archivedView ? "?project_archived=true" : ""
                    }`,
                  )
                }
              >
                <Folder size={19} />
                <span>
                  <strong>{project.name}</strong>
                  <small>
                    {project.description ||
                      project.directory ||
                      project.client_directory ||
                      t("projects.noDescription")}
                  </small>
                </span>
              </button>
            ))
          )}
        </aside>
        <main className={styles.detail}>
          {standalone ? (
            <TaskConversations
              key="standalone"
              projectID={null}
              queue={taskQueue}
              allTasks
              onSelectTask={(id) =>
                navigate(
                  id
                    ? `/conversations/${encodeURIComponent(id)}`
                    : "/conversations",
                )
              }
            />
          ) : loading ? (
            <div className={styles.loading}>
              <Spin />
            </div>
          ) : !selected ? (
            <Empty
              description={t(
                projectID ? "projects.unavailable" : "projects.choose",
              )}
            >
              {!projectID && (
                <Button type="primary" onClick={() => openEditor("new")}>
                  {t("projects.create")}
                </Button>
              )}
            </Empty>
          ) : (
            <>
              <div
                className={`${styles.projectHeader} ${
                  embedded ? workspaceStyles.managerProjectHeader : ""
                }`}
              >
                <div>
                  <Typography.Title level={3}>{selected.name}</Typography.Title>
                  {selected.description && (
                    <Typography.Paragraph type="secondary">
                      {selected.description}
                    </Typography.Paragraph>
                  )}
                  <Typography.Text type="secondary">
                    {t("projects.updated", {
                      time: formatServerIsoDateTime(
                        selected.updated_at,
                        timeZone,
                      ),
                    })}
                  </Typography.Text>
                </div>
                <Space wrap>
                  <Button
                    icon={
                      selected.archived ? (
                        <ArchiveRestore size={15} />
                      ) : (
                        <Archive size={15} />
                      )
                    }
                    loading={archiving}
                    onClick={() => void archiveProject()}
                  >
                    {t(
                      selected.archived
                        ? "projects.restore"
                        : "projects.archive",
                    )}
                  </Button>
                  <Button
                    icon={<Pencil size={15} />}
                    onClick={() => openEditor(selected)}
                  >
                    {t("projects.edit")}
                  </Button>
                  <Button
                    danger
                    icon={<Trash2 size={15} />}
                    onClick={() => {
                      setDeleteError("");
                      setDeleteTarget(selected);
                    }}
                  >
                    {t("projects.delete")}
                  </Button>
                </Space>
              </div>
              {selected.archived && (
                <Alert type="info" message={t("projects.archivedHint")} />
              )}
              <Tabs
                defaultActiveKey={embedded ? "configuration" : "tasks"}
                key={selected.project_id}
                items={[
                  {
                    key: "tasks",
                    label: t("projects.tasks.title"),
                    children: (
                      <TaskConversations
                        key={selected.project_id}
                        projectID={selected.project_id}
                        project={selected}
                        queue={taskQueue}
                        onSelectTask={(id) =>
                          navigate(
                            id
                              ? `/conversations/${encodeURIComponent(id)}`
                              : `/conversations?project_id=${encodeURIComponent(
                                  selected.project_id,
                                )}`,
                          )
                        }
                      />
                    ),
                  },
                  {
                    key: "configuration",
                    label: t("projects.configuration"),
                    children: (
                      <div className={styles.detail}>
                        <Card
                          title={
                            <Space>
                              <FolderOpen size={17} />
                              {t("projects.directory")}
                            </Space>
                          }
                          size="small"
                        >
                          <Typography.Text copyable={!!selected.directory}>
                            {selected.directory || t("projects.noDirectory")}
                          </Typography.Text>
                        </Card>
                        <Card
                          title={t("projects.clientDirectory")}
                          size="small"
                        >
                          <Typography.Text
                            copyable={!!selected.client_directory}
                          >
                            {selected.client_directory ||
                              t("projects.noClientDirectory")}
                          </Typography.Text>
                        </Card>
                        <div className={styles.capabilities}>
                          <Card title={t("projects.plugins")} size="small">
                            <Space wrap>
                              {selected.plugins.map((id) => (
                                <Tag key={id}>
                                  {resources?.plugins.find(
                                    (p) => p.plugin_id === id,
                                  )?.name ?? id}
                                </Tag>
                              ))}
                              {!selected.plugins.length && (
                                <Typography.Text type="secondary">
                                  {t("projects.none")}
                                </Typography.Text>
                              )}
                            </Space>
                          </Card>
                          <Card
                            title={t("projects.collaboration")}
                            size="small"
                          >
                            <Space wrap>
                              {selected.memory_enabled && (
                                <Tag>{t("projects.memory")}</Tag>
                              )}
                              {selected.team_enabled && (
                                <Tag>{t("projects.team")}</Tag>
                              )}
                              {selected.subagents.map((slug) => (
                                <Tag key={slug}>
                                  {resources?.subagents.find(
                                    (s) => s.slug === slug,
                                  )?.name ?? slug}
                                </Tag>
                              ))}
                            </Space>
                          </Card>
                          <Card title={t("projects.skills")} size="small">
                            {selected.skills.length ? (
                              <Space wrap>
                                {selected.skills.map((skill) => (
                                  <Tag key={projectSkillKey(skill)}>
                                    {skillName(skill)}
                                  </Tag>
                                ))}
                              </Space>
                            ) : (
                              <Typography.Text type="secondary">
                                {t("projects.none")}
                              </Typography.Text>
                            )}
                          </Card>
                          <Card title={t("projects.mcp")} size="small">
                            {selected.mcp_servers.length ? (
                              <Space wrap>
                                {selected.mcp_servers.map((name) => (
                                  <Tag key={name}>
                                    {resources?.mcp_servers.find(
                                      (entry) => entry.server_name === name,
                                    )?.name ?? name}
                                  </Tag>
                                ))}
                              </Space>
                            ) : (
                              <Typography.Text type="secondary">
                                {t("projects.none")}
                              </Typography.Text>
                            )}
                          </Card>
                        </div>
                        <Card title={t("projects.instructions")} size="small">
                          <Typography.Paragraph className={styles.instructions}>
                            {selected.instructions ||
                              t("projects.noInstructions")}
                          </Typography.Paragraph>
                        </Card>
                      </div>
                    ),
                  },
                ].filter((item) => !embedded || item.key !== "tasks")}
              />
            </>
          )}
        </main>
      </div>
      <Modal
        title={t("projects.delete")}
        open={deleteTarget !== null}
        onCancel={() => {
          if (!deleting) setDeleteTarget(null);
        }}
        onOk={() => void deleteProject()}
        okText={t("projects.confirmDelete")}
        cancelText={t("projects.cancel")}
        okButtonProps={{ danger: true }}
        confirmLoading={deleting}
        cancelButtonProps={{ disabled: deleting }}
        closable={!deleting}
        maskClosable={!deleting}
      >
        <Typography.Paragraph>
          {t("projects.deleteConfirm", { name: deleteTarget?.name })}
        </Typography.Paragraph>
        {deleteError && <Alert type="error" showIcon message={deleteError} />}
      </Modal>
      <Modal
        title={t(editing === "new" ? "projects.create" : "projects.edit")}
        open={editing !== null}
        width={760}
        forceRender
        onCancel={() => {
          if (!saving) setEditing(null);
        }}
        onOk={() => void save()}
        confirmLoading={saving}
        okText={t("projects.save")}
        cancelText={t("projects.cancel")}
        cancelButtonProps={{ disabled: saving }}
        closable={!saving}
        maskClosable={!saving}
      >
        {saveError && (
          <Alert
            type="error"
            showIcon
            message={saveError}
            className={styles.alert}
          />
        )}
        <Form
          form={form}
          layout="vertical"
          disabled={saving}
          initialValues={{ ...EMPTY, skills: [] }}
        >
          <Tabs
            activeKey={detailTab}
            onChange={setDetailTab}
            items={[
              {
                key: "general",
                label: t("projects.general"),
                forceRender: true,
                children: (
                  <>
                    <Form.Item
                      name="name"
                      label={t("projects.name")}
                      rules={[
                        {
                          required: true,
                          whitespace: true,
                          message: t("projects.nameRequired"),
                        },
                      ]}
                    >
                      <Input maxLength={128} />
                    </Form.Item>
                    <Form.Item
                      name="description"
                      label={t("projects.description")}
                    >
                      <Input.TextArea rows={2} maxLength={1024} />
                    </Form.Item>
                    <Form.Item
                      label={t("projects.directory")}
                      extra={t("projects.directoryHint")}
                    >
                      <Form.Item name="directory" noStyle>
                        {hostID ? (
                          <Input
                            disabled={saving || !resources}
                            placeholder={t(
                              "projects.tasks.hosts.remoteDirectory",
                            )}
                          />
                        ) : (
                          <RootDirSelect
                            disabled={saving || !resources}
                            treeRoots={resources?.directory_roots}
                          />
                        )}
                      </Form.Item>
                      <Button
                        type="link"
                        size="small"
                        onClick={() => form.setFieldValue("directory", null)}
                      >
                        {t("projects.clearDirectory")}
                      </Button>
                    </Form.Item>
                    <Form.Item
                      name="client_directory"
                      label={t("projects.clientDirectory")}
                      extra={t("projects.clientDirectoryHint")}
                    >
                      <Input
                        maxLength={4096}
                        allowClear
                        placeholder={t("projects.clientDirectoryPlaceholder")}
                      />
                    </Form.Item>
                  </>
                ),
              },
              {
                key: "capabilities",
                label: t("projects.capabilities"),
                forceRender: true,
                children: (
                  <>
                    {resourceError && (
                      <Alert
                        type="error"
                        showIcon
                        message={resourceError}
                        action={
                          <Button onClick={() => void loadResources()}>
                            {t("projects.refresh")}
                          </Button>
                        }
                        className={styles.alert}
                      />
                    )}
                    <Form.Item
                      name="skills"
                      label={t("projects.skills")}
                      extra={t("projects.skillsHint")}
                    >
                      <ProjectSkillsField
                        installed={resources?.skills ?? []}
                        disabled={saving}
                        active={
                          editing !== null && detailTab === "capabilities"
                        }
                      />
                    </Form.Item>
                    <Form.Item
                      name="mcp_servers"
                      label={t("projects.mcp")}
                      extra={t("projects.mcpHint")}
                    >
                      <Select
                        mode="multiple"
                        showSearch
                        optionFilterProp="label"
                        options={mcpOptions}
                        loading={!resources && !resourceError}
                        placeholder={t("projects.selectMcp")}
                      />
                    </Form.Item>
                    <Form.Item
                      name="plugins"
                      label={t("projects.plugins")}
                      extra={t("projects.pluginsHint")}
                    >
                      <Select
                        mode="multiple"
                        showSearch
                        optionFilterProp="label"
                        options={resources?.plugins.map((p) => ({
                          value: p.plugin_id,
                          label: p.name,
                          disabled: !p.available,
                        }))}
                        placeholder={t("projects.selectPlugins")}
                      />
                    </Form.Item>
                    <Form.Item
                      name="subagents"
                      label={t("projects.subagents")}
                      extra={t("projects.subagentsHint")}
                      dependencies={["team_enabled"]}
                      rules={[
                        ({ getFieldValue }) => ({
                          validator: (_, value: string[]) =>
                            getFieldValue("team_enabled") && !value?.length
                              ? Promise.reject(
                                  new Error(t("projects.teamRolesRequired")),
                                )
                              : Promise.resolve(),
                        }),
                      ]}
                    >
                      <Select
                        mode="multiple"
                        showSearch
                        optionFilterProp="label"
                        maxCount={16}
                        options={resources?.subagents.map((s) => ({
                          value: s.slug,
                          label: s.name,
                        }))}
                      />
                    </Form.Item>
                    <Form.Item
                      name="memory_enabled"
                      label={t("projects.memory")}
                      valuePropName="checked"
                      extra={t("projects.memoryHint")}
                    >
                      <Switch />
                    </Form.Item>
                    <Form.Item
                      name="team_enabled"
                      label={t("projects.team")}
                      valuePropName="checked"
                      extra={t("projects.teamHint")}
                    >
                      <Switch />
                    </Form.Item>
                  </>
                ),
              },
              {
                key: "instructions",
                label: t("projects.instructions"),
                forceRender: true,
                children: (
                  <Form.Item
                    name="instructions"
                    label={t("projects.instructions")}
                    extra={t("projects.instructionsHint")}
                  >
                    <Input.TextArea
                      rows={13}
                      maxLength={32768}
                      showCount
                      placeholder={t("projects.instructionsPlaceholder")}
                    />
                  </Form.Item>
                ),
              },
            ]}
          />
        </Form>
      </Modal>
    </Shell>
  );
}
