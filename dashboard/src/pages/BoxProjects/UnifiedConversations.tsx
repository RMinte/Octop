import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Alert, Button, Drawer, Modal, Popover, Select, Spin } from "antd";
import {
  ChevronDown,
  Folder,
  History,
  Monitor,
  MoreHorizontal,
  Settings2,
} from "lucide-react";
import { useTranslation } from "react-i18next";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { type Project } from "../../api/modules/projects";
import { useTaskHost } from "../../context/TaskHostContext";
import { apiErrorMessage } from "../../utils/apiError";
import { ProjectsManager } from "./index";
import ProjectNavigator, { type WorkspaceScope } from "./ProjectNavigator";
import ProjectOverview from "./ProjectOverview";
import TaskConversations, {
  type TaskConversationListScope,
} from "./TaskConversations";
import TaskHostBoundary from "./TaskHostBoundary";
import TaskHostDirectory from "./TaskHostDirectory";
import TaskMigrations from "./TaskMigrations";
import { useTaskMessageQueue } from "./useTaskMessageQueue";
import styles from "./Workspace.module.less";

/** A stable Task and its project share one workspace, independent of expert identity. */
export default function UnifiedConversations() {
  const [params] = useSearchParams();
  const hostID = params.get("host_id");
  return (
    <TaskHostBoundary key={hostID ?? "local"} hostID={hostID}>
      <UnifiedConversationBody />
    </TaskHostBoundary>
  );
}

function UnifiedConversationBody() {
  const { projectsApi, projectTasksApi, hostID } = useTaskHost();
  const { t } = useTranslation();
  const { taskID } = useParams<{ taskID: string }>();
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const queue = useTaskMessageQueue();
  const [projects, setProjects] = useState<Project[]>([]);
  const [resolved, setResolved] = useState<{
    taskID: string | undefined;
    projectID: string | null;
    project?: Project;
    expertID: string | null;
    projectOverview: boolean;
  } | null>(null);
  const [error, setError] = useState("");
  const [version, setVersion] = useState(0);
  const [projectMenuOpen, setProjectMenuOpen] = useState(false);
  const [managerOpen, setManagerOpen] = useState(false);
  const [hostsOpen, setHostsOpen] = useState(false);
  const workbenchDirty = useRef(false);
  const setWorkbenchDirty = useCallback((dirty: boolean) => {
    workbenchDirty.current = dirty;
  }, []);
  const navigationConfirmation = useRef(false);
  const [managedProjectID, setManagedProjectID] = useState<string | null>(null);
  const [editorRequest, setEditorRequest] = useState<{
    project: Project | "new";
    revision: number;
  }>();
  const projectParam = params.get("project_id");
  const expertParam = params.get("expert_id");
  const scopeProjectID = params.get("scope_project");
  const rawScope = params.get("workspace_scope");
  const scope: WorkspaceScope =
    rawScope &&
    ["all", "active", "pinned", "standalone", "project"].includes(rawScope)
      ? (rawScope as WorkspaceScope)
      : "all";
  const projectPage = params.get("view") === "project";
  const refresh = useCallback(() => setVersion((value) => value + 1), []);

  useEffect(() => {
    let disposed = false;
    void (async () => {
      try {
        const rows = await projectsApi.list();
        const projectID = taskID
          ? (await projectTasksApi.get(taskID)).project_id
          : projectParam;
        const project = projectID
          ? rows.find((row) => row.project_id === projectID) ??
            (await projectsApi.get(projectID))
          : undefined;
        if (!disposed) {
          setProjects(rows);
          setResolved({
            taskID,
            projectID,
            project,
            expertID: expertParam,
            projectOverview: projectPage && !taskID,
          });
          setError("");
        }
      } catch (cause) {
        if (!disposed)
          setError(apiErrorMessage(cause, t("projects.tasks.failure"), t));
      }
    })();
    return () => {
      disposed = true;
    };
  }, [
    taskID,
    projectParam,
    expertParam,
    projectPage,
    version,
    t,
    projectsApi,
    projectTasksApi,
  ]);

  const guardNavigation = (action: () => void, leaving = true) => {
    if (!workbenchDirty.current || !leaving) {
      action();
      return;
    }
    if (navigationConfirmation.current) return;
    navigationConfirmation.current = true;
    Modal.confirm({
      title: t("projects.tasks.workbench.unsaved"),
      content: t("projects.tasks.workbench.discardHint"),
      okText: t("projects.tasks.workbench.discard"),
      cancelText: t("common.cancel"),
      onOk: () => {
        navigationConfirmation.current = false;
        setWorkbenchDirty(false);
        action();
      },
      onCancel: () => {
        navigationConfirmation.current = false;
      },
    });
  };
  const route = (
    id: string | null,
    next: URLSearchParams,
    confirmed = false,
  ) => {
    const leaving =
      id !== (resolved?.taskID ?? null) ||
      (!id && next.get("project_id") !== (resolved?.projectID ?? null)) ||
      (next.get("view") === "project") !== !!resolved?.projectOverview;
    const go = () =>
      navigate(
        (id ? "/conversations/" + encodeURIComponent(id) : "/conversations") +
          (next.size ? "?" + next.toString() : ""),
      );
    if (confirmed) {
      setWorkbenchDirty(false);
      go();
    } else guardNavigation(go, leaving);
  };
  const startConversation = (id: string | null, projectID: string | null) => {
    const next = new URLSearchParams(params);
    next.delete("expert_id");
    next.delete("project_id");
    next.delete("view");
    if (!id && projectID) next.set("project_id", projectID);
    route(id, next);
    setProjectMenuOpen(false);
  };
  const selectTask = (id: string | null) =>
    startConversation(
      id,
      scope === "project"
        ? scopeProjectID ?? resolved?.projectID ?? null
        : null,
    );
  const selectScope = (value: WorkspaceScope) => {
    const next = new URLSearchParams(params);
    next.set("workspace_scope", value);
    next.delete("scope_project");
    next.delete("view");
    if (projectPage && !taskID) {
      next.delete("project_id");
      next.delete("expert_id");
    }
    route(taskID ?? null, next);
    setProjectMenuOpen(false);
  };
  const openProject = (project: Project) => {
    const next = new URLSearchParams(params);
    next.set("workspace_scope", "project");
    next.set("scope_project", project.project_id);
    next.set("project_id", project.project_id);
    next.set("view", "project");
    next.delete("expert_id");
    route(null, next);
    setProjectMenuOpen(false);
  };
  const manage = (project?: Project | "new") => {
    setManagedProjectID(
      project && project !== "new"
        ? project.project_id
        : resolved?.projectID ?? scopeProjectID,
    );
    setEditorRequest(project ? { project, revision: Date.now() } : undefined);
    setManagerOpen(true);
    setProjectMenuOpen(false);
  };
  const listScope = useMemo<TaskConversationListScope>(() => {
    if (scope === "project")
      return { projectID: scopeProjectID ?? resolved?.projectID ?? null };
    if (scope === "standalone") return { projectID: null };
    if (scope === "active") return { active: true };
    if (scope === "pinned") return { pinned: true };
    return {};
  }, [scope, scopeProjectID, resolved?.projectID]);
  const removedProject = (id: string) => {
    if (resolved?.projectID !== id && scopeProjectID !== id) return;
    const next = new URLSearchParams(params);
    ["project_id", "scope_project", "view", "expert_id"].forEach((key) =>
      next.delete(key),
    );
    next.set("workspace_scope", "all");
    route(null, next, true);
    setManagerOpen(false);
  };
  const more = (
    <Popover
      trigger="click"
      placement="bottomLeft"
      content={
        <div className={styles.moreMenu}>
          <Button
            type="text"
            icon={<Settings2 size={14} />}
            onClick={() => manage()}
          >
            {t("projects.workspace.manageProjects")}
          </Button>
          {!hostID && <TaskMigrations projects={projects} />}
          <Button
            type="text"
            icon={<History size={14} />}
            onClick={() => guardNavigation(() => navigate("/chat"))}
          >
            {t("projects.tasks.legacyHistory")}
          </Button>
        </div>
      }
    >
      <Button type="text" size="small" icon={<MoreHorizontal size={15} />}>
        {t("projects.workspace.more")}
      </Button>
    </Popover>
  );
  const navigation = (
    <ProjectNavigator
      projects={projects}
      scope={scope}
      projectID={scopeProjectID}
      onScope={selectScope}
      onProject={openProject}
      onEdit={(project) => manage(project)}
      onCreate={() => manage("new")}
      more={more}
    />
  );
  const ready =
    resolved?.taskID === taskID &&
    (taskID || resolved?.projectID === projectParam) &&
    resolved?.expertID === expertParam &&
    resolved?.projectOverview === (projectPage && !taskID);
  const overviewProject = resolved?.projectOverview
    ? resolved.project
    : undefined;

  return (
    <section className={styles.workspace} aria-label={t("projects.title")}>
      <header className={styles.toolbar}>
        <div className={styles.brand}>
          <span className={styles.brandDot} />
          <h1>{t("projects.title")}</h1>
        </div>
        <Popover
          trigger="click"
          placement="bottomLeft"
          open={projectMenuOpen}
          onOpenChange={setProjectMenuOpen}
          content={<div className={styles.navigationPopup}>{navigation}</div>}
        >
          <Button
            type="text"
            size="small"
            icon={<Folder size={15} />}
            aria-label={t("projects.workspace.projectMenu")}
          >
            {t("projects.list")}
            <ChevronDown size={12} />
          </Button>
        </Popover>
        <div className={styles.toolbarActions}>
          <Select
            className={styles.projectSelect}
            size="small"
            aria-label={t("projects.tasks.project")}
            disabled={!!taskID || projectPage}
            value={resolved?.projectID ?? ""}
            options={[
              { value: "", label: t("projects.tasks.noProject") },
              ...projects
                .filter(
                  (project) =>
                    !project.archived ||
                    project.project_id === resolved?.projectID,
                )
                .map((project) => ({
                  value: project.project_id,
                  label: project.name,
                  disabled: project.archived,
                })),
            ]}
            onChange={(id: string) => startConversation(null, id || null)}
          />
          <Button
            type="text"
            size="small"
            icon={<Monitor size={15} />}
            onClick={() => setHostsOpen(true)}
          >
            {hostID
              ? t("projects.workspace.remoteHost")
              : t("projects.tasks.hosts.local")}
          </Button>
        </div>
      </header>
      <div className={styles.body} aria-busy={!ready}>
        {resolved && (
          <div className={styles.conversationBody} hidden={!ready || !!error}>
            <TaskConversations
              projectID={resolved.projectID}
              project={resolved.project}
              projectOptions={projects}
              queue={queue}
              selectedTaskID={resolved.taskID ?? null}
              onSelectTask={selectTask}
              onWorkbenchDirtyChange={setWorkbenchDirty}
              allTasks
              defaultExpertID={resolved.expertID}
              workspace
              navigation={navigation}
              listScope={listScope}
              overview={
                overviewProject ? (
                  <ProjectOverview
                    project={overviewProject}
                    onEdit={() => manage(overviewProject)}
                    onManage={() => manage()}
                    onNew={() =>
                      startConversation(null, overviewProject.project_id)
                    }
                  />
                ) : undefined
              }
            />
          </div>
        )}
        {error ? (
          <div className={styles.loading}>
            <Alert
              type="error"
              message={error}
              action={
                <Button onClick={refresh}>{t("projects.refresh")}</Button>
              }
            />
          </div>
        ) : !ready ? (
          <div className={styles.loading}>
            <Spin />
          </div>
        ) : null}
      </div>
      <Drawer
        title={t("projects.workspace.manageProjects")}
        open={managerOpen}
        onClose={() => setManagerOpen(false)}
        width={960}
      >
        <ProjectsManager
          embedded
          initialProjectID={managedProjectID}
          editorRequest={editorRequest}
          queue={queue}
          onChanged={refresh}
          onRemoved={removedProject}
          onOpenConversation={(id, projectID) => {
            startConversation(id, projectID);
            setManagerOpen(false);
          }}
        />
      </Drawer>
      <Drawer
        title={t("projects.tasks.hosts.host")}
        open={hostsOpen}
        onClose={() => setHostsOpen(false)}
        width={560}
      >
        <TaskHostDirectory
          hostID={hostID}
          onNavigate={(url) => guardNavigation(() => navigate(url))}
        />
      </Drawer>
    </section>
  );
}
